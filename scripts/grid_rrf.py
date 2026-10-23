"""网格搜索：稠密/BM25 权重比 × RRF 的 k 常数，看哪组在固定测试集上召回最好。

为什么要有它：
    "向量 0.7 / BM25 0.3" 这类比例是拍脑袋定的。RRF 里权重只影响名次贡献，
    到底哪组好，只能拿固定问题集跑一遍、看 Recall 数字。

指标口径：
    Recall@k = 预期页码出现在前 k 名（只测检索，不测回答）。
    测试集就是 scripts/eval_min.py 里的 5 道题 —— 只有 5 题，结论只代表这 5 题。

用法：
    python scripts/grid_rrf.py
    python scripts/grid_rrf.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import _search_query  # noqa: E402
from app.rag import pipeline  # noqa: E402
from scripts.eval_min import CASES, SOURCE  # noqa: E402

# (稠密权重, BM25 权重, 说明)。哈希通道在网格里关掉（前面消融已证明它没有边际贡献），
# 生产默认仍是 dense/hash/bm25 = 2.0/0.5/1.0。
RATIOS = [
    (2.0, 1.0, "默认 0.67/0.33"),
    (0.7, 0.3, "你指定 0.7/0.3"),
    (0.6, 0.4, "0.6/0.4"),
    (0.5, 0.5, "一半一半"),
    (0.8, 0.2, "0.8/0.2"),
    (1.0, 0.0, "只用向量"),
    (0.0, 1.0, "只用 BM25"),
]

KS = [60, 40, 30, 20]


def rank_of(hits, expected_pages) -> int | None:
    for i, (chunk, _score, _channels) in enumerate(hits, start=1):
        if chunk.page in expected_pages:
            return i
    return None


def load_cases(path: str | None):
    if not path:
        return CASES
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [{"q": item["q"], "pages": item["pages"]} for item in data["items"]]


async def run_grid(cases) -> list[dict]:
    # 翻译只做一次（_search_query 内部有缓存，重复调用不会重复花钱）
    queries = [await _search_query(case["q"]) for case in cases]

    rows = []
    for w_dense, w_bm25, label in RATIOS:
        for k in KS:
            pipeline._W_DENSE = w_dense
            pipeline._W_BM25 = w_bm25
            pipeline._W_HASH = 0.0
            pipeline._RRF_K = k

            ranks = [
                rank_of(
                    pipeline.retrieve_scored(q, top_k=5, source=SOURCE), case["pages"]
                )
                for q, case in zip(queries, cases, strict=True)
            ]
            recall = {
                n: sum(1 for r in ranks if r and r <= n) for n in (1, 3, 5)
            }
            rows.append(
                {
                    "label": label,
                    "w_dense": w_dense,
                    "w_bm25": w_bm25,
                    "k": k,
                    "ranks": ranks,
                    "recall": recall,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cases", help="评测集 JSON（默认用 eval_min.py 里内置的 5 题）")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    rows = asyncio.run(run_grid(cases))
    total = len(cases)

    if args.json:
        print(json.dumps({"total": total, "rows": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"测试集: {SOURCE} | {total} 题 | hash 通道权重=0（网格内关闭）")
    print("-" * 78)
    print(f"{'配置':<18}{'dense/bm25':<16}{'k':<6}{'R@1':<8}{'R@3':<8}{'R@5':<8}")
    print("-" * 78)
    for r in rows:
        print(
            f"{r['label']:<18}"
            f"{r['w_dense']:.1f}/{r['w_bm25']:.1f}      "
            f"{r['k']:<6}"
            f"{r['recall'][1]}/{total:<6}"
            f"{r['recall'][3]}/{total:<6}"
            f"{r['recall'][5]}/{total:<6}"
        )
    print("-" * 78)

    best = max(rows, key=lambda r: (r["recall"][1], r["recall"][3], r["recall"][5]))
    print(f"本题集最好: {best['label']} (dense={best['w_dense']}, bm25={best['w_bm25']}, k={best['k']})"
          f" → R@1 {best['recall'][1]}/{total}, R@3 {best['recall'][3]}/{total}, R@5 {best['recall'][5]}/{total}")
    print(f"注意：只有 {total} 题、且来自同一篇论文，名次抖动很大，不能当成通用最优；"
          "扩到 30 题以上、覆盖多篇论文才有定论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
