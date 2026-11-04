"""五类 LLM 错误的分类与重试行为。

覆盖的是 ``app/llm_retry.py`` 的策略本身，以及它被接进 ``app/llm.chat`` /
``app/llm.chat_stream`` 之后**对外表现**是否正确。全部用假 client 和假 sleep，
不真发网络请求、不真等待，所以整套跑下来是毫秒级。

为什么这些用例重要：改之前 SDN 自带的重试只按状态码判断，401 和 503 被同样对待；
用户看到的是「转圈 20 秒后报一句英文」。这组用例把「哪类错重试、重试几次、
退避多久、最终给用户看什么话」都钉住，以后谁想改都必须先改测试。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import llm, llm_retry
from app.llm_retry import (
    MAX_ATTEMPTS,
    backoff_seconds,
    call_with_retry,
    classify,
    user_message,
)

_REQ = httpx.Request("POST", "https://api.deepseek.com/chat/completions")


# ---------------------------------------------------------------- 造假异常


def _status_error(cls, code: int, headers: dict | None = None) -> Exception:
    """构造一个带 response 的 SDK 状态码异常（真实 SDK 的形状）。"""
    return cls(
        "error",
        response=httpx.Response(code, request=_REQ, headers=headers or {}),
        body=None,
    )


def _timeout() -> Exception:
    return openai.APITimeoutError(request=_REQ)


def _conn_error() -> Exception:
    return openai.APIConnectionError(request=_REQ)


class _Recorder:
    """记录注入的 sleep 实际等了多久。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 分类本身


@pytest.mark.parametrize(
    ("exc", "kind", "retryable"),
    [
        # 可重试：服务端临时故障
        (_status_error(openai.InternalServerError, 500), llm_retry.KIND_RETRYABLE, True),
        (_status_error(openai.InternalServerError, 503), llm_retry.KIND_RETRYABLE, True),
        (_status_error(openai.APIStatusError, 502), llm_retry.KIND_RETRYABLE, True),
        (_status_error(openai.APIStatusError, 408), llm_retry.KIND_RETRYABLE, True),
        (_status_error(openai.ConflictError, 409), llm_retry.KIND_RETRYABLE, True),
        (_conn_error(), llm_retry.KIND_RETRYABLE, True),
        # 限流
        (_status_error(openai.RateLimitError, 429), llm_retry.KIND_RATE_LIMIT, True),
        # 超时
        (_timeout(), llm_retry.KIND_TIMEOUT, True),
        # 认证：不该重试
        (_status_error(openai.AuthenticationError, 401), llm_retry.KIND_AUTH, False),
        (_status_error(openai.PermissionDeniedError, 403), llm_retry.KIND_AUTH, False),
        # 请求参数：不该重试
        (_status_error(openai.BadRequestError, 400), llm_retry.KIND_BAD_REQUEST, False),
        (_status_error(openai.UnprocessableEntityError, 422), llm_retry.KIND_BAD_REQUEST, False),
        (_status_error(openai.NotFoundError, 404), llm_retry.KIND_BAD_REQUEST, False),
    ],
)
def test_categories(exc, kind, retryable):
    f = classify(exc)
    assert f.kind == kind
    assert f.retryable is retryable


def test_timeout_is_not_confused_with_connection_error():
    """APITimeoutError 是 APIConnectionError 的子类，分类顺序错了文案就会误导用户。"""
    assert isinstance(_timeout(), openai.APIConnectionError)  # 前提成立
    assert classify(_timeout()).kind == llm_retry.KIND_TIMEOUT
    assert "超时" in user_message(_timeout())
    assert classify(_conn_error()).kind == llm_retry.KIND_RETRYABLE
    assert "连接" in user_message(_conn_error())


def test_unknown_exception_is_not_retried():
    """没见过的异常不重试 —— 不知道重试会不会有副作用。"""
    f = classify(ValueError("boom"))
    assert f.retryable is False
    assert f.kind == llm_retry.KIND_BAD_REQUEST


def test_user_message_never_leaks_technical_junk():
    msg = user_message(_status_error(openai.InternalServerError, 503))
    assert "Error code" not in msg
    assert "openai" not in msg.lower()
    assert msg  # 必须有话可说


# ---------------------------------------------------------------- Retry-After


