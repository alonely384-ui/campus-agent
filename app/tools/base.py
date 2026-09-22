"""工具基类：定义一个"工具"长什么样。

Agent 的本质就是：大模型自己不干活，它只会"决定调用哪个工具、传什么参数"，
真正干活的是你写的这些 Python 函数。

所以每个工具必须能回答三个问题：
    1. 你叫什么名字？（name）
    2. 你是干什么的？（description）← 模型靠这句话决定要不要用你，写得越清楚越好
    3. 你需要什么参数？（parameters）
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """所有工具都要继承这个类，并实现 name / description / parameters / run。"""

    #: 工具名，模型调用时用的标识，只能用小写字母和下划线
    name: str = ""

    #: 工具说明。这句话会原样送给大模型，所以要用中文写清楚"什么时候该用我"
    description: str = ""

    #: 参数的 JSON Schema 描述
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    async def run(self, **kwargs: Any) -> str:
        """真正执行工具的地方。返回值必须是字符串（模型只看得懂文本）。"""
        raise NotImplementedError

    def to_openai_schema(self) -> dict[str, Any]:
        """转成大模型能理解的"函数说明书"格式。

        这个格式是 OpenAI 定的，DeepSeek 等也都照抄，不用改。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
