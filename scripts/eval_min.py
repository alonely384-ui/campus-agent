"""最小测试集：5 道固定题，只看"正确的那一页排第几"。

为什么要有它：
    之前每次改检索都靠"我记得好像命中了"，没有可重复的数字。
    这个脚本不调大模型、不问 LLM 评分，只做一件最硬的事：
    用固定问题检索，看预期页码出现在第几名。改完代码跑一遍就知道有没有退步。

用法：
    python scripts/eval_min.py            # 跑 5 题
    python scripts/eval_min.py --json     # 输出机器可读结果

指标口径：
    Recall@k = 预期页码出现在前 k 名（只看检索，不看回答）；
    这里只报 @1/@3/@5，因为 top_k 默认取 5。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import _search_query  # noqa: E402
from app.rag.pipeline import retrieve_scored  # noqa: E402

# 预期页码来自人工翻 PDF 核对（v7 版本），不是模型生成的
CASES = [
    {
        "q": "论文训练用了多少块 GPU，什么型号？",
        "pages": [7],
        "why": "5.2 Hardware and Schedule 里写着 8 NVIDIA P100",
    },
    {
        "q": "Adam 优化器的 β1、β2 是多少？",
        "pages": [7],
        "why": "同一页的优化器小节 β1=0.9 β2=0.999",
    },
    {
        "q": "这篇论文在 WMT 2014 英德任务上 BLEU 多少？",
        "pages": [8],
        "why": "Table 2 里 en-de 28.4",
    },
    {
        "q": "这篇论文的第六作者是谁？",
        "pages": [1],
        "why": "首页作者列表（检索到首页即可，答案靠元数据）",
    },
    {
        "q": "LayerNorm 的参考文献是哪一篇？",
        "pages": [10],
        "why": "References 里 [11] Ba et al. Layer Normalization",
    },
]

SOURCE = "1706.03762v7.pdf"


def rank_of(hits, expected_pages):
    """返回第一个命中预期页码的排名（1 起），没有则 None。"""
    for i, (chunk, _score, _channels) in enumerate(hits, start=1):
        if chunk.page in expected_pages:
            return i
    return None


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument(
        "--raw", action="store_true", help="不调查询翻译，直接用原问题检索（对照用）"
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    rows = []
    for case in CASES:
        # 默认走真实链路：中文问题先翻成英文检索词（和网页上提问完全一致）
        query = case["q"] if args.raw else await _search_query(case["q"])
        hits = retrieve_scored(query, top_k=args.top_k, source=SOURCE)
        pages = [c.page for c, _s, _ch in hits]
        r = rank_of(hits, case["pages"])
        top = hits[0] if hits else None
        rows.append(
            {
                "question": case["q"],
                "query": query,
                "expected_pages": case["pages"],
                "pages": pages,
                "rank": r,
                "top_score": round(top[1], 4) if top else 0.0,
                "top_channels": top[2] if top else [],
                "why": case["why"],
            }
        )

    total = len(rows)
    recall = {k: sum(1 for r in rows if r["rank"] and r["rank"] <= k) for k in (1, 3, 5)}

    if args.json:
        print(json.dumps({"rows": rows, "recall": recall}, ensure_ascii=False, indent=2))
        return 0

    print(f"单篇: {SOURCE} | top_k={args.top_k} | 题数={total}")
    print("-" * 96)
    for r in rows:
        rank = f"第{r['rank']}名" if r["rank"] else "未进前5"
        print(f"[{rank:>6}] {r['question']}")
        print(f"         检索词: {r['query']}")
        print(f"         页码: {r['pages']} 预期 {r['expected_pages']} | 首名分 {r['top_score']}")
    print("-" * 96)
    for k in (1, 3, 5):
        print(f"Recall@{k}: {recall[k]}/{total} = {recall[k] / total:.0%}")
    missed = [r["question"] for r in rows if not r["rank"]]
    if missed:
        print("未命中:", "；".join(missed))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