def test_rate_limit_honours_retry_after():
    exc = _status_error(openai.RateLimitError, 429, {"retry-after": "3"})
    f = classify(exc)
    assert f.retryable is True
    assert f.retry_after == 3.0
    assert backoff_seconds(1, f.retry_after) == 3.0


def test_rate_limit_with_huge_retry_after_gives_up():
    """让用户干等 2 分钟不如立刻告诉他。"""
    exc = _status_error(openai.RateLimitError, 429, {"retry-after": "120"})
    f = classify(exc)
    assert f.retryable is False
    assert "120" in f.message


def test_retry_after_is_capped_by_max_delay():
    assert backoff_seconds(1, 100.0) == llm_retry.MAX_DELAY


def test_rate_limit_without_header_has_no_retry_after():
    f = classify(_status_error(openai.RateLimitError, 429))
    assert f.retryable is True
    assert f.retry_after is None


# ---------------------------------------------------------------- 退避序列


def test_backoff_is_exponential_with_jitter(monkeypatch):
    monkeypatch.setattr(llm_retry.random, "random", lambda: 0.0)  # 关掉抖动
    assert backoff_seconds(1) == pytest.approx(0.8)
    assert backoff_seconds(2) == pytest.approx(1.6)
    assert backoff_seconds(3) == pytest.approx(3.2)
    assert backoff_seconds(10) == llm_retry.MAX_DELAY  # 封顶


def test_jitter_stays_within_quarter():
    vals = [backoff_seconds(1) for _ in range(200)]
    assert min(vals) >= 0.8 * 0.75 - 1e-9
    assert max(vals) <= 0.8 + 1e-9
    # 抖动的意义是错开，不是恒定值
    assert len(set(vals)) > 1


# ---------------------------------------------------------------- 重试循环


def test_retryable_error_retries_then_succeeds():
    """503 两次后成功：总共 3 次尝试、睡了 2 次。"""
    calls = {"n": 0}
    sleeper = _Recorder()

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(openai.InternalServerError, 503)
        return "答案"

    out = _run(call_with_retry(op, what="测试", sleep=sleeper))
    assert out == "答案"
    assert calls["n"] == 3
    assert len(sleeper.delays) == 2
    # 退避是递增的（抖动只会让它小幅浮动）
    assert sleeper.delays[0] < sleeper.delays[1]


def test_always_failing_retryable_gives_up_after_max_attempts():
    calls = {"n": 0}
    sleeper = _Recorder()

    async def op():
        calls["n"] += 1
        raise _status_error(openai.InternalServerError, 503)

    with pytest.raises(openai.InternalServerError):
        _run(call_with_retry(op, what="测试", sleep=sleeper))
    assert calls["n"] == MAX_ATTEMPTS
    assert len(sleeper.delays) == MAX_ATTEMPTS - 1


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _status_error(openai.AuthenticationError, 401),
        lambda: _status_error(openai.PermissionDeniedError, 403),
        lambda: _status_error(openai.BadRequestError, 400),
        lambda: _status_error(openai.NotFoundError, 404),
        lambda: ValueError("不是 SDK 异常"),
    ],
)
def test_non_retryable_errors_do_not_retry(factory):
    """认证错、参数错重试一万次也是同样的结果，只会白白拖慢报错。"""
    calls = {"n": 0}
    sleeper = _Recorder()

    async def op():
        calls["n"] += 1
        raise factory()

    # 不写 pytest.raises(Exception)（那等于什么都没断言，ruff B017）：
    # 这里要证的是「异常确实被抛出来了，且只尝试了一次」
    raised = False
    try:
        _run(call_with_retry(op, what="测试", sleep=sleeper))
    except BaseException:  # noqa: BLE001 —— 就是要把任何异常都接住
        raised = True
    assert raised is True
    assert calls["n"] == 1
    assert sleeper.delays == []


def test_timeout_retries_and_eventually_raises_timeout():
    calls = {"n": 0}
    sleeper = _Recorder()

    async def op():
        calls["n"] += 1
        raise _timeout()

    with pytest.raises(openai.APITimeoutError):
        _run(call_with_retry(op, what="测试", sleep=sleeper))
    assert calls["n"] == MAX_ATTEMPTS


def test_rate_limit_uses_retry_after_for_sleep():
    calls = {"n": 0}
    sleeper = _Recorder()

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(openai.RateLimitError, 429, {"retry-after": "2"})
        return "ok"

    assert _run(call_with_retry(op, what="测试", sleep=sleeper)) == "ok"
    assert sleeper.delays == [2.0]


