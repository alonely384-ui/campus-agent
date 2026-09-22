"""工具注册表：把所有工具集中登记，方便按名字查找。

加新工具只需要两步：
    1. 在 app/tools/ 下新建一个文件，继承 BaseTool
    2. 在下面的 _ALL_TOOLS 里加一行
"""

from app.tools.base import BaseTool
from app.tools.calculator import CalculatorTool

# 所有可用的工具
_ALL_TOOLS: list[BaseTool] = [
    CalculatorTool(),
    # 第 4 周在这里继续加：
    # ScheduleTool(),   # 查课表
    # NoteTool(),       # 生成笔记
]

# 名字 -> 工具对象，方便 O(1) 查找
_TOOL_MAP: dict[str, BaseTool] = {t.name: t for t in _ALL_TOOLS}


def list_tools() -> list[BaseTool]:
    """拿到所有工具。"""
    return _ALL_TOOLS


def tool_schemas() -> list[dict]:
    """拿到所有工具的"说明书"，用于塞进给大模型的请求里。"""
    return [t.to_openai_schema() for t in _ALL_TOOLS]


def get_tool(name: str) -> BaseTool | None:
    """按名字找工具，找不到返回 None。"""
    return _TOOL_MAP.get(name)
