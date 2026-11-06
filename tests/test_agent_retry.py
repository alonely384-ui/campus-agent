"""Agent 主循环必须和 app.llm.chat() 用同一套错误处置。

背景：改造前 `run_agent` / `run_agent_stream` 直接 `await get_client().chat...create(...)`，
绕过了 `call_with_retry`。结果是同一个上游 503：

- 走 `llm.chat()` 的路径（翻译、写作、摘要）会重试两次再报错，往往就好了；
- 走 Agent 主循环的路径（就是用户真正在聊天框里问问题的那条）一次就失败。

两条路径对同一件事给出不同表现，用户看到的是「有时候能答、有时候莫名报错」。
这里锁住三条性质：

1. 主循环遇到可重试错误会重试；
2. 预算按逻辑请求扣一次，不随重试次数增加；
3. 流式只在「建立流」阶段重试，开始吐字之后不重来。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import agent

_REQ = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")


def _status_error(cls, code: int) -> Exception:
    return cls("error", response=httpx.Response(code, request=_REQ), body=None)


def _run(coro):
    return asyncio.run(coro)


def _reply(text: str) -> SimpleNamespace:
    """造一个「模型直接给答案、不调工具」的响应。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))]
    )


class _CreateStub:
    """按调用次序返回预设结果；元素是异常就抛，否则原样返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.n = 0

    def __call__(self, **_kwargs):
        self.n += 1
        item = self.script[min(self.n - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            return _raise(item)
        return _value(item)


async def _raise(exc):
    raise exc


async def _value(v):
    return v


def _patch(monkeypatch, create) -> dict:
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    # app/agent.py 里是 `from app.llm import get_client`，名字在 import 那一刻就
    # 绑进了 agent 的模块命名空间，所以必须 patch agent.get_client；
    # patch llm.get_client 不会影响这里（实测：仍会打真网络）。
    monkeypatch.setattr(agent, "get_client", lambda: client)
    charged = {"n": 0}
    monkeypatch.setattr(agent, "reserve_llm_call", lambda: charged.__setitem__("n", charged["n"] + 1))
    monkeypatch.setattr(agent, "tool_schemas", lambda: [])
    # 检索阶段不参与本测试，直接给空上下文，省掉整条 RAG 管道
    async def _no_context(*_a, **_kw):
        return "", []

    monkeypatch.setattr(agent, "_rag_context_with_evidence", _no_context)
    return charged


def test_agent_main_loop_retries_retryable_errors(monkeypatch):
    """一次 503 不该让整轮问答失败——这正是改造前会失败的那一类。"""
    create = _CreateStub([_status_error(openai.InternalServerError, 503), _reply("答案")])
    _patch(monkeypatch, create)

    result = _run(agent.run_agent("你好"))

    assert create.n == 2, "503 应该触发一次重试"
    assert result.answer == "答案"


def test_agent_main_loop_charges_budget_once(monkeypatch):
    """预算按「用户问了几次」记，重试不重复扣。"""
    create = _CreateStub(
        [
            _status_error(openai.InternalServerError, 503),
            _status_error(openai.InternalServerError, 502),
            _reply("答案"),
        ]
    )
    charged = _patch(monkeypatch, create)

    result = _run(agent.run_agent("你好"))

    assert create.n == 3
    assert charged["n"] == 1
    assert result.answer == "答案"


def test_agent_main_loop_does_not_retry_auth_errors(monkeypatch):
    """401 重试没有意义，必须一次失败就把原始异常抛出去。"""
    create = _CreateStub([_status_error(openai.AuthenticationError, 401)])
    _patch(monkeypatch, create)

    with pytest.raises(openai.AuthenticationError):
        _run(agent.run_agent("你好"))
    assert create.n == 1


def test_agent_stream_retries_before_first_token_only(monkeypatch):
    """建立流失败可以重试；已经开始吐字就不再重来（用户已经看到前半段）。"""

    class _Stream:
        def __init__(self) -> None:
            self.i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.i += 1
            if self.i == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="你", tool_calls=None))])
            raise StopAsyncIteration

    create = _CreateStub([_status_error(openai.InternalServerError, 503), _Stream()])
    _patch(monkeypatch, create)

    async def collect() -> list[dict]:
        return [e async for e in agent.run_agent_stream("你好")]

    events = _run(collect())

    assert create.n == 2, "建流失败应重试一次"
    deltas = [e["text"] for e in events if e.get("type") == "answer_delta"]
    assert deltas == ["你"], "重试成功后前半段只出现一次，不该重复"


def test_agent_uses_shared_retry_helper():
    """防止有人再把主循环改回裸 await——那样两条路径的行为会再次分叉。"""
    import inspect

    src = inspect.getsource(agent.run_agent)
    assert "call_with_retry" in src
    src_stream = inspect.getsource(agent.run_agent_stream)
    assert "call_with_retry" in src_stream
    assert "llm_retry" in agent.__dict__ or callable(agent.call_with_retry)