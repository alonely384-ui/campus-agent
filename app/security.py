"""公网安全层：管理员认证、IP 限流、LLM 调用预算、请求体上限。

设计约束（来自赛题场景，不是通用的安全模板）：
    评委打开演示地址就要能问答、能翻译、能读论文，**不能要求输密码**。
    所以这里不做全站 Basic Auth，只保护会改数据、改配置、删索引、烧额度的接口。

四层各自独立，任何一层可以单独关掉：
    1. 认证层   ADMIN_PATHS 里的路径必须带有效 Cookie，否则 401/403
    2. 限流层   按路径分档做令牌桶，超限 429 + Retry-After
    3. 预算层   统计真实模型调用次数，超出当日预算 429；落盘持久化
    4. 体积层   请求体实际读取字节数超限 413

为什么不用 nginx 的 limit_req：
    当前服务器没有 nginx、没有域名，而且 11-30 到期。把安全绑在 nginx 上，
    等于把「能否安全」绑在「是否续费 + 是否买域名」上。应用层实现随代码迁移。

为什么用中间件而不是给每个路由加 Depends：
    `/ui/*` 是 StaticFiles 挂载，不经过路由依赖，Depends 根本管不到它。
    中间件跑在 ASGI 层，先于 StaticFiles 与路由匹配执行，一处覆盖全部。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

# ============================================================ 路径分类
#
# 唯一事实来源。main.py 的 _assert_route_coverage() 会拿 app.routes 与这里对账，
# 有任何一个路由既不在 ADMIN 也不在 PUBLIC 里，就拒绝启动——「新接口默认公开」
# 这件事在架构上被堵死，不依赖开发者记性。

# 管理员专用：会改数据 / 改配置 / 删索引 / 烧额度
ADMIN_PATHS: frozenset[str] = frozenset(
    {
        "/settings",                      # GET 泄露配置，POST 改配置
        "/settings/restore",
        "/settings/schedule",             # GET 启停状态里可能带调度信息，一并保护
        "/settings/test-connection",
        "/settings/index/health",         # 单次 4 秒，CPU 密集
        "/settings/index/cleanup",        # 删除索引块
        "/settings/about",                # 技术栈清单，对竞赛对手有价值
        "/papers/{paper_id}",             # DELETE 删论文
    }
)

# 公开：演示功能 + 只读数据 + 页面本身
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        # ---- 只读 ----
        "/favicon.ico",
        "/health",
        "/papers/files",
        "/papers",
        "/papers/topics",
        "/reader/papers",
        "/paper/{filename}/structure",
        "/paper/{filename}/layout",
        "/paper/{filename}/translation",
        "/paper/{filename}",
        "/recommendations",
        "/writing/renderers",
        "/writing/chart/{name}",
        "/writing/diagram/{name}",
        # ---- 公开演示（有副作用，靠限流与预算保护） ----
        "/chat",
        "/chat/stream",
        "/upload",
        "/papers/upload",
        "/paper/{filename}/translate",
        "/paper/{filename}/figure/{index}",
        "/translate/selection",
        "/library/download",
        "/paper/{filename}/references/download",
        "/recommendations/refresh",
        "/recommendations/{key}/feedback",
        "/recommendations/schedule",
        "/graph",
        "/graph/survey",
        "/graph/frontier",
        "/writing/framework",
        "/writing/diagram",
        "/writing/paper-figures",
        "/writing/chart",
        "/writing/zh2en",
        # ---- 登录端点自身必须公开，否则永远登不进来 ----
        "/admin/login",
        "/admin/logout",
        # ---- 接口文档：**只在 dev 下真的存在** ----
        # 生产环境 FastAPI 的 openapi_url 被置为 None，这三条路由根本不注册，
        # 中间件拿到的会是「未登记路径」→ 404，效果与关文档一致。
        # 这里登记它们，是为了让 dev 环境下 /docs 能正常打开调试；
        # 对账逻辑会跳过 docs/redoc/openapi.json，所以不会因为生产缺失而报脱节。
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)

# 静态页面前缀：页面可以公开（否则评委打不开），页面里的管理操作由接口层挡住
PUBLIC_PREFIXES: tuple[str, ...] = ("/ui/",)

# 生产环境要关掉的文档端点（由 main.py 传给 FastAPI 的 docs_url=None 实现）
DOC_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", "/openapi.json"})


def normalize_path(path: str) -> str:
    """把路径规范成用于匹配的形态。

    只做两件事：去掉末尾多余的 `/`、把连续斜杠压成一个。
    不处理 `..`——那由 Starlette 的 StaticFiles 和路由自己管，
    这里越俎代庖反而可能把恶意路径「修」成合法路径。
    """
    if not path:
        return "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


# ============================================================ 凭证

_TOKEN_ENV = "ADMIN_TOKEN"
_SECRET_ENV = "ADMIN_COOKIE_SECRET"
COOKIE_NAME = "ca_admin"

# Cookie 有效期（秒）。评审现场一次演示不超过 2 小时，过期就要重新输。
SESSION_TTL = 2 * 60 * 60

# 登录失败锁定：同一 IP 连续失败 N 次锁 M 秒
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60


def _admin_token() -> str:
    return str(getattr(settings, "admin_token", "") or "").strip()


def _cookie_secret() -> str:
    return str(getattr(settings, "admin_cookie_secret", "") or "").strip()


def admin_configured() -> bool:
    """管理员凭证是否已配置。

    **没有配置就是没配置，绝不自动放行。**
    第一版设计里曾打算「ADMIN_TOKEN 为空时放行」当作应急开关，
    那等于给所有管理接口留了一个静默后门：部署时漏配一个环境变量，
    认证就整体失效且日志里一切正常。现在改成相反方向：
    没配凭证 → 所有管理接口一律 503，并每 60 秒记一次错误日志。
    """
    return bool(_admin_token()) and bool(_cookie_secret())


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(token: str) -> bool:
    """校验管理员口令。用常量时间比较，避免计时侧信道。"""
    expected = _admin_token()
    if not expected or not token:
        return False
    return hmac.compare_digest(_token_digest(token), _token_digest(expected))


def sign_session(now: float | None = None) -> str:
    """签发 Session Cookie 值：`<过期时间戳>.<HMAC-SHA256(secret, 时间戳)>`。

    服务端不存 session 表，所以进程重启不会把管理员踢下线；
    代价是没有「立即吊销单个会话」的能力——要全部吊销就换
    ADMIN_COOKIE_SECRET（所有旧 Cookie 立刻失效）。
    """
    expires = int((now if now is not None else time.time()) + SESSION_TTL)
    stamp = str(expires)
    mac = hmac.new(_cookie_secret().encode("utf-8"), stamp.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{stamp}.{mac}"


def verify_session(value: str | None) -> bool:
    """校验 Cookie 值：签名对 + 未过期。任一不满足都返回 False。"""
    secret = _cookie_secret()
    if not value or not secret or "." not in value:
        return False
    stamp, _, mac = value.partition(".")
    if not stamp.isdigit() or not mac:
        return False
    expected = hmac.new(secret.encode("utf-8"), stamp.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    return int(stamp) > time.time()


# ============================================================ 客户端 IP
#
# 默认**完全忽略** X-Forwarded-For / X-Real-IP。
# 理由：直接暴露时这两个头由客户端随便写，信它等于让任何人伪造 IP 绕过限流。
# 将来接入 nginx 时，在 .env 里写 TRUSTED_PROXIES=127.0.0.1，并让 nginx 用
# `proxy_set_header X-Forwarded-For $remote_addr`（覆盖而不是追加），才启用。

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _trusted_proxies() -> frozenset[str]:
    raw = str(getattr(settings, "trusted_proxies", "") or "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip()) | _LOOPBACK


def client_ip(scope: dict[str, Any], headers: list[tuple[bytes, bytes]]) -> str:
    """取真实客户端 IP。

    - 默认：用 TCP 连接的对端地址（scope["client"]），代理头一律不看
    - 配了 TRUSTED_PROXIES 且对端在名单里：取 X-Forwarded-For **最后一个**值
      （最后一个才是最近一跳代理看到的真实来源；取第一个会被客户端自带的值骗）
    """
    peer = ""
    client = scope.get("client")
    if client:
        peer = str(client[0])
    if not peer:
        return "unknown"

    trusted = _trusted_proxies()
    if peer not in trusted:
        return peer

    forwarded = ""
    for name, value in headers:
        if name.lower() == b"x-forwarded-for":
            forwarded = value.decode("latin-1")
    if not forwarded:
        return peer
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    return parts[-1] if parts else peer


# ============================================================ 令牌桶限流
#
# 分档依据是实测数据（见 docs/SECURITY-PHASE1-PLAN.md 第 4.1 节）：
#   /graph/survey        c=1 626ms → c=16 10144ms   完全串行，CPU 密集
#   /chat                单次 2.3–7.6s，瓶颈在上游网络（省钱比省 CPU 重要）
#   /settings/index/health 单次 4 秒，已被 ADMIN 层挡住，这里再兜一道


@dataclass(frozen=True)
class Bucket:
    rate: float   # 每秒补充的令牌数
    burst: int    # 桶容量（允许的瞬时并发）


# 档位 A 轻量只读：c=8 时 310ms，2 req/s 远低于饱和点
BUCKET_LIGHT = Bucket(rate=2.0, burst=10)
# 档位 B LLM 对话：单次 2.3–7.6s，6 次/分钟已经打满
BUCKET_CHAT = Bucket(rate=0.1, burst=3)
# 档位 C LLM 其他：单次 1.2–5.1s
BUCKET_LLM = Bucket(rate=0.2, burst=3)
# 档位 D 重量级：单次 8–180s，还会外网下载 + 入库
BUCKET_HEAVY = Bucket(rate=0.02, burst=2)
# 档位 E CPU 密集：并发完全不扩展
BUCKET_CPU = Bucket(rate=0.05, burst=2)

# 路径 → 档位。路径模板带 {xxx} 的形式与 main.py 的路由模板完全一致，
# 匹配时先精确查，查不到再按前缀（用于 /paper/{filename}/xxx 这类）。
_RATE_TABLE: dict[str, Bucket] = {
    # ---- A 轻量只读 ----
    "/favicon.ico": BUCKET_LIGHT,
    "/health": BUCKET_LIGHT,
    "/papers": BUCKET_LIGHT,
    "/papers/files": BUCKET_LIGHT,
    "/papers/topics": BUCKET_LIGHT,
    "/reader/papers": BUCKET_LIGHT,
    "/recommendations": BUCKET_LIGHT,
    "/graph": BUCKET_LIGHT,
    "/writing/renderers": BUCKET_LIGHT,
    # ---- B LLM 对话（最贵的两项） ----
    "/chat": BUCKET_CHAT,
    "/chat/stream": BUCKET_CHAT,
    # ---- C LLM 其他 ----
    "/translate/selection": BUCKET_LLM,
    "/writing/framework": BUCKET_LLM,
    "/writing/diagram": BUCKET_LLM,
    "/writing/zh2en": BUCKET_LLM,
    "/writing/chart": BUCKET_LLM,
    "/writing/paper-figures": BUCKET_LLM,
    "/paper/{filename}/translate": BUCKET_LLM,
    # ---- D 重量级（外网 + 入库） ----
    "/upload": BUCKET_HEAVY,
    "/papers/upload": BUCKET_HEAVY,
    "/library/download": BUCKET_HEAVY,
    "/paper/{filename}/references/download": BUCKET_HEAVY,
    "/recommendations/refresh": BUCKET_HEAVY,
    "/recommendations/{key}/feedback": BUCKET_LIGHT,
    "/recommendations/schedule": BUCKET_LIGHT,
    # ---- E CPU 密集 ----
    "/graph/survey": BUCKET_CPU,
    "/graph/frontier": BUCKET_CPU,
    "/paper/{filename}/layout": BUCKET_CPU,
    "/paper/{filename}/figure/{index}": BUCKET_LIGHT,
    "/paper/{filename}/structure": BUCKET_LIGHT,
    "/paper/{filename}/translation": BUCKET_LIGHT,
    "/paper/{filename}": BUCKET_LIGHT,
    "/writing/chart/{name}": BUCKET_LIGHT,
    "/writing/diagram/{name}": BUCKET_LIGHT,
    # 管理接口：管理员也是人，给宽松档，避免自己连点时被自己挡住
    "/settings": BUCKET_LIGHT,
    "/settings/about": BUCKET_LIGHT,
    "/settings/restore": BUCKET_LIGHT,
    "/settings/schedule": BUCKET_LIGHT,
    "/settings/test-connection": BUCKET_LLM,
    # 文档端点：dev 下存在。虽然只是静态渲染，但生成 openapi 要遍历全部路由
    "/openapi.json": BUCKET_LIGHT,
    "/docs": BUCKET_LIGHT,
    "/redoc": BUCKET_LIGHT,
    "/settings/index/health": BUCKET_CPU,
    "/settings/index/cleanup": BUCKET_CPU,
    "/papers/{paper_id}": BUCKET_LIGHT,
    "/admin/login": Bucket(rate=0.05, burst=5),
    "/admin/logout": BUCKET_LIGHT,
}

# 长度大于它们的路径前缀一律按最严格档算，防止有人用超长路径刷。
DEFAULT_BUCKET = BUCKET_CPU


def bucket_for(template: str) -> Bucket:
    """按路由模板取限流档位。未登记的模板按最严档处理（失败即拒绝）。"""
    return _RATE_TABLE.get(template, DEFAULT_BUCKET)


@dataclass
class _BucketState:
    tokens: float
    stamp: float


class RateLimiter:
    """进程内令牌桶。**重启即清空，多 worker 不共享**——这是已知边界。

    为什么接受这个边界：比赛现场是单进程单机，重启窗口内被刷的次数
    上限就是「桶容量 × IP 数」，代价可算且很小。而引入 Redis 会带来
    一个新的外部依赖和新的故障点，得不偿失。这个限制写在
    docs/SECURITY-PHASE1-PLAN.md 的风险清单里，并在 /settings 的管理面板可见。
    """

    def __init__(self, idle_seconds: float = 3600.0, max_keys: int = 20000) -> None:
        self._states: dict[str, _BucketState] = {}
        self._lock = threading.Lock()
        self._idle = idle_seconds
        self._max_keys = max_keys
        self._last_sweep = time.monotonic()

    def allow(self, key: str, bucket: Bucket) -> tuple[bool, int]:
        """取一个令牌。返回 (是否放行, 建议等待秒数)。"""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self._max_keys:
                    # 内存自保：键盘超过上限时按最老的清，绝不无限增长
                    self._sweep(now, force=True)
                state = _BucketState(tokens=float(bucket.burst), stamp=now)
                self._states[key] = state

            elapsed = now - state.stamp
            if elapsed > 0:
                state.tokens = min(float(bucket.burst), state.tokens + elapsed * bucket.rate)
                state.stamp = now

            if state.tokens >= 1.0:
                state.tokens -= 1.0
                return True, 0
            need = (1.0 - state.tokens) / bucket.rate if bucket.rate > 0 else 60.0
            return False, max(1, int(need + 0.999))

    def _sweep(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._last_sweep < 60:
            return
        self._last_sweep = now
        cutoff = now - self._idle
        dead = [k for k, v in self._states.items() if v.stamp < cutoff]
        for k in dead:
            self._states.pop(k, None)
        if force and len(self._states) >= self._max_keys:
            ordered = sorted(self._states.items(), key=lambda kv: kv[1].stamp)
            for k, _ in ordered[: len(ordered) // 2]:
                self._states.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


limiter = RateLimiter()


# ============================================================ 登录失败计数


class LoginGuard:
    """同一 IP 连续登录失败 N 次后锁定 M 秒。"""

    def __init__(self) -> None:
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked(self, ip: str) -> int:
        """返回剩余锁定秒数，0 表示未锁定。"""
        with self._lock:
            item = self._fails.get(ip)
            if item is None:
                return 0
            count, since = item
            if count < LOGIN_MAX_FAILS:
                return 0
            left = int(since + LOGIN_LOCK_SECONDS - time.time())
            if left <= 0:
                self._fails.pop(ip, None)
                return 0
            return left

    def fail(self, ip: str) -> int:
        """记一次失败，返回当前剩余锁定秒数（0 = 还没触发锁定）。"""
        with self._lock:
            now = time.time()
            count, since = self._fails.get(ip, (0, now))
            count += 1
            # `since` 只在第一次失败时记录，作为锁定计时起点；
            # 第 1→2 次时若重写 since，锁定窗口会一直被往后推。
            self._fails[ip] = (count, since)
            if count < LOGIN_MAX_FAILS:
                return 0
            # 刚跨过阈值：锁定从现在开始算，返回满额秒数。
            # 原来是 `int(since + LOCK - now)`，第 5 次失败时 since≈now，
            # 算出来≈LOCK 看似没问题，但 `or LOGIN_LOCK_SECONDS` 的兜底会掩盖
            # 「刚好约等于 0」的边界；直接返回常量更清楚。
            return LOGIN_LOCK_SECONDS

    def succeed(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


login_guard = LoginGuard()



# ============================================================ LLM 调用预算
#
# 统计口径（用户 m15060 明确要求）：
#   1. 计的是**真实模型 API 调用次数**，不是 HTTP 请求次数。
#      Agent 一次 /chat 最多 5 轮工具循环 + 1 次查询翻译 = 至多 6 次调用。
#   2. 每次调用前先在预算里记一笔，调不通也不退——宁可少算额度，
#      不能因为失败重试把账算漏（上游已经可能计费）。
#   3. 必须持久化 + 并发安全：内存计数在重启后归零，等于给刷额度留了后门。


class DailyBudget:
    """按自然日统计的模型调用预算，落盘 JSON，进程重启不丢。"""

    def __init__(self, path_getter: Callable[[], Path], limit_getter: Callable[[], int]) -> None:
        self._path_getter = path_getter
        self._limit_getter = limit_getter
        self._lock = threading.Lock()
        self._day = ""
        self._count = 0
        self._loaded = False

    # ---- 持久化 ----

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._path_getter()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._day = str(raw.get("day") or "")
            self._count = int(raw.get("count") or 0)
        except (OSError, ValueError, TypeError):
            self._day, self._count = "", 0

    def _save_locked(self) -> None:
        path = self._path_getter()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"day": self._day, "count": self._count}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            # 落盘失败不能让主流程炸掉：宁可这次统计只存在内存里
            logger.warning("LLM 预算落盘失败（本次仅内存计数）：%s", path, exc_info=True)

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    # ---- 状态 ----

    def _refresh_locked(self) -> None:
        today = self._today()
        if self._day != today:
            self._day = today
            self._count = 0

    def used(self) -> int:
        with self._lock:
            self._load_locked()
            self._refresh_locked()
            return self._count

    def limit(self) -> int:
        return self._limit_getter()

    def remaining(self) -> int:
        return max(0, self.limit() - self.used())

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def status(self) -> dict[str, Any]:
        used = self.used()
        limit = self.limit()
        return {
            "day": self._day or self._today(),
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "persisted_to": str(self._path_getter()),
            "note": "预算重启后仍保留、并发安全；超限返回 429",
        }

    # ---- 计数 ----

    def consume(self, n: int = 1) -> bool:
        """记 n 次模型调用。返回 False 表示已超预算（调用方应拒绝）。"""
        with self._lock:
            self._load_locked()
            self._refresh_locked()
            limit = self.limit()
            if self._count + n > limit:
                return False
            self._count += n
            self._save_locked()
            return True

    def reset(self) -> None:
        with self._lock:
            self._load_locked()
            self._day = self._today()
            self._count = 0
            self._save_locked()


daily_budget = DailyBudget(
    path_getter=lambda: Path(str(getattr(settings, "budget_file", "") or "data/meta/llm-budget.json")),
    limit_getter=lambda: int(getattr(settings, "llm_daily_budget", 800) or 800),
)


class BudgetExceeded(RuntimeError):
    """当日模型调用预算已用完。调用方应转成 429 而不是 500。"""


def reserve_llm_call(n: int = 1) -> None:
    """每次真实调用模型前调一次。超预算抛 BudgetExceeded。

    为什么在**调用前**扣：扣在调用后的话，并发请求会同时通过检查
    再各自调用，最后一起把账单冲爆。先扣后调，超额的那批直接被拒。
    """
    if not daily_budget.consume(n):
        raise BudgetExceeded("今日演示额度已用完，请明天再试")


# ============================================================ 请求体上限
#
# 只靠 Content-Length 是不够的：分块传输（Transfer-Encoding: chunked）
# 根本不带这个头，攻击者可以一边发一边读，把内存吃满。
# 解决办法是包一层 receive()，**边读边累加实际字节数**，超了立刻返回 413。

MAX_JSON_BODY = 1024 * 1024        # 1MB：正常 /chat 请求体 < 4KB，留 250 倍余量
MAX_UPLOAD_BODY = 24 * 1024 * 1024  # 24MB：单文件业务上限 20MB + multipart 边界余量


def _is_upload(path: str) -> bool:
    return path in ("/upload", "/papers/upload", "/writing/chart")


def body_limit_for(path: str) -> int:
    return MAX_UPLOAD_BODY if _is_upload(path) else MAX_JSON_BODY


class ClientDisconnected(RuntimeError):
    """客户端在传输中途断开。"""


def _safe_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


# ============================================================ 工具函数


def extract_path(scope: dict[str, Any]) -> str:
    """从 ASGI scope 取请求路径（不含 query string）。"""
    return str(scope.get("path") or "/")


def _headers(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    return list(scope.get("headers") or [])


def cookie_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """从 Cookie 头里取某个键的值。不引第三方库。"""
    raw = ""
    for key, value in headers:
        if key.lower() == b"cookie":
            raw = value.decode("latin-1")
            break
    if not raw:
        return None
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def origin_allowed(scope: dict[str, Any]) -> bool:
    """CSRF 第二道闸：管理写接口的 Origin/Referer host 必须等于请求 host。

    Cookie 已经是 SameSite=Strict，跨站根本带不过来；这一层是纵深防御，
    也顺便挡住某些老浏览器对 SameSite 支持不全的情况。
    两个头都不带时放行——命令行 curl 不带 Origin，硬拒绝会让运维没法用。
    """
    headers = _headers(scope)
    host = _safe_header(headers, b"host").split(":")[0].strip().lower()
    if not host:
        return True

    for name in (b"origin", b"referer"):
        raw = _safe_header(headers, name).strip()
        if not raw:
            continue
        # http://host:port/path → host
        rest = raw.split("://", 1)[-1]
        candidate = rest.split("/", 1)[0].split(":", 1)[0].strip().lower()
        if candidate and candidate != host:
            return False
    return True


def redact(text: str) -> str:
    """把出现在文本里的管理员凭证打码（日志脱敏用）。"""
    token = _admin_token()
    if token and token in text:
        text = text.replace(token, "***")
    return text


__all__ = [
    "ADMIN_PATHS",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "DOC_PATHS",
    "BUCKET_CHAT",
    "BUCKET_CPU",
    "BUCKET_HEAVY",
    "BUCKET_LIGHT",
    "BUCKET_LLM",
    "COOKIE_NAME",
    "BudgetExceeded",
    "ClientDisconnected",
    "DailyBudget",
    "LoginGuard",
    "RateLimiter",
    "SESSION_TTL",
    "admin_configured",
    "body_limit_for",
    "bucket_for",
    "client_ip",
    "cookie_value",
    "daily_budget",
    "extract_path",
    "limiter",
    "login_guard",
    "normalize_path",
    "origin_allowed",
    "redact",
    "reserve_llm_call",
    "sign_session",
    "verify_password",
    "verify_session",
]