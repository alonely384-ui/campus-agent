"""把 data/catalog.json 的类别从 arXiv 学科分类改成「按主旨分类」。

用法：
    python scripts/reclassify_topics.py            # 只打印，不写盘
    python scripts/reclassify_topics.py --write    # 写回 data/catalog.json

为什么单独做：catalog.json 是生成物，但主旨分类需要人工核一遍，
所以先打印「标题 → 主旨」清单，确认后再 --write。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.topics import assign_all  # noqa: E402

CATALOG = BASE_DIR / "data" / "catalog.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="写回 catalog.json")
    args = parser.parse_args()

    items = json.loads(CATALOG.read_text(encoding="utf-8"))
    items = assign_all(items)

    for item in items:
        mark = " " if item.get("indexed") else "!"
        print(
            f'{mark} {item["file"][:36]:36s} | {item["topic"]:8s} '
            f'| {item.get("arxiv_category", ""):6s} | {str(item.get("title", ""))[:52]}'
        )

    dist = Counter(item["topic"] for item in items)
    print(f"\n分布（共 {len(items)} 篇）：")
    for topic, count in dist.most_common():
        print(f"  {topic}: {count}")
    print("! = 未建索引（扫描件）")

    if args.write:
        CATALOG.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写回 {CATALOG}")
    else:
        print("\n（预览模式，未写盘；加 --write 落盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
