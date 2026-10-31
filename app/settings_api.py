"""设置中心的服务层：白名单 + 校验 + 读写 + 生效。

设计原则（对应审计报告 docs/SETTINGS-AUDIT.md）：
    1. 只暴露后端**真正支持热更新**的项。审计里判定"改了不生效"的
       GRAPH_FUZZY_ACCEPT / RETRIEVE_TOP_K / DEFAULT_CHUNK_SIZE 一律不进来。
    2. 校验必须在服务端做一遍，前端 max/min 只是提示。
    3. API Key 永不出现在任何响应里，只给 `has_key` 与末 4 位掩码。
    4. 写配置走 app.config 的原子写；生效失败要把磁盘和内存一起回滚。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app import config, llm
from app.logging_config import get_logger

logger = get_logger(__name__)

# 默认值取自 app/config.py 与各模块的 os.getenv 兜底值，两边必须一致
SECRET_PLACEHOLDERS = ("sk-请填写", "sk-在这里填你的key", "")


# ---------------------------------------------------------------- 参数定义


@dataclass(frozen=True)
class Defense:
    """数值项的边界与说明。"""

    lo: float
    hi: float
    step: float
    hint: str


@dataclass(frozen=True)
class Setting:
    """一个可在设置页里改的项。"""

    key: str  # 写进 .env 的键名
    group: str  # model / rag / schedule
    label: str  # 界面上的名字
    kind: Literal["str", "float", "int", "bool", "enum"]
    default: Any  # 恢复默认值时用的值（None 表示"从 .env 删掉这一行"）
    hint: str = ""
    defense: Defense | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    # 改了之后必须重置 LLM 客户端才生效
    needs_client_reset: bool = False
    # RAG 项对应 app.rag.pipeline 里的模块全局名。留空表示"_" + key 去掉 RAG_ 前缀。
    # 必须能显式指定，因为 RAG_RERANK 的真实变量叫 _RERANK_ENABLED，并非 _RERANK。
    attr: str = ""


def _d(lo: float, hi: float, step: float, hint: str) -> Defense:
    return Defense(lo=lo, hi=hi, step=step, hint=hint)


SETTINGS: dict[str, Setting] = {}


def _reg(item: Setting) -> Setting:
    SETTINGS[item.key] = item
    return item


# ---- A. 模型设置 ----
_reg(
    Setting(
        key="LLM_MODEL",
        group="model",
        label="模型名称",
        kind="str",
        default="deepseek-chat",
        hint="填写服务商支持的模型名，例如 deepseek-chat / deepseek-reasoner",
    )
)
_reg(
    Setting(
        key="LLM_BASE_URL",
        group="model",
        label="Base URL",
        kind="str",
        default="https://api.deepseek.com",
        hint="OpenAI 兼容接口地址，需以 http:// 或 https:// 开头",
        needs_client_reset=True,
    )
)
_reg(
    Setting(
        key="LLM_API_KEY",
        group="model",
        label="API Key",
        kind="str",
        default=None,
        hint="只写不回显；保存后立即重建客户端",
        secret=True,
        needs_client_reset=True,
    )
)

# ---- B. RAG 检索设置：审计确认可热更新的 9 项 ----
_reg(
    Setting(
        key="RAG_W_DENSE",
        group="rag",
        label="稠密通道权重",
        kind="float",
        default="2.0",
        hint="ONNX 句向量通道在 RRF 融合里的权重",
        defense=_d(0.0, 10.0, 0.1, "0 表示完全不使用稠密通道"),
    )
)
_reg(
    Setting(
        key="RAG_W_BM25",
        group="rag",
        label="BM25 通道权重",
        kind="float",
        default="1.0",
        hint="关键词通道权重，提高它会偏向字面命中",
        defense=_d(0.0, 10.0, 0.1, "0 表示不使用关键词通道"),
    )
)
_reg(
    Setting(
        key="RAG_W_HASH",
        group="rag",
        label="哈希通道权重",
        kind="float",
        default="0.5",
        hint="离线兜底通道；稠密通道不可用时会自动接管稠密的权重",
        defense=_d(0.0, 10.0, 0.1, "0 表示不使用哈希兜底"),
    )
)
_reg(
    Setting(
        key="RAG_RRF_K",
        group="rag",
        label="RRF 平滑常数",
        kind="int",
        default="60",
        hint="越大越平缓（名次差异影响变小），业界常用 60",
        defense=_d(1, 200, 1, "取值范围 1-200"),
    )
)
_reg(
    Setting(
        key="RAG_RERANK",
        group="rag",
        label="启用重排序",
        kind="bool",
        default="1",
        hint="用交叉编码器对候选重排；关闭后退回纯 RRF 排序",
        attr="_RERANK_ENABLED",
    )
)
_reg(
    Setting(
        key="RAG_RERANK_POOL",
        group="rag",
        label="重排候选池",
        kind="int",
        default="15",
        hint="送入重排的候选条数。实测 10/15/20 的 R@1 为 28/31/30（38 题）",
        defense=_d(5, 50, 1, "取值范围 5-50，过大显著变慢"),
    )
)
_reg(
    Setting(
        key="RAG_RECALL_DEPTH",
        group="rag",
        label="召回深度",
        kind="int",
        default="50",
        hint="每个通道先取多少条再融合",
        defense=_d(10, 200, 5, "取值范围 10-200"),
    )
)
_reg(
    Setting(
        key="RAG_CITATION_PENALTY",
        group="rag",
        label="参考文献降权系数",
        kind="float",
        default="0.6",
        hint="命中参考文献区域的块乘以此系数，减少「引用了但没讲」的干扰",
        defense=_d(0.0, 1.0, 0.05, "0 表示完全丢弃引用段落，1 表示不降权"),
    )
)

# ---- C. 定时刷新 ----
_reg(
    Setting(
        key="DISCOVER_SCHEDULE",
        group="schedule",
        label="刷新周期",
        kind="enum",
        default="daily",
        hint="daily=每天，weekly=每周一，off=不自动刷新",
        choices=("daily", "weekly", "off"),
    )
)
_reg(
    Setting(
        key="DISCOVER_HOUR",
        group="schedule",
        label="执行时刻（小时）",
        kind="int",
        default="8",
        hint="本地时间 0-23 点。改完下一次启动生效",
        defense=_d(0, 23, 1, "取值范围 0-23"),
    )
)


# ---------------------------------------------------------------- 校验


class SettingError(ValueError):
    """校验失败。message 会原样展示给用户，必须是能看懂的中文。"""


def _check_url(value: str) -> str:
    if not re.match(r"^https?://", value):
        raise SettingError("Base URL 必须以 http:// 或 https:// 开头")
    if any(ch in value for ch in " \t"):
        raise SettingError("Base URL 不能包含空格")
    return value.rstrip("/") or value


def _check_numeric(item: Setting, raw: str) -> str:
    defense = item.defense
    assert defense is not None
    try:
        num = float(raw)
    except (TypeError, ValueError):
        raise SettingError(f"{item.label}必须是数字") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise SettingError(f"{item.label}必须是有限数字")
    if item.kind == "int":
        if num != int(num):
            raise SettingError(f"{item.label}必须是整数")
        num = int(num)
    if not (defense.lo <= num <= defense.hi):
        raise SettingError(
            f"{item.label}必须在 {defense.lo:g} ~ {defense.hi:g} 之间（当前输入 {num:g}）"
        )
    return str(int(num)) if item.kind == "int" else f"{num:g}"


def canonical(key: str, raw: Any) -> str:
    """校验并规范化一个值，返回要写进 .env 的字符串。"""
    item = SETTINGS.get(key)
    if item is None:
        raise SettingError(f"不支持的配置项: {key}")

    text = "" if raw is None else str(raw).strip()

    if item.kind == "bool":
        low = text.lower()
        if low in ("1", "true", "on", "yes"):
            return "1"
        if low in ("0", "false", "off", "no"):
            return "0"
        raise SettingError(f"{item.label}只能是开或关")

    if item.kind == "enum":
        low = text.lower()
        if low not in item.choices:
            raise SettingError(f"{item.label}只能是 {' / '.join(item.choices)} 之一")
        return low

    if item.kind in ("int", "float"):
        return _check_numeric(item, text)

    # str
    if not text:
        raise SettingError(f"{item.label}不能为空")
    if len(text) > 300:
        raise SettingError(f"{item.label}过长（最多 300 字符）")
    if "\n" in text or "\r" in text:
        raise SettingError(f"{item.label}不能包含换行")
    if item.key == "LLM_BASE_URL":
        return _check_url(text)
    return text


# ---------------------------------------------------------------- 读取


def mask_key(value: str) -> str:
    """脱敏：只留末 4 位。这是 API Key 唯一允许出现在响应里的形式。"""
    if not value or value in SECRET_PLACEHOLDERS:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def has_real_key() -> bool:
    """当前 key 是不是真的填过（不是占位符、不是空）。"""
    key = (config.settings.llm_api_key or "").strip()
    return bool(key) and key not in SECRET_PLACEHOLDERS


def _pipeline_attr(item: Setting) -> str:
    """RAG 项在 app.rag.pipeline 里的模块全局名。"""
    return item.attr or "_" + item.key.replace("RAG_", "", 1)


def current_value(item: Setting) -> str:
    """读当前**生效**的值。

    注意读的是 settings / 模块常量，而不是 .env 文件 ——
    环境变量的优先级高于 .env，只有前者才代表真正在跑的值。

    RAG 项要同时看两处，顺序与环境变量优先级保持一致：
    os.environ（进程内热更新/系统环境变量）优先，
    拿不到再回落到 settings 字段（重启后由 .env 填进来）。
    只看模块全局的话，重启后设置页会显示代码默认值，而实际跑的是 .env 里的值。
    """
    if item.group == "rag":
        from app.rag import pipeline

        env = os.environ.get(item.key)
        if env is not None and env != "":
            return env  # 原样显示，是否合法由 pipeline 的解析逻辑决定
        attr = _pipeline_attr(item)
        if hasattr(pipeline, attr):
            return _stringify(item, getattr(pipeline, attr))
        return _stringify(item, getattr(config.settings, item.key.lower(), item.default))

    if item.key == "LLM_MODEL":
        return config.settings.llm_model
    if item.key == "LLM_BASE_URL":
        return config.settings.llm_base_url
    if item.key == "LLM_API_KEY":
        return config.settings.llm_api_key
    if item.key == "DISCOVER_SCHEDULE":
        env = os.environ.get("DISCOVER_SCHEDULE")
        if env:
            return env.strip().lower()
        return str(getattr(config.settings, "discover_schedule", "daily") or "daily").lower()
    if item.key == "DISCOVER_HOUR":
        env = os.environ.get("DISCOVER_HOUR")
        if env:
            return env
        return str(getattr(config.settings, "discover_hour", 8))
    return item.default or ""


def _stringify(item: Setting, value: Any) -> str:
    """把 bool / int / float 统一转成 .env 里能原样写回、也能原样解析的字符串。

    bool 必须转成 "1"/"0"：canonical() 认得 1/0，而 str(True) 是 "True"，
    虽然也能解析，但写回 .env 后不如 0/1 直观。
    """
    if item.kind == "bool":
        return "1" if value else "0"
    if value is None:
        return "" if item.default is None else str(item.default)
    return str(value)


def env_overrides(item: Setting) -> dict[str, Any]:
    """检查这一项是不是被**系统环境变量**盖住了。

    为什么需要：pydantic-settings 的优先级是「环境变量 > .env > 默认值」。
    用户在设置页改完，我们通过 config._reload_setting 把 os.environ 设成新值，
    本次进程内是对的；但如果操作系统里本来就设了同名变量（比如 systemd 的
    Environment=、Docker 的 -e、shell 的 $env:），**进程重启后**又会变回系统值。
    不提示的话用户会以为"我明明改了，怎么重启又回去了"。

    判定依据必须是**进程启动时**的环境变量快照，不能直接看 os.environ：
    set_env_value() 保存时会同步写 os.environ，所以保存过一项之后，
    这一项就永远能在 os.environ 里查到——用它做判据会导致"改什么就警告什么"，
    把真警告淹没在假警告里。见 config.was_in_system_env()。
    """
    if not config.was_in_system_env(item.key):
        # 启动时没有这个变量，现在的值只可能是我们自己保存进去的
        return {"overridden": False}
    value = os.environ.get(item.key)
    if value is None:
        # 启动时有、现在被删了（比如恢复默认值），同样不算覆盖
        return {"overridden": False}
    return {"overridden": True, "env_value": "" if item.secret else value}


def describe(item: Setting) -> dict[str, Any]:
    """把一项设置序列化成前端要的形状。敏感项只给掩码。"""
    raw = current_value(item)
    payload: dict[str, Any] = {
        "key": item.key,
        "group": item.group,
        "label": item.label,
        "kind": item.kind,
        "hint": item.hint,
        # 前端靠它决定这一项是不是「留空即不修改」的敏感输入框
        "secret": item.secret,
    }
    if item.secret:
        payload["has_key"] = has_real_key()
        payload["masked"] = mask_key(raw)
        payload["value"] = ""
    else:
        payload["value"] = raw
    if item.defense is not None:
        payload["lo"] = item.defense.lo
        payload["hi"] = item.defense.hi
        payload["step"] = item.defense.step
        payload["range_hint"] = item.defense.hint
    if item.choices:
        payload["choices"] = list(item.choices)
    if item.default is None:
        payload["default"] = None
    else:
        payload["default"] = item.default
    env = env_overrides(item)
    payload["env_override"] = env["overridden"]
    if env["overridden"]:
        payload["env_value"] = env.get("env_value", "")
    return payload


def snapshot() -> dict[str, Any]:
    """整个设置页要的数据：按 group 分好组。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in SETTINGS.values():
        groups.setdefault(item.group, []).append(describe(item))
    overridden = [k for k, v in SETTINGS.items() if env_overrides(v)["overridden"]]
    return {
        "groups": groups,
        "env_file": str(config._env_path()),
        "key_configured": has_real_key(),
        # 被系统环境变量覆盖的键名列表；前端据此在页头挂一条醒目提示
        "env_overrides": overridden,
    }


