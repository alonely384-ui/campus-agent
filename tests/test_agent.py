"""Agent 的参数解析测试。

为什么重点测 _parse_arguments？
    因为整个 Agent 最脆弱的环节就是"模型给的参数格式不对"。
    这一处崩了，用户看到的就是"服务器错误"，而不是"我换个说法再问"。
    **测试要优先覆盖最容易出问题的地方，而不是最容易写的地方。**
"""

import pytest

from app.agent import _parse_arguments
from app.tools.registry import get_tool, list_tools, tool_schemas


# ---------------- 参数解析 ----------------
def test_normal_json():
    assert _parse_arguments('{"expression": "1+1"}') == {"expression": "1+1"}


def test_empty_string_returns_empty_dict():
    """有的模型会返回空字符串，不能因此崩溃。"""
    assert _parse_arguments("") == {}
    assert _parse_arguments("   ") == {}


def test_strips_markdown_code_fence():
    """真实高频问题：模型爱把 JSON 包在 ```json ... ``` 里。"""
    raw = '```json\n{"expression": "23*47"}\n```'
    assert _parse_arguments(raw) == {"expression": "23*47"}


def test_strips_fence_without_language_tag():
    raw = '```\n{"expression": "1+1"}\n```'
    assert _parse_arguments(raw) == {"expression": "1+1"}


def test_ignores_trailing_explanation():
    """模型有时会在 JSON 后面补一句解释。"""
    raw = '{"expression": "1+1"}\n\n这个表达式很简单。'
    assert _parse_arguments(raw) == {"expression": "1+1"}


def test_invalid_json_returns_error_key():
    """解析失败时返回 _error，而不是抛异常 —— 让模型有机会自我纠正。"""
    result = _parse_arguments("{expression: 1+1}")
    assert "_error" in result


def test_non_object_json_returns_error_key():
    result = _parse_arguments("[1, 2, 3]")
    assert "_error" in result


# ---------------- 工具注册表 ----------------
def test_calculator_is_registered():
    tool = get_tool("calculator")
    assert tool is not None
    assert tool.name == "calculator"


def test_unknown_tool_returns_none():
    """模型可能幻觉出不存在的工具名，查找必须安全返回 None。"""
    assert get_tool("nonexistent_tool_xyz") is None


def test_tool_schema_shape():
    """发给模型的结构必须符合 OpenAI 的函数调用协议，否则模型无法识别。"""
    schemas = tool_schemas()
    assert len(schemas) == len(list_tools())
    for s in schemas:
        assert s["type"] == "function"
        assert "name" in s["function"]
        assert "description" in s["function"]
        assert s["function"]["parameters"]["type"] == "object"


def test_every_tool_has_description():
    """description 是模型判断"该不该用这个工具"的唯一依据，不能为空。"""
    for tool in list_tools():
        assert tool.name, "工具必须有名字"
        assert len(tool.description) > 10, f"{tool.name} 的 description 太短，模型看不懂用途"
