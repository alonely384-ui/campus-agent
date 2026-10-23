"""对照：生产默认 vs 0.7/0.3 vs 只用向量，逐题看差在哪。

用法：python scripts/compare_configs.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import _search_query  # noqa: E402
from app.rag import pipeline  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CASES_FILE = BASE / "data" / "eval" / "qa_v7.json"
SOURCE = "1706.03762v7.pdf"

CONFIGS = [
    ("生产默认 2.0/0.5/1.0 k=60", 2.0, 0.5, 1.0, 60),
    ("你指定 0.7/0/0.3 k=60", 0.7, 0.0, 0.3, 60),
    ("你指定 0.7/0/0.3 k=30", 0.7, 0.0, 0.3, 30),
    ("只用向量 1.0/0/0 k=60", 1.0, 0.0, 0.0, 60),
]


def rank_of(hits, expected_pages):
    for i, (chunk, _s, _c) in enumerate(hits, start=1):
        if chunk.page in expected_pages:
            return i
    return None


async def main() -> int:
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = data["items"]
    queries = [await _search_query(item["q"]) for item in cases]

    results = {}
    for label, wd, wh, wb, k in CONFIGS:
        pipeline._W_DENSE, pipeline._W_HASH, pipeline._W_BM25, pipeline._RRF_K = wd, wh, wb, k
        ranks = [
            rank_of(pipeline.retrieve_scored(q, top_k=5, source=SOURCE), item["pages"])
            for q, item in zip(queries, cases, strict=True)
        ]
        results[label] = ranks

    header = f"{'问题':<40}" + "".join(f"{lbl[:12]:<14}" for lbl, *_ in CONFIGS)
    print(header)
    print("-" * len(header))
    for idx, item in enumerate(cases):
        cells = "".join(
            f"{('p' + str(r) if r else '✗'):<14}" for r in (res[idx] for res in results.values())
        )
        print(f"{item['q']:<40}{cells}")

    print("-" * len(header))
    for label, ranks in results.items():
        hit1 = sum(1 for r in ranks if r and r <= 1)
        print(f"{label}: R@1 {hit1}/{len(ranks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