def about() -> dict[str, Any]:
    """设置页「关于」分区要的数据。版本号取 app.main 里的同一个常量。"""
    import sys

    from app.main import VERSION

    return {
        "name": "科研小助手",
        "version": VERSION,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "stack": [
            "FastAPI + Uvicorn（Web 服务）",
            "ChromaDB（向量库）+ 自实现 BM25 / RRF 融合",
            "ONNX Runtime + ms-marco-MiniLM-L-6-v2（交叉编码器重排）",
            "PyMuPDF + pdfplumber + RapidOCR（文档解析）",
            "ECharts + pdf.js（前端，本地内置，无 CDN 依赖）",
            "OpenAI 兼容接口（默认 DeepSeek）",
        ],
    }


# ---------------------------------------------------------------- 写入


def _apply_runtime(item: Setting, value: str) -> None:
    """把值刷进内存里的运行态（RAG 模块全局 + settings + os.environ）。"""
    if item.group == "rag":
        from app.rag import pipeline

        attr = _pipeline_attr(item)
        # 变量名对不上就是代码改错了，必须炸出来，不能静默地把设置当成功
        if not hasattr(pipeline, attr):
            raise RuntimeError(f"app.rag.pipeline 里没有 {attr}，配置无法生效")
        if item.kind == "bool":
            setattr(pipeline, attr, value != "0")
        elif item.kind == "int":
            setattr(pipeline, attr, int(value))
        else:
            setattr(pipeline, attr, float(value))
        # 必须同时刷 settings：重启后 .env 由 pydantic 填进 settings，
        # 只改模块全局的话，重启就把用户改的值丢了（只在本次进程内有效）。
        config._reload_setting(item.key, value)
        return
    # 模型与定时器走 config 的 env + settings 同步
    config._reload_setting(item.key, value)
    # 定时周期光改配置不会让已经在跑的任务换挡：scheduler 的线程是启动时建好的，
    # 只改 .env 会变成「设置页显示 off、后台还在按 daily 跑」。这里立刻重启它。
    if item.key == "DISCOVER_SCHEDULE":
        _restart_scheduler(value)


