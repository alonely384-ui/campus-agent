"""配置中心：所有 Key、地址、开关都从 .env 文件读，代码里不写死任何密钥。

为什么单独搞一个文件？
    如果 API Key 写散在 10 个文件里，哪天换了 Key 你得改 10 个地方；
    而且一不小心把 Key 提交到 GitHub，钱就会被别人刷光。

本模块还提供**运行时改配置并落盘**的能力（设置中心用）：
    set_env_value("LLM_MODEL", "deepseek-reasoner")
        → 改 .env 里那一行的值（其余字节原样不动）
        → 同步刷新 os.environ 与 settings（环境变量优先级高于 .env，见 _reload_setting）

三条铁律：
    1. 只做行级替换，不整份重写 .env —— 注释、顺序、空行全部保留。
    2. 临时文件 + os.replace 原子替换 —— 写到一半断电，磁盘上仍是旧文件。
    3. 写盘失败必须把内存改回去 —— 不留下"内存新、磁盘旧"的分裂状态。

为什么 RAG 参数要进 Settings 的字段，而不是各自 os.getenv？
    pydantic-settings 读 .env 只是把值填进 Settings 对象，**不会**把 .env 的键
    写进 os.environ。所以"用户在 .env 里写了 RAG_W_DENSE=3.25" 这种事，
    os.getenv("RAG_W_DENSE") 是拿不到的。设置中心保存的值重启后就丢了。
    结论：凡是想让用户改的参数，一律走 Settings 字段 + settings_api 注册，
    不允许再写 `os.getenv("RAG_XXX")`。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 这里不能 `from app.logging_config import get_logger`：
# logging_config 反过来 import 本模块的 settings，会形成循环导入。
# 直接用标准库拿 logger，效果完全一样（同一个 logger 对象）。
logger = logging.getLogger(__name__)

# .env 所在路径。默认是进程工作目录下的 .env；测试里可以指到临时文件。
ENV_FILE = Path(".env")

# 进程**启动那一刻**操作系统里本来就有的环境变量名（只记名字，不记值）。
#
# 为什么需要：set_env_value() 为了让新值立刻生效，会把值同步写进 os.environ。
# 于是"这个键在 os.environ 里"就不再等于"用户/系统真的设置了这个环境变量"——
# 用户每保存一次，这一项就把自己伪装成被系统环境变量覆盖，
# 设置页于是弹出一条假的"重启后会被盖回去"警告。
#
# 判定"是否被系统环境变量覆盖"必须以**启动时**为准：启动后才出现的键一定是
# 我们自己写进去的，不算数。想判断某键是否在快照里，用 was_in_system_env()。
_SYSTEM_ENV_AT_BOOT: frozenset[str] = frozenset(os.environ)


def was_in_system_env(key: str) -> bool:
    """``key`` 在进程启动时是否已存在于操作系统环境变量中。

    只有返回 True，才算"用户在系统层面设置了这个变量"（systemd Environment=、
    Docker -e、shell export 等），才值得警告"重启后会被盖回去"。
    """
    return key in _SYSTEM_ENV_AT_BOOT

# 写 .env 时的互斥锁：两个标签页同时点保存，不能交错写同一个文件
_ENV_LOCK_MUTEX = None


def _lock():
    """延迟创建锁：模块导入时不建对象，避免和线程相关的副作用。"""
    global _ENV_LOCK_MUTEX
    if _ENV_LOCK_MUTEX is None:
        import threading

        _ENV_LOCK_MUTEX = threading.Lock()
    return _ENV_LOCK_MUTEX


def _env_path() -> Path:
    """当前生效的 .env 路径。延迟取值，保证测试里改环境变量后立即生效。"""
    return Path(os.environ.get("CAMPUS_ENV_FILE") or ENV_FILE)


class Settings(BaseSettings):
    """从 .env 读取的配置。字段名必须和 .env 里的变量名一致（大小写不敏感）。"""

    # ---- LLM ----
    llm_api_key: str = "sk-请填写"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # ---- 向量库 ----
    chroma_dir: str = "./data/chroma"

    # ---- 数据库 ----
    redis_url: str = "redis://localhost:6379/0"
    mysql_url: str = "mysql+pymysql://root:password@localhost:3306/campus_agent"

    # ---- 服务 ----
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # ---- RAG 检索参数（设置中心 B 模块可改，见 app/settings_api.py） ----
    # 类型决定 pydantic 怎么解析 .env 里的字符串：int 字段读 "77" 得到 77，
    # 字段声明成 str 再自己转，设置页的 kind 和这里的类型必须一致。
    rag_w_dense: float = 2.0
    rag_w_bm25: float = 1.0
    rag_w_hash: float = 0.5
    rag_rrf_k: int = 60
    # .env 里写 0/1，这里先收成 int，pipeline 再按 !=0 转 bool
    rag_rerank: int = 1
    rag_rerank_pool: int = 15
    rag_recall_depth: int = 50
    rag_citation_penalty: float = 0.6

    # ---- 定时刷新（设置中心 C 模块可改） ----
    discover_schedule: str = "daily"
    discover_hour: int = 8

    # 不在这里写死 env_file：读哪个文件由下面的 settings_customise_sources
    # 在每次构造时动态决定，保证和写入侧（_env_path）指向同一个文件。
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """让「读」和「写」用同一个 .env。

        默认情况下 pydantic 读 model_config["env_file"]，那个值在导入时就定死了；
        写入侧却认 CAMPUS_ENV_FILE。两边可能指向不同文件，会出现
        「设置页保存成功、重启后读不到」的分裂。

        这里每次实例化都按 _env_path() 现建一个 DotEnvSettingsSource，
        优先级仍是 init > 环境变量 > .env > 默认值，和原来一致。
        """
        from pydantic_settings import DotEnvSettingsSource

        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(
                settings_cls,
                env_file=_env_path(),
                env_file_encoding="utf-8",
            ),
            file_secret_settings,
        )


# 全局唯一的配置对象，其他文件 `from app.config import settings` 直接用
settings = Settings()


# ---------------------------------------------------------------- .env 读写


def _line_re(key: str) -> re.Pattern[str]:
    """匹配 ``KEY=...`` 那一行（允许前后空格、允许 export 前缀）。"""
    return re.compile(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=\s*)(.*)$")


def read_env_file() -> dict[str, str]:
    """把 .env 读成字典。读不到或没有文件都返回空字典，不抛异常。

    只用于「诊断/展示当前落盘值」，不要用它替代 settings ——
    环境变量的优先级高于 .env，最终生效值以 settings 为准。
    """
    path = _env_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        out[key] = value.strip()
    return out


def _write_env_lines(lines: list[str]) -> None:
    """把行列表**原子地**写回 .env。

    先写同目录的临时文件，`os.replace` 再替换目标。
    同目录很关键：跨盘符的 replace 不是原子操作。
    失败时清理临时文件并抛出，绝不吞掉。
    """
    path = _env_path()
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            if lines and not lines[-1].endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def set_env_value(key: str, value: str) -> None:
    """把 ``key`` 写进 .env（行级替换），并让新值立刻在进程内生效。

    原子性说明：
        磁盘侧用临时文件 + os.replace，崩在写入中途磁盘仍是旧文件；
        内存侧先记住旧值，写盘抛异常就整体回滚，不留"内存新、磁盘旧"。
    """
    key = key.strip()
    if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"非法的配置键名: {key!r}")
    if "\n" in value or "\r" in value:
        # 值里夹换行会破坏 .env 结构（甚至能注入别的键）
        raise ValueError("配置值不能包含换行符")

    with _lock():
        path = _env_path()
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            original = ""

        pattern = _line_re(key)
        lines = original.splitlines()
        replaced = False
        new_lines: list[str] = []
        for line in lines:
            match = pattern.match(line)
            if match and not replaced:
                new_lines.append(f"{match.group(1)}{value}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{key}={value}")

        _write_env_lines(new_lines)

    # 磁盘写成功之后才动内存：顺序不能反，否则写盘失败就无法回滚
    _reload_setting(key, value)
    logger.info("配置已更新: %s", key)  # 只记键名，绝不记值


def _reload_setting(key: str, value: str) -> None:
    """让新值在进程内生效。

    必须**同时**写 os.environ：
    pydantic-settings 的优先级是「环境变量 > .env > 默认值」，
    只改 .env 不改环境变量，那些已经被环境变量盖住的键重启后会被盖回去，
    出现"设置页显示新值、实际运行还是旧值"的假象。
    """
    os.environ[key] = value
    field = key.lower()
    if field in Settings.model_fields:
        setattr(settings, field, value)


def unset_env_value(key: str) -> None:
    """从 .env 删掉某一项，并清掉内存里的覆盖值（恢复默认值用）。"""
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"非法的配置键名: {key!r}")

    with _lock():
        path = _env_path()
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        pattern = _line_re(key)
        new_lines = [line for line in original.splitlines() if not pattern.match(line)]
        if len(new_lines) != len(original.splitlines()):
            _write_env_lines(new_lines)

    os.environ.pop(key, None)
    field = key.lower()
    if field in Settings.model_fields:
        default = Settings.model_fields[field].get_default(call_default_factory=True)
        setattr(settings, field, default)
    logger.info("配置已恢复默认: %s", key)


def reload_settings() -> None:
    """重新从 .env + 环境变量构造 settings（进程启动或测试用）。"""
    fresh = Settings()
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))