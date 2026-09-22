"""命令行入口：不启动网页也能测试 Agent。

用法：
    python -m app.cli "23*47 等于多少"
    python -m app.cli          ← 进入交互模式，随便聊
"""

import asyncio
import sys

from app.agent import run_agent
from app.logging_config import setup_logging


async def ask_once(question: str) -> None:
    """问一句，打印答案 + 工具调用轨迹。"""
    result = await run_agent(question)

    # 把 Agent 调用过哪些工具打出来 —— 排查问题时全靠这个
    for step in result.steps:
        print(f"  🔧 [第{step.round}轮] {step.tool_name}({step.arguments})")
        print(f"     ↳ {step.result}")

    print(f"\n🤖 {result.answer}\n")
    print(f"（共 {result.rounds} 轮）")


async def interactive() -> None:
    """交互模式：反复输入问题。输入 exit / q 退出。"""
    print("=" * 50)
    print("Campus Agent 命令行模式（输入 exit 退出）")
    print("=" * 50)

    history: list[dict] = []

    while True:
        try:
            question = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            print("再见！")
            break

        result = await run_agent(question, history)

        for step in result.steps:
            print(f"  🔧 [第{step.round}轮] {step.tool_name}({step.arguments})")
            print(f"     ↳ {step.result}")

        print(f"\n🤖 {result.answer}")

        # 记住对话，这样下一轮它能理解"那再算一下"这种指代
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": result.answer})


def main() -> None:
    setup_logging()

    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        asyncio.run(ask_once(question))
    else:
        asyncio.run(interactive())


if __name__ == "__main__":
    main()