def _restart_scheduler(mode: str) -> None:
    """按新周期重启定时线程。失败必须抛，由 save() 走回滚。

    注意 start() 返回的是 dict，不能直接 if not info —— 非空 dict 恒为真，
    会把「切失败」当成成功。要看 running 字段。off 模式下 running 本就为 False，
    这不算失败。
    """
    from app import scheduler

    info = scheduler.start(mode)
    if mode != "off" and not info.get("running"):
        raise RuntimeError(f"定时刷新没能切到 {mode}，请检查服务日志")


def _snapshot_env_line(key: str) -> tuple[bool, str]:
    """回滚用：记下这个键在 .env 里原本**存不存在**、原值是什么。

    只记 current_value() 是不够的：原 .env 里根本没有这一行时，
    current_value() 返回的是代码默认值，回滚会把它**写进** .env，
    凭空多出一行 —— 用户什么都没改，配置文件却变了。
    所以必须区分「原本没有」与「原本是某个值」。
    """
    lines = config.read_env_file()
    if key in lines:
        return True, lines[key]
    return False, ""


def _rollback_env(key: str, existed: bool, old: str) -> None:
    """把 .env 里这个键恢复到调用前的样子：原本没有就删掉，有就写回原值。"""
    if existed:
        config.set_env_value(key, old)
    else:
        config.unset_env_value(key)


