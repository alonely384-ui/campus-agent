"""LLM 客户端：把"调用大模型"这件事封装成一个函数。

目标：其他代码只写 `await chat(messages)`，不需要知道用的哪家模型、
      也不需要知道 HTTP 请求长什么样。

⚠️ 注意这里用 lazy（懒加载）创建 client，而不是在文件顶部直接创建：
   如果写成 client = AsyncOpenAI(...) 放在 import 时执行，
   那么在 .env 还没被读取的时刻就已经把（空的）API Key 塞进去了，
   后面无论怎么改配置都不会生效。这是个非常常见的坑。
"""

from collections.abc import AsyncIterator

import httpx
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
        # 显式给超时和重试。不传的话 SDK 默认 timeout=600s（10 分钟），
        # 上游卡住时请求会一直挂着，用户端只看到「转圈」，而这边占着一个
        # 工作线程和一条连接，几个并发就能把服务拖死。
        # connect 单独收紧到 10s：连不上这种事几秒内就该知道，不该等 60s。
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            max_retries=2,
        )
        logger.info("LLM 客户端已创建: %s (%s)", settings.llm_base_url, settings.llm_model)
    return _client


def reset_client() -> None:
    """丢掉已缓存的客户端，下一次调用按新的 key / base_url 重建。

    为什么需要：`get_client()` 只在 `_client is None` 那一刻读配置，
    建好之后就固定了。设置页改完 API Key 或 Base URL 不重建，
    请求还会打向旧地址，界面上就会出现"显示已保存、实际没生效"的假象。

    为什么要 close：AsyncOpenAI 内部持有 httpx 连接池，
    不关掉就丢掉引用，连接会一直挂到进程退出。close 是异步的，
    这里用 `client.close()` 的同步包装拿不到，所以只在事件循环里 await 调用
    （见 reset_client_async）；同步版本只做解引用，用于重启/测试场景。
    """
    global _client
    _client = None


async def reset_client_async() -> None:
    """异步版：关掉旧连接池再解引用，设置页保存后应当用这个。"""
    global _client
    old, _client = _client, None
    if old is None:
        return
    try:
        await old.close()
    except Exception:  # noqa: BLE001 —— 关连接失败不该阻断配置生效，但必须留痕
        logger.warning("关闭旧 LLM 客户端时出错（已忽略，不影响新配置生效）", exc_info=True)


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

    # try/finally 关流：外面这个生成器被提前丢弃时（用户关掉页面、点了停止、
    # 或者上层用 asyncio.wait_for 超时取消），async for 不会被走完，
    # 底层那条 HTTP 连接会一直挂在连接池里直到进程退出。
    # 几个用户反复刷新就能把连接池占满，之后所有请求都卡在建连阶段。
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content
            if piece:
                yield piece
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 —— 关连接失败不该影响已经吐给用户的内容
                logger.warning("关闭 LLM 流失败（已忽略）", exc_info=True)
