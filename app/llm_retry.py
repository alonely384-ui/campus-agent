"""LLM 调用的错误分类与重试策略。

**为什么不用 SDK 自带的 max_retries**：openai SDK 的 `_should_retry` 只看状态码
（408 / 409 / 429 / ≥500 重试，其余 4xx 不重试），但对调用方来说这五类错误的
处置方式完全不同：

    可重试类   503 服务繁忙 / 502 网关抖动 / 连接被重置  → 该重试，重试往往就好了
    认证类     401 / 403                                → 重试是白费，key 错了就是错了
    请求参数   400 / 422                                → 重试是白费，同一个请求重发结果一样
    限流       429                                      → 该重试，但必须尊重 Retry-After
    超时       APITimeoutError                          → 该重试，但要限制总时长

前几轮线上表现是：上游 503 时 SDK 悄悄重试两次，用户端只看到「转圈 20 秒然后
报错」，既不知道发生了什么、也没法判断该不该重试。现在改成由我们自己分类，
每类给出确定的次数、退避间隔和一句人话，并在日志里留下分类与尝试次数。

**与预算的关系（很重要）**：`reserve_llm_call()` 只在**首次尝试前**扣一次，
重试不重复扣。重试是「同一个逻辑请求」的内部细节，如果每次重试都扣预算，
一次 503 抖动就能吃掉用户 3 次额度，账面上的次数就不再对应「用户问了几次」。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import openai

from app.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------- 分类常量

#: 一个逻辑调用最多尝试几次（含首次）。3 次 = 首次 + 2 次重试。
MAX_ATTEMPTS = 3

#: 退避基数：第 n 次重试等 `BASE_DELAY * 2**(n-1)` 秒。
#: 0.8s、1.6s —— 比 SDK 默认的 0.5s 略长，因为上游 503 通常不是几百毫秒能恢复的。
BASE_DELAY = 0.8

#: 单次退避上限。上游要求等更久时我们宁可放弃也不干等。
MAX_DELAY = 8.0

#: Retry-After 超过这个秒数就放弃重试，直接把错误抛给用户。
#: 让人等 30 秒不如立刻告诉他「上游限流了，请稍后再试」。
MAX_RETRY_AFTER = 20.0

# 五类错误的稳定标识，前端/日志/测试都按它判断，不要用中文文案做判断依据
KIND_RETRYABLE = "retryable"    # 服务端临时故障，重试
KIND_RATE_LIMIT = "rate_limit"  # 限流，按 Retry-After 重试
KIND_TIMEOUT = "timeout"        # 超时，退避后重试
KIND_AUTH = "auth"              # 认证/权限，不重试
KIND_BAD_REQUEST = "bad_request"  # 请求本身有问题，不重试


@dataclass(frozen=True)
class Failure:
    """一次失败的分类结果。"""

    kind: str
    #: 给用户看的一句话。**不要塞技术栈、不要塞请求体**，它会被直接显示在聊天框里。
    message: str
    retryable: bool
    #: 上游通过 Retry-After 指定的等待秒数，没有则为 None
    retry_after: float | None = None


def _retry_after_seconds(exc: BaseException) -> float | None:
    """从异常里抠 Retry-After。SDK 把它存在 response.headers 里。"""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def classify(exc: BaseException) -> Failure:
    """把 SDK 抛出的异常归到五类之一。

    顺序有讲究：`APITimeoutError` 是 `APIConnectionError` 的子类，必须先判超时，
    否则超时会被当成普通连接错误（文案会变成「网络连接失败」，误导用户去查网络）。
    """
    # ---- 限流：单独一类，因为它的等待时间由上游说了算
    if isinstance(exc, openai.RateLimitError):
        wait = _retry_after_seconds(exc)
        if wait is not None and wait > MAX_RETRY_AFTER:
            return Failure(
                kind=KIND_RATE_LIMIT,
                message=f"模型服务正在限流，需要等待 {wait:.0f} 秒，已停止重试，请稍后再问。",
                retryable=False,
                retry_after=wait,
            )
        return Failure(
            kind=KIND_RATE_LIMIT,
            message="模型服务正在限流，稍等片刻会自动重试。",
            retryable=True,
            retry_after=wait,
        )

    # ---- 认证类：key 错了，重试没有意义
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return Failure(
            kind=KIND_AUTH,
            message="API Key 无效或没有权限，请到设置中心检查 Key 是否填对。",
            retryable=False,
        )

    # ---- 请求参数：同一个请求重发结果一样
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return Failure(
            kind=KIND_BAD_REQUEST,
            message="请求参数被模型服务拒绝，请到设置中心检查模型名是否可用。",
            retryable=False,
        )

    # ---- 模型不存在：也算请求参数问题，但文案要更具体
    if isinstance(exc, openai.NotFoundError):
        return Failure(
            kind=KIND_BAD_REQUEST,
            message="模型不存在，请到设置中心确认模型名与所选服务商是否匹配。",
            retryable=False,
        )

    # ---- 超时：必须是 APIConnectionError 的判断之前
    if isinstance(exc, openai.APITimeoutError):
        return Failure(
            kind=KIND_TIMEOUT,
            message="模型响应超时，正在重试。",
            retryable=True,
        )

    # ---- 连接层错误：断网、DNS、连接被重置
    if isinstance(exc, openai.APIConnectionError):
        return Failure(
            kind=KIND_RETRYABLE,
            message="连接模型服务失败，正在重试。",
            retryable=True,
        )

    # ---- 5xx / 408 / 409：服务端临时故障
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500 or status in (408, 409):
            return Failure(
                kind=KIND_RETRYABLE,
                message=f"模型服务暂时不可用（{status}），正在重试。",
                retryable=True,
            )
        # 其余的 4xx（包括将来 SDK 新增的状态码）一律不重试
        return Failure(
            kind=KIND_BAD_REQUEST,
            message=f"模型服务返回错误（{status}），请稍后再试或到设置中心检查配置。",
            retryable=False,
        )

    # ---- 兜底：没见过的异常不重试，因为不知道重试会不会有副作用
    return Failure(
        kind=KIND_BAD_REQUEST,
        message="调用模型时发生未知错误，请稍后再试。",
        retryable=False,
    )


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """第 `attempt` 次重试（从 1 开始）应该等多久。

    有 Retry-After 时以它为准，但夹在 [0, MAX_DELAY] 内；
    否则按指数退避，并加 ±25% 抖动——同一时刻失败的多个请求错开重试，
    避免它们整齐地在同一毫秒撞回上游，把一次抖动放大成雪崩。
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, MAX_DELAY))
    base = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return base * (1 - 0.25 * random.random())