def _apply_runtime_failed(item: Setting, old: str) -> None:
    """生效阶段抛异常时，把运行态改回去，别留下半拉子状态。"""
    try:
        _apply_runtime(item, old)
    except Exception:  # noqa: BLE001 —— 回滚本身失败只能记日志，但要让人看见
        logger.exception("配置 %s 生效失败后，回滚运行态也失败了", item.key)


def save(key: str, raw: Any) -> dict[str, Any]:
    """保存一项配置。返回新值；任何一步失败都抛 SettingError 或原样抛出。"""
    item = SETTINGS.get(key)
    if item is None:
        raise SettingError(f"不支持的配置项: {key}")

    value = canonical(key, raw)
    old = current_value(item)
    existed, old_line = _snapshot_env_line(item.key)

    if item.secret and value in SECRET_PLACEHOLDERS:
        raise SettingError("API Key 不能是占位符")

    # 1) 先写磁盘（原子）。失败会抛，此时内存和 .env 都还是旧的
    config.set_env_value(item.key, value)

    # 2) 再刷运行态。这一步若失败，必须把磁盘和内存一起改回去
    try:
        _apply_runtime(item, value)
    except Exception as exc:  # noqa: BLE001
        logger.exception("配置 %s 写入成功但生效失败，开始回滚", item.key)
        try:
            _rollback_env(item.key, existed, old_line)
        except Exception:  # noqa: BLE001
            logger.exception("回滚 %s 的 .env 写入失败，磁盘可能残留新值", item.key)
        _apply_runtime_failed(item, old)
        raise SettingError(f"{item.label}生效失败，已回滚：{exc}") from exc

    return {"key": key, "value": "" if item.secret else value, "restart_required": False}


