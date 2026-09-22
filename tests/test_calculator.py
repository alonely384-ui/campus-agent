"""计算器工具的测试。

为什么第一个测试写工具而不是写 Agent？
    因为 Agent 依赖网络和模型（不稳定、要花钱），
    而工具是纯函数（稳定、免费、秒出结果）。
    **能测纯逻辑就先测纯逻辑** —— 这是工程化的第一步。
"""

import pytest

from app.tools.calculator import CalculatorTool


@pytest.fixture
def calc() -> CalculatorTool:
    return CalculatorTool()


@pytest.mark.asyncio
async def test_basic_arithmetic(calc):
    assert await calc.run(expression="23*47") == "23*47 = 1081"
    assert await calc.run(expression="128+72") == "128+72 = 200"


@pytest.mark.asyncio
async def test_operator_precedence(calc):
    """先乘除后加减，这是最基本的正确性。"""
    assert "600" in await calc.run(expression="2+4*150-2")


@pytest.mark.asyncio
async def test_power_and_mod(calc):
    assert "1024" in await calc.run(expression="2**10")
    assert "1" in await calc.run(expression="10%3")


@pytest.mark.asyncio
async def test_negative_and_decimal(calc):
    assert "-5" in await calc.run(expression="-5+0")
    assert "2.5" in await calc.run(expression="5/2")


@pytest.mark.asyncio
async def test_missing_argument(calc):
    """参数没传时不能崩，要返回可读的错误。"""
    result = await calc.run()
    assert "错误" in result


@pytest.mark.asyncio
async def test_rejects_malicious_expression(calc):
    """安全测试：绝不能执行任意代码。

    如果哪天有人把 _safe_eval 改回 eval()，这个测试会立刻失败 ——
    这就是"测试当护栏"的意义。
    """
    result = await calc.run(expression="__import__('os').system('echo hacked')")
    assert "错误" in result or "不支持" in result
    assert "hacked" not in result


@pytest.mark.asyncio
async def test_division_by_zero_is_handled(calc):
    """除零不能把服务搞崩。"""
    result = await calc.run(expression="1/0")
    assert "出错" in result