@dataclass
class RetryLog:
    """一次逻辑调用里发生过的重试，用于日志与测试断言。"""

    attempts: int = 0
    kinds: list[str] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)

    def record(self, kind: str, delay: float) -> None:
        self.kinds.append(kind)
        self.delays.append(delay)


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    what: str = "LLM 调用",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """执行 ``operation``，按五类错误策略重试。

    ``sleep`` 可注入，测试里换成假实现就不用真等（否则一条 429 用例要跑 5 秒）。
    """
    log = RetryLog()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log.attempts = attempt
        try:
            return await operation()
        except BaseException as exc:  # noqa: BLE001 —— 分类器要看到全部异常
            # CancelledError / KeyboardInterrupt 不该被当成可重试错误吞掉
            if not isinstance(exc, Exception):
                raise
            failure = classify(exc)
            last = attempt >= MAX_ATTEMPTS
            if not failure.retryable or last:
                logger.warning(
                    "%s 失败（第 %d/%d 次，分类=%s）：%s。已重试的等待序列 %s",
                    what,
                    attempt,
                    MAX_ATTEMPTS,
                    failure.kind,
                    failure.message,
                    [round(d, 2) for d in log.delays] or "无",
                )
                if failure.kind == KIND_TIMEOUT:
                    # 超时是唯一允许上层区分处理的一类：agent 里会把超时
                    # 转成「资料检索超时，本次回答可能不完整」而不是整轮失败
                    raise
                raise
            delay = backoff_seconds(attempt, failure.retry_after)
            log.record(failure.kind, delay)
            logger.info(
                "%s 第 %d 次失败（分类=%s），%.2f 秒后重试：%s",
                what,
                attempt,
                failure.kind,
                delay,
                failure.message,
            )
            await sleep(delay)

    # for 循环里必然 return 或 raise，这里只是让类型检查器满意
    raise RuntimeError("call_with_retry 逻辑错误：循环未返回也未抛出")


def user_message(exc: BaseException) -> str:
    """把异常转成可以直接显示给用户的一句话。

    用在所有「调用模型失败」的出口，保证用户看到的是中文人话，
    而不是 `openai.InternalServerError: Error code: 503`。
    """
    return classify(exc).message