def restore_default(key: str) -> dict[str, Any]:
    """单项恢复默认。

    默认值是 None 的（API Key）表示「从 .env 删掉这一行」，
    其它项写回代码里的默认值 —— 都通过 config 的原子接口完成。
    """
    item = SETTINGS.get(key)
    if item is None:
        raise SettingError(f"不支持的配置项: {key}")

    old = current_value(item)
    existed, old_line = _snapshot_env_line(item.key)
    if item.default is None:
        config.unset_env_value(item.key)
    else:
        config.set_env_value(item.key, str(item.default))

    new = current_value(item) if item.default is None else str(item.default)
    try:
        _apply_runtime(item, new)
    except Exception as exc:  # noqa: BLE001
        logger.exception("恢复默认 %s 后生效失败，开始回滚", item.key)
        _rollback_env(item.key, existed, old_line)
        _apply_runtime_failed(item, old)
        raise SettingError(f"{item.label}恢复默认失败，已回滚：{exc}") from exc

    # 恢复 API Key 等于让 key 失效，客户端必须重建，否则还会拿着旧 key 发请求
    if item.needs_client_reset:
        llm.reset_client()
    return {"key": key, "value": "" if item.secret else new}


@dataclass
class ConnectionProbe:
    ok: bool
    model: str
    base_url: str
    latency_ms: int = 0
    detail: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


