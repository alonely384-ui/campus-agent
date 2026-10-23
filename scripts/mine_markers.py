"""挖候选真值：在论文里找"数字型事实句"，并检查它在原文中是否只出现在唯一一页。

为什么要有它：
    手写"预期页码"容易记错（例如把 β2 记成 0.999）。这里让程序先把候选句捞出来，
    每条都打印页码和原文片段，人工挑完之后再由 build_eval_set 复验，避免凭记忆写真值。

用法：
    python scripts/mine_markers.py 1706.03762v7.pdf 2005.14165v4.pdf 2307.09288v2.pdf
    python scripts/mine_markers.py <pdf> --max 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = Path(__file__).resolve().parent.parent
PAPERS = BASE / "data" / "papers"
CACHE = BASE / "data" / "eval" / "pages"

PATTERNS = [
    r"\b\d+(?:\.\d+)?\s?(?:billion|million|B|M)\s+(?:parameters|training tokens|tokens)",
    r"\b\d[\d,]*\s+(?:NVIDIA\s+)?(?:A100|H100|V100|P100|TPU)s?\b",
    r"\b\d+(?:\.\d+)?\s?(?:billion|million)\s+parameters\b",
    r"\bβ1\s*=\s*\d+(?:\.\d+)?",
    r"\bβ2\s*=\s*\d+(?:\.\d+)?",
    r"\blearning rate\s*(?:of|is|=|:)?\s*\d+(?:\.\d+)?(?:e-?\d+)?",
    r"\bbatch size\s*(?:of|is|=|:)?\s*\d[\d,]*",
    r"\bcontext (?:length|window)\s*(?:of|is|=|:)?\s*\d[\d,]*",
    r"\b\d+(?:\.\d+)?\s+BLEU\b",
    r"\b\d[\d,]*\s+(?:layers|transformer layers)\b",
    r"\b\d[\d,\.]*\s?(?:days|hours|minutes|seconds)\b",
    r"\b\d[\d,\.]*\s+(?:images|image-text pairs|examples|tokens)\b",
    r"\b\d+(?:\.\d+)?%\s+(?:accuracy|top-1|zero-shot|exact match)",
    r"\bzero-shot accuracy of \d+(?:\.\d+)?%",
]


def page_texts(filename: str) -> list[str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / (Path(filename).stem + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from pypdf import PdfReader

    reader = PdfReader(str(PAPERS / filename))
    texts = [(page.extract_text() or "") for page in reader.pages]
    cache.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return texts


def sentences(text: str) -> list[str]:
    # 粗切句：按句号/换行断，够用即可
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    return [p.strip() for p in parts if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("papers", nargs="+")
    parser.add_argument("--max", type=int, default=18, help="每篇最多打印多少条候选")
    args = parser.parse_args()

    for filename in args.papers:
        texts = page_texts(filename)
        print("=" * 100)
        print(f"{filename} | {len(texts)} 页")
        print("=" * 100)
        found: list[tuple[str, int, str]] = []
        for pattern in PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            for page_no, text in enumerate(texts, start=1):
                for sent in sentences(text):
                    match = regex.search(sent)
                    if not match:
                        continue
                    token = match.group(0)
                    pages = [i + 1 for i, t in enumerate(texts) if token in t]
                    if len(pages) != 1:
                        continue  # 多页出现无法唯一定位，跳过
                    if len(found) < args.max and sent not in [s for _, _, s in found]:
                        found.append((token, page_no, sent[:180]))
                        break
        for token, page_no, sent in found:
            print(f"[p{page_no:>2}] {token!r}")
            print(f"      {sent}")
        print(f"候选 {len(found)} 条\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
