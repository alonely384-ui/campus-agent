"""命令行：刷新"新论文推荐"缓存（赛题 1.4.2 / 1.4.3）。

用法：
    python scripts/refresh_recommend.py                    # 引用关系 + 近期上架，两路一起扫
    python scripts/refresh_recommend.py --max-papers 3 --limit 8
    python scripts/refresh_recommend.py --no-recent        # 只扫引用关系（快一半）

为什么要脚本：
    赛题写的是"定期刷新 arXiv"——定期任务就该由计划任务调脚本，
    网页上的"刷新"按钮只是同一个函数的另一个入口。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.logging_config import setup_logging  # noqa: E402
from app.recommend import build_recommendations  # noqa: E402


async def run(max_papers: int, limit: int, include_recent: bool) -> int:
    payload = await build_recommendations(
        limit=limit, max_papers=max_papers, include_recent=include_recent
    )
    sources = payload.get("sources") or {}
    print(f"刷新时间：{payload['updated_at']}")
    print(
        f"库内论文：{payload['library_size']} 篇；候选：{payload.get('candidates', 0)} 条"
        f"（引用扩展 {sources.get('cited', 0)} / 新上架 {sources.get('recent', 0)}，"
        f"近期上架扫了 {payload.get('recent_scanned', 0)} 篇）；推荐：{len(payload['items'])} 条"
    )
    if payload.get("errors"):
        print("抓取告警：")
        for line in payload["errors"][:5]:
            print(f"  - {line}")
    print("-" * 72)
    for index, item in enumerate(payload["items"], 1):
        code = "有代码" if item.get("github") else "无代码"
        origin = "新上架" if item.get("source") == "recent" else "引用扩展"
        print(f"{index:2d}. [{item['score']:.3f}] {item['title'][:70]}")
        print(
            f"    {item['arxiv_id'] or '(无 arXiv 号)'} · {item.get('year') or '?'} · {code}"
            f" · {origin} · 引用库内 {len(item.get('cited_by') or [])} 篇"
            f" · 最像 {item.get('topic_of', '')[:40]}"
        )
        print(f"    理由：{item.get('reason', '')}")
    return 0


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="刷新新论文推荐缓存")
    parser.add_argument("--max-papers", type=int, default=6, help="扫几篇库内论文的引用关系")
    parser.add_argument("--limit", type=int, default=12, help="推荐条目上限")
    parser.add_argument(
        "--no-recent", action="store_true", help="不扫 arXiv 近期上架（只走引用扩展，快一半）"
    )
    args = parser.parse_args()
    return asyncio.run(
        run(max(1, min(args.max_papers, 12)), max(1, min(args.limit, 30)), not args.no_recent)
    )


if __name__ == "__main__":
    raise SystemExit(main())