async def test_connection(*, timeout: float = 20.0) -> ConnectionProbe:
    """用**当前生效的配置**真发一次最小请求，验证 key / base_url / model 都对。

    返回里的 error 必须是人话 + 上游原文，方便用户自己判断是 key 错、
    余额不足还是模型名不存在。绝不把 key 拼进错误信息。
    """
    import time

    from openai import AsyncOpenAI

    model = config.settings.llm_model
    base_url = config.settings.llm_base_url
    probe = ConnectionProbe(ok=False, model=model, base_url=base_url)

    if not has_real_key():
        probe.error = "还没有配置 API Key，请先在上面填入并保存"
        return probe

    client = AsyncOpenAI(
        api_key=config.settings.llm_api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,  # 测连接要的是「现在就告诉我」，不做退避重试
    )
    started = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        probe.latency_ms = int((time.perf_counter() - started) * 1000)
        probe.ok = True
        probe.detail = f"模型 {resp.model or model} 响应正常"
    except Exception as exc:  # noqa: BLE001 —— 这一层就是要把各种异常翻译成人话
        probe.latency_ms = int((time.perf_counter() - started) * 1000)
        probe.error = _explain(exc)
        logger.info("测试连接失败: %s", type(exc).__name__)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭测试连接客户端失败", exc_info=True)
    return probe


def _explain(exc: Exception) -> str:
    """把上游异常翻译成能落地排查的提示。"""
    import httpx

    name = type(exc).__name__
    text = str(exc)

    status = getattr(exc, "status_code", None)
    if status == 401:
        return f"API Key 无效或已失效（401）。上游原文：{text}"
    if status == 403:
        return f"API Key 没有访问该模型的权限（403）。上游原文：{text}"
    if status == 404:
        return f"模型名或 Base URL 不存在（404），请检查模型名称。上游原文：{text}"
    if status == 429:
        return f"触发限流或余额不足（429）。上游原文：{text}"
    if isinstance(status, int) and status >= 500:
        return f"上游服务异常（{status}），稍后重试。上游原文：{text}"
    if name == "APITimeoutError" or "timeout" in name.lower():
        return f"连接超时（{name}）。请检查 Base URL 是否可达、网络是否通畅。"
    if name == "APIConnectionError":
        return f"连不上服务器（{name}）。请检查 Base URL 与网络。上游原文：{text}"
    if isinstance(exc, httpx.HTTPError):
        return f"网络错误（{name}）：{text}"
    return f"{name}: {text}"