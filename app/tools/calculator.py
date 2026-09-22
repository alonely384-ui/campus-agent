"""示例工具 1：计算器。

第一个工具故意做得极简单——目的是让你先把「工具调用」这条链路跑通，
而不是被业务逻辑绊住。跑通之后，照抄这个文件的形状再加课表、日程等工具。
"""

import ast
import operator

from app.tools.base import BaseTool

# 允许的运算符白名单。
# 安全提醒：绝对不要用 eval() 去算用户给的字符串！别人可以塞
# `__import__('os').system('rm -rf /')` 进来。所以这里用 AST 解析 + 白名单运算。
_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _safe_eval(node: ast.AST) -> float:
    """只允许数字和四则运算，其他一律拒绝。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("只支持数字")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持的一元运算符")
        return op(_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式: {type(node).__name__}")


class CalculatorTool(BaseTool):
    name = "calculator"
    description = (
        "计算数学表达式。当用户的问题涉及数字运算（加减乘除、乘方、取余）时调用。"
        "不要自己心算，一律调用这个工具，避免算错。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 '23*47+15' 或 '2**10'",
            }
        },
        "required": ["expression"],
    }

    async def run(self, expression: str = "", **kwargs) -> str:
        if not expression:
            return "错误：缺少 expression 参数"
        try:
            tree = ast.parse(expression, mode="eval")
            result = _safe_eval(tree)
            # 结果是 12.0 这种整数时，显示成 12 更好看
            if result == int(result):
                result = int(result)
            return f"{expression} = {result}"
        except Exception as e:
            return f"计算出错：{e}"
