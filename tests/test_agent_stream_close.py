"""Agent 上游 LLM 流的显式关闭（2026-11-18，审计第二轮缺陷一）。

修之前 `run_agent_stream` 里是这样的：

    stream = await call_with_retry(_open, ...)
    async for chunk in stream:      # ← 没有 try/finally
        ...

openai 的 `AsyncStream.__stream__` 内部确实自带 `finally: await response.aclose()`，
但那条 finally **只有在生成器被推进到结束、或被显式 aclose() 时才会执行**。
外层 async for 被 `CancelledError`（用户点停止 / 浏览器断开 / 上层取消）打断时
两者都不成立，底层 httpx 响应只能等 GC 释放——前端已经显示"已停止生成"，
上游可能还在吐 token 计费。

这里锁住四条性质：
1. 正常读完 → 关一次；
2. 取消 → 关，且取消语义继续向外传播；
3. 上游异常 → 关，且原始异常原样抛出；
4. 关闭本身抛错 → 不掩盖原始异常 / 取消信号。

夹具直接复刻 `test_agent_retry.py` 的做法：patch `agent.get_client`（模块里是
`from app.llm import get_client`，名字已绑进 agent 命名空间）+ 把检索短路掉。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import agent


def _chunk(text: str | None = None, tool_calls=None) -> SimpleNamespace:
    """形状像 openai chunk 的对象：代码只读 choices[0].delta。"""
    delta = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _StreamStub:
    """模拟 openai.AsyncStream 的可迭代表面 + aclose()/close()。

    script 里的元素：普通对象正常返回，BaseException 实例就地抛出。
    `closes` 记录被关了几次，`aclose_raises` 控制关的时候抛不抛错。
    """

    def __init__(self, script, *, aclose_raises: BaseException | None = None):
        self._script = list(script)
        self._i = 0
        self.closes = 0
        self.aclose_raises = aclose_raises

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._script):
            raise StopAsyncIteration
        item = self._script[self._i]
        self._i += 1
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closes += 1
        if self.aclose_raises is not None:
            raise self.aclose_raises

    async def close(self) -> None:  # openai 里 aclose 是 close 的别名，两个都提供
        await self.aclose()


def _patch(monkeypatch, stream: _StreamStub) -> None:
    async def _create(**_kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    monkeypatch.setattr(agent, "get_client", lambda: client)
    monkeypatch.setattr(agent, "reserve_llm_call", lambda: None)
    monkeypatch.setattr(agent, "tool_schemas", lambda: [])

    async def _no_context(*_a, **_kw):
        return "", []

    monkeypatch.setattr(agent, "_rag_context_with_evidence", _no_context)


async def _collect(agen) -> list[dict]:
    return [ev async for ev in agen]


# ------------------------------------------------------------------ 关闭器本身


async def test_closer_prefers_aclose_and_closes_once():
    stream = _StreamStub([])
    await agent._close_agent_stream(stream)
    assert stream.closes == 1


async def test_closer_falls_back_to_close():
    class _OnlyClose:
        def __init__(self) -> None:
            self.closes = 0

        async def close(self) -> None:
            self.closes += 1

    s = _OnlyClose()
    await agent._close_agent_stream(s)
    assert s.closes == 1


async def test_closer_is_safe_on_unclosable_object():
    """既没 aclose 也没 close 的对象不该报错（SDK 换实现时不至于炸）。"""
    await agent._close_agent_stream(object())


async def test_closer_swallows_ordinary_error():
    stream = _StreamStub([], aclose_raises=OSError("关不掉"))
    await agent._close_agent_stream(stream)  # 不抛
    assert stream.closes == 1


async def test_closer_does_not_swallow_cancellation():
    """关流时被取消：CancelledError 必须继续传播。"""
    stream = _StreamStub([], aclose_raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await agent._close_agent_stream(stream)


# ------------------------------------------------------------------ 四条生命周期路径


async def test_stream_closed_after_normal_completion(monkeypatch):
    """正常读完一轮定稿 → 上游流关闭。"""
    stream = _StreamStub([_chunk("答案")])
    _patch(monkeypatch, stream)

    events = await _collect(agent.run_agent_stream("你好"))

    assert any(e["type"] == "answer_delta" for e in events)
    assert any(e["type"] == "done" for e in events)
    assert stream.closes >= 1, "正常结束后必须显式关流"


async def test_stream_closed_when_cancelled(monkeypatch):
    """流到一半被取消 → 仍然要关，且取消继续抛给调用方。"""
    stream = _StreamStub([_chunk("半段"), asyncio.CancelledError()])
    _patch(monkeypatch, stream)

    with pytest.raises(asyncio.CancelledError):
        await _collect(agent.run_agent_stream("你好"))

    assert stream.closes >= 1, "取消路径也必须关流，否则前端停了后端还在烧 token"


async def test_stream_closed_by_consumer_aclose(monkeypatch):
    """消费者主动 aclose() 生成器（等价于前端断开、框架关流）→ 关上游流。"""
    stream = _StreamStub([_chunk("半段"), _chunk("又半段")])
    _patch(monkeypatch, stream)

    gen = agent.run_agent_stream("你好")
    await gen.__anext__()  # 至少拿到一条事件
    await gen.aclose()

    assert stream.closes >= 1


async def test_stream_closed_on_upstream_error(monkeypatch):
    """上游抛异常 → 关，且原始异常原样抛出（不被包装成别的类型）。"""
    stream = _StreamStub([_chunk("半段"), RuntimeError("上游炸了")])
    _patch(monkeypatch, stream)

    with pytest.raises(RuntimeError, match="上游炸了"):
        await _collect(agent.run_agent_stream("你好"))

    assert stream.closes >= 1


async def test_close_error_does_not_mask_upstream_error(monkeypatch):
    """上游异常 + 关流也异常 → 抛出的仍是上游那个异常。"""
    stream = _StreamStub([_chunk("半段"), RuntimeError("上游炸了")], aclose_raises=OSError("关不掉"))
    _patch(monkeypatch, stream)

    with pytest.raises(RuntimeError, match="上游炸了"):
        await _collect(agent.run_agent_stream("你好"))


async def test_close_error_does_not_mask_cancellation(monkeypatch):
    """取消 + 关流抛普通异常 → 仍是 CancelledError，不会变成 OSError。"""
    stream = _StreamStub([_chunk("半段")], aclose_raises=OSError("关不掉"))
    _patch(monkeypatch, stream)

    gen = agent.run_agent_stream("你好")
    await gen.__anext__()
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())


async def test_stream_closed_in_tool_call_round(monkeypatch):
    """有工具调用的那一轮：读完就关，不能带着旧流进入下一轮。"""
    tc = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="calculator", arguments='{"expression": "1+1"}'),
    )
    # 第一轮：要调工具；第二轮：直接给答案
    stream = _StreamStub([_chunk(None, tool_calls=[tc])])
    _patch(monkeypatch, stream)

    events = await _collect(agent.run_agent_stream("1+1 等于几"))

    assert any(e["type"] == "tool_call" for e in events)
    assert stream.closes >= 1, "工具调用分支退出时也要释放上游流"