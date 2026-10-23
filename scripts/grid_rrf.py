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
        return [{"q": c["q"], "pages": c["pages"], "paper": SOURCE} for c in CASES]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        {"q": item["q"], "pages": item["pages"], "paper": item.get("paper", SOURCE)}
        for item in data["items"]
    ]


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
                    pipeline.retrieve_scored(q, top_k=5, source=case["paper"]),
                    case["pages"],
                )
                for q, case in zip(queries, cases, strict=True)
            ]
            recall = {
                n: sum(1 for r in ranks if r and r <= n) for n in (1, 3, 5)
            }
            per_paper: dict[str, list[int]] = {}
            for case, rank in zip(cases, ranks, strict=True):
                per_paper.setdefault(case["paper"], []).append(rank)
            rows.append(
                {
                    "label": label,
                    "w_dense": w_dense,
                    "w_bm25": w_bm25,
                    "k": k,
                    "ranks": ranks,
                    "recall": recall,
                    "per_paper": {
                        paper: {
                            n: sum(1 for r in rs if r and r <= n) for n in (1, 3, 5)
                        }
                        for paper, rs in per_paper.items()
                    },
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

    print(f"评测集: {total} 题 | 论文 {len({c['paper'] for c in cases})} 篇 | hash 权重=0（网格内关闭）")
    print("-" * 96)
    print(f"{'配置':<18}{'dense/bm25':<14}{'k':<5}{'总R@1':<9}{'总R@3':<9}{'总R@5':<9}分论文 R@1")
    print("-" * 96)
    for r in rows:
        per = "  ".join(
            f"{paper.split('.')[0][:12]}:{v[1]}" for paper, v in sorted(r["per_paper"].items())
        )
        print(
            f"{r['label']:<18}"
            f"{r['w_dense']:.1f}/{r['w_bm25']:.1f}     "
            f"{r['k']:<5}"
            f"{r['recall'][1]:>2}/{total:<6}"
            f"{r['recall'][3]:>2}/{total:<6}"
            f"{r['recall'][5]:>2}/{total:<6}"
            f"{per}"
        )
    print("-" * 96)

    best = max(rows, key=lambda r: (r["recall"][1], r["recall"][3], r["recall"][5]))
    print(f"本题集最好: {best['label']} (dense={best['w_dense']}, bm25={best['w_bm25']}, k={best['k']})"
          f" → R@1 {best['recall'][1]}/{total}, R@3 {best['recall'][3]}/{total}, R@5 {best['recall'][5]}/{total}")
    print(f"注意：{total} 题、{len({c['paper'] for c in cases})} 篇论文，结论只代表这个集合；"
          "权重差异小于 ±2 题时应视为噪声。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
