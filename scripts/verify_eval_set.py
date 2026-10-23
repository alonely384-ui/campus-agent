"""校验评测集：每条的 marker 必须只在声明的页码出现。

为什么要有它：
    真值页码如果靠记忆，就会悄悄错。这个脚本把"我以为的第几页"变成可反驳的检查：
    只要 marker 出现在别的页、或者根本找不到，就直接报错退出。

用法：
    python scripts/verify_eval_set.py                     # 校验 data/eval/qa_multi.json
    python scripts/verify_eval_set.py --file data/eval/qa_v7.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.mine_markers import page_texts  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
DEFAULT = BASE / "data" / "eval" / "qa_multi.json"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(DEFAULT))
    args = parser.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not items:
        print("评测集为空")
        return 1

    cache: dict[str, list[str]] = {}
    failures = 0
    for idx, item in enumerate(items, start=1):
        paper = item["paper"]
        if paper not in cache:
            cache[paper] = [norm(t) for t in page_texts(paper)]
        texts = cache[paper]
        marker = norm(item["marker"])
        found = [i + 1 for i, text in enumerate(texts) if marker in text]
        expected = item["pages"]
        if found != expected:
            failures += 1
            print(f"[FAIL {idx:>2}] {item['q']}")
            print(f"           marker={item['marker']!r} 期望页={expected} 实际={found}")
        elif len(expected) != 1:
            failures += 1
            print(f"[FAIL {idx:>2}] 期望页必须恰好 1 页: {item['q']} -> {expected}")

    papers = sorted({item["paper"] for item in items})
    colloquial = sum(1 for item in items if item.get("colloquial"))
    print("-" * 70)
    print(f"共 {len(items)} 题 | 论文 {len(papers)} 篇: {papers} | 口语化 {colloquial} 题 | 失败 {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
