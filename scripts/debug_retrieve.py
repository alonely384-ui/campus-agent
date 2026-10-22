r"""把检索过程打印出来，判断"没检索到"还是"排错序"。

用法（在项目根目录）：
    .venv\Scripts\python.exe scripts\debug_retrieve.py
    .venv\Scripts\python.exe scripts\debug_retrieve.py --top-k 8
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 直接 python scripts/xxx.py 时，让 import app 能找到项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import _search_query
from app.rag.pipeline import retrieve_scored

# 5 个已暴露问题的题：expected 是人工翻 PDF 确认的答案页（None = 还没确认）
CASES: list[tuple[str, str | None, int | None]] = [
    ("论文训练用了多少块 GPU，什么型号？", "1706.03762v7.pdf", 7),
    ("Adam 优化器的 β1 和 β2 分别是多少？", "1706.03762v7.pdf", 7),
    ("BLEU 分数是多少？", "1706.03762v7.pdf", 8),
    ("这篇论文的第六作者是谁？", "1706.03762v7.pdf", 1),
    ("LayerNorm 对应哪条参考文献？", "1706.03762v7.pdf", 10),
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    explainable = 0
    for question, source, expect in CASES:
        query = await _search_query(question)
        rows = retrieve_scored(query, top_k=args.top_k, source=source)

        print("=" * 72)
        print(f"问题: {question}")
        print(f"检索词: {query}")
        if expect:
            print(f"期望页: {expect}")

        hit_at = []
        for i, (chunk, score, channels) in enumerate(rows, 1):
            snippet = " ".join(chunk.text.split())[:90]
            mark = " <== 期望页" if expect and chunk.page == expect else ""
            if mark:
                hit_at.append(i)
            print(
                f"  #{i}  p{chunk.page:<4} score={score:.5f}  "
                f"ch={'+'.join(channels)}{mark}"
            )
            print(f"      {snippet}")

        if expect:
            print(f"  -> 期望页在 top{args.top_k} 的位置: {hit_at or '未命中'}")
            explainable += 1 if hit_at else 0

    print("=" * 72)
    print(f"召回情况: {explainable}/{sum(1 for _, _, e in CASES if e)} 道题的正确页进入了 top{args.top_k}")


if __name__ == "__main__":
    asyncio.run(main())
