"""批量预翻译论文：把 data/meta/translations/<stem>.json 提前生成好。

为什么要这个脚本：
    演示/比赛现场如果网络不稳，或者不想每次都等模型出活，
    提前把几篇论文的译文跑好、连同 JSON 一起提交，换台机器也能直接看译文。

用法：
    python scripts/translate_papers.py 1607.06450v1.pdf 1706.03762v7.pdf --pages 1-9
    python scripts/translate_papers.py 1607.06450v1.pdf --all          # 跳过参考文献
    python scripts/translate_papers.py 1607.06450v1.pdf --pages 3 --force

输出：
    data/meta/translations/1607.06450v1.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.papers import PAPERS_DIR  # noqa: E402
from app.translate import (  # noqa: E402
    load_translation,
    references_page_of,
    save_translation,
    translate_pages,
)


def parse_pages(spec: str, ref_page: int | None, total_pages: int) -> list[int]:
    """'1-9,12' / 'all' → 页码列表。all 时跳过 References 及其后面。"""
    spec = (spec or "").strip().lower()
    if spec in ("all", "", "*"):
        limit = ref_page - 1 if ref_page and ref_page > 1 else total_pages
        return list(range(1, limit + 1))
    wanted: list[int] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            a, _, b = piece.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                wanted.extend(range(int(a), int(b) + 1))
        elif piece.isdigit():
            wanted.append(int(piece))
    return sorted({p for p in wanted if 1 <= p <= total_pages})


async def run_one(filename: str, pages_spec: str, force: bool) -> None:
    pdf = PAPERS_DIR / filename
    if not pdf.is_file():
        print(f"[跳过] 找不到 {pdf}")
        return
    import fitz

    with fitz.open(pdf) as doc:
        total = len(doc)
    ref_page = references_page_of(filename)
    wanted = parse_pages(pages_spec, ref_page, total)
    print(f"\n== {filename}（{total} 页，References 起始 {ref_page}）→ 译 {len(wanted)} 页")

    done = 0
    async for ev in translate_pages(filename, pdf, pages=wanted, force=force):
        if ev["type"] == "page":
            done += 1
            flag = "缓存" if ev["cached"] else "新译"
            print(f"  [{done}/{len(wanted)}] 第 {ev['page']} 页 · {flag} · {len(ev['markdown'])} 字")
        elif ev["type"] == "error":
            print(f"  [失败] 第 {ev.get('page')} 页：{ev.get('message')}")
        elif ev["type"] == "done":
            data = load_translation(filename)
            saved = save_translation(filename, data)
            print(f"  完成，累计缓存 {len(data.get('pages', {}))} 页 → {saved}")


def main() -> None:
    ap = argparse.ArgumentParser(description="批量预翻译论文（译文存 data/meta/translations）")
    ap.add_argument("papers", nargs="+", help="data/papers 下的 PDF 文件名")
    ap.add_argument("--pages", default="all", help="页码：'all' 或 '1-9' 或 '3,5,7'（默认 all）")
    ap.add_argument("--force", action="store_true", help="无视缓存重译")
    args = ap.parse_args()

    for name in args.papers:
        asyncio.run(run_one(name, args.pages, args.force))


if __name__ == "__main__":
    main()
