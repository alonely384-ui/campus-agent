"""LLM 客户端：把"调用大模型"这件事封装成一个函数。

目标：其他代码只写 `await chat(messages)`，不需要知道用的哪家模型、
      也不需要知道 HTTP 请求长什么样。

⚠️ 注意这里用 lazy（懒加载）创建 client，而不是在文件顶部直接创建：
   如果写成 client = AsyncOpenAI(...) 放在 import 时执行，
   那么在 .env 还没被读取的时刻就已经把（空的）API Key 塞进去了，
   后面无论怎么改配置都不会生效。这是个非常常见的坑。
"""

from typing import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """第一次调用时才真正创建客户端，之后复用同一个。"""
    global _client
    if _client is None:
        if not settings.llm_api_key or settings.llm_api_key.startswith("sk-在这里"):
            # 提前给出人话提示，而不是等着报一个看不懂的 401
            raise RuntimeError(
                "还没有配置 API Key。请把 .env.example 复制成 .env，"
                "并在里面填入 LLM_API_KEY。"
            )
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        logger.info("LLM 客户端已创建: %s (%s)", settings.llm_base_url, settings.llm_model)
    return _client


async def chat(messages: list[dict], temperature: float = 0.3) -> str:
    """一次性拿完整回答。

    参数 messages 的形状（这是 OpenAI 定的标准格式，记住它）：
        [
            {"role": "system", "content": "你是一个校园助手"},
            {"role": "user",   "content": "明天有什么课"},
        ]
    """
    logger.info("请求模型 %s，消息数 %d", settings.llm_model, len(messages))

    resp = await get_client().chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=temperature,
    )
    answer = resp.choices[0].message.content or ""
    logger.info("模型返回 %d 个字符", len(answer))
    return answer


async def chat_stream(messages: list[dict], temperature: float = 0.3) -> AsyncIterator[str]:
    """流式拿回答：一个字一个字往外吐。

    这是聊天框"打字机效果"的原理。用 async for 消费：
        async for piece in chat_stream(messages):
            print(piece, end="")
    """
    logger.info("流式请求模型 %s", settings.llm_model)

    stream = await get_client().chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=temperature,
        stream=True,  # ← 关键就这一行
    )

    async for chunk in stream:
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            yield piece
