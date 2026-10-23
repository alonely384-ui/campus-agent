"""重排 A/B 对照脚本：同一批题、同一口径，只切 RAG_RERANK 开关。

用法：
    python scripts/eval_rerank_ab.py                 # 默认 38 题多篇评测集
    python scripts/eval_rerank_ab.py --cases data/eval/qa_v7.json

指标说明：
    R@k = "答案所在页的块出现在第 k 名以内" 的题数占比。
    这里每题都指定了论文（source 过滤），模拟用户在下拉框里选定一篇后再提问。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.agent import _search_query  # noqa: E402
from app.rag import pipeline  # noqa: E402


def load_cases(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["items"]


def rank_of(hits, pages: list[int]) -> int | None:
    for i, (chunk, _score, _channels) in enumerate(hits, start=1):
        if chunk.page in pages:
            return i
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="data/eval/qa_multi.json")
    parser.add_argument("--paper", default=None, help="只跑某一篇（默认全部）")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.paper:
        cases = [c for c in cases if c.get("paper") == args.paper]

    # 翻译只做一次：重排前后用的是同一批英文查询，保证对照公平
    queries = [await _search_query(case["q"]) for case in cases]

    for label, enabled in (("关重排", False), ("开重排", True)):
        pipeline._RERANK_ENABLED = enabled
        ranks = [
            rank_of(pipeline.retrieve_scored(q, top_k=5, source=case["paper"]), case["pages"])
            for q, case in zip(queries, cases, strict=True)
        ]
        r1 = sum(1 for r in ranks if r == 1)
        r3 = sum(1 for r in ranks if r and r <= 3)
        r5 = sum(1 for r in ranks if r and r <= 5)
        print(f"{label}: R@1 {r1}/{len(cases)}  R@3 {r3}/{len(cases)}  R@5 {r5}/{len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