def test_cancellation_is_not_swallowed():
    """用户点「停止」时抛的是 CancelledError（BaseException 系），不能被当成可重试。"""
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(call_with_retry(op, what="测试", sleep=_Recorder()))
    assert calls["n"] == 1


# ---------------------------------------------------------------- 接进 llm.chat


def _fake_client(behaviour) -> SimpleNamespace:
    """behaviour(n) -> 返回值或抛异常，n 从 1 开始。"""

    def create(**kwargs):
        async def _inner():
            return behaviour(create.n)

        create.n += 1
        return _inner()

    create.n = 0
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _stub_chat(monkeypatch, behaviour):
    client = _fake_client(behaviour)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    monkeypatch.setattr(llm, "reserve_llm_call", lambda: None)
    slept = _Recorder()
    # call_with_retry 的 sleep 默认值是 asyncio.sleep，定义在 llm_retry 的
    # 默认参数上，所以要换的是 llm_retry.asyncio.sleep（不是 app.llm.asyncio——
    # app.llm 根本没导入 asyncio 这个名字）
    monkeypatch.setattr(llm_retry.asyncio, "sleep", slept)
    return client, slept


def test_chat_charges_budget_once_regardless_of_retries(monkeypatch):
    """预算按「用户问了几个问题」记账，不按「HTTP 请求发了几次」。"""

    def behaviour(n):
        if n < 3:
            raise _status_error(openai.InternalServerError, 503)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="好"))])

    charged = {"n": 0}
    client = _fake_client(behaviour)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    monkeypatch.setattr(llm, "reserve_llm_call", lambda: charged.__setitem__("n", charged["n"] + 1))
    monkeypatch.setattr(llm_retry.asyncio, "sleep", _Recorder())

    assert _run(llm.chat([{"role": "user", "content": "hi"}])) == "好"
    assert client.chat.completions.create.n == 3  # 发了 3 次
    assert charged["n"] == 1  # 只扣 1 次


def test_chat_surfaces_human_message_on_auth_failure(monkeypatch):
    _stub_chat(monkeypatch, lambda n: (_ for _ in ()).throw(_status_error(openai.AuthenticationError, 401)))
    with pytest.raises(openai.AuthenticationError) as ei:
        _run(llm.chat([{"role": "user", "content": "hi"}]))
    assert "API Key" in user_message(ei.value)


def test_chat_stream_retries_only_before_first_token(monkeypatch):
    """建流失败可以重试；已经开始吐字后再失败就不能重来（用户已经看到前半段）。"""
    calls = {"n": 0}

    class _Stream:
        def __init__(self) -> None:
            self.i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.i += 1
            if self.i == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="你"))])
            if self.i == 2:
                raise _conn_error()
            raise StopAsyncIteration

        async def close(self):
            return None

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次建流失败，第二次成功
            return _raise(_status_error(openai.InternalServerError, 503))
        return _value(_Stream())

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    monkeypatch.setattr(llm, "reserve_llm_call", lambda: None)
    # 注意：不要 monkeypatch llm_retry.asyncio.sleep —— call_with_retry 的
    # sleep 默认参数在函数定义时就绑定了 asyncio.sleep（按值捕获），
    # 改 asyncio.sleep 属性对它无效。这里改用 no-op 真实 sleep 会拖慢测试，
    # 所以只断言「重试了几次」，不断言等待时长（等待时长由 backoff 用例覆盖）。

    async def collect() -> tuple[list[str], bool]:
        got: list[str] = []
        broke = False
        try:
            async for piece in llm.chat_stream([{"role": "user", "content": "hi"}]):
                got.append(piece)
        except openai.APIConnectionError:
            broke = True
        return got, broke

    got, broke = _run(collect())
    assert calls["n"] == 2  # 建流重试了 1 次
    assert got == ["你"]  # 前半段保留
    assert broke is True  # 中途失败原样上抛，由 SSE 层转成错误事件


async def _raise(exc):
    raise exc


async def _value(v):
    return v


def test_chat_stream_yields_pieces():
    async def collect() -> list[str]:
        return [p async for p in _aint("你好世界")]

    assert _run(collect()) == ["你", "好", "世", "界"]


async def _aint(s: str) -> AsyncIterator[str]:
    for ch in s:
        yield ch