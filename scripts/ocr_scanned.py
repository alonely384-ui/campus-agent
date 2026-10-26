"""给扫描版 PDF 生成 OCR 缓存（赛题 1.1.2：要能吃扫描件）。

用法：
    python scripts/ocr_scanned.py                      # 自动找出 data/papers 里没有文字层的 PDF
    python scripts/ocr_scanned.py --pdf 1706.03762v1_img.pdf
    python scripts/ocr_scanned.py --all-pages          # 有文字层也整篇 OCR（一般不需要）

产物：data/papers/ocr/<文件名>.json  ->  {"0": "第一页文字", "1": "第二页文字", ...}
检索侧（load_pdf / page_text）只读缓存，不在请求里跑 OCR。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader  # noqa: E402

from app.ocr import OCR_DIR, available, ocr_pdf, save_cache  # noqa: E402

PAPERS = Path(__file__).resolve().parent.parent / "data" / "papers"


def empty_pages(pdf: Path) -> list[int]:
    """哪些页没有文字层（0 基页号）。"""
    reader = PdfReader(str(pdf))
    return [i for i, page in enumerate(reader.pages) if not (page.extract_text() or "").strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="只处理这一篇（文件名或路径）")
    parser.add_argument("--all-pages", action="store_true", help="整篇 OCR，而不是只补空白页")
    parser.add_argument("--max-pages", type=int, default=0, help="最多 OCR 多少页（0 = 不限）")
    args = parser.parse_args()

    if not available():
        print("OCR 引擎不可用：pip install rapidocr-onnxruntime")
        return 1

    targets = [PAPERS / args.pdf] if args.pdf else sorted(PAPERS.glob("*.pdf"))
    todo: list[tuple[Path, list[int]]] = []
    for pdf in targets:
        if not pdf.exists():
            print(f"跳过（不存在）: {pdf.name}")
            continue
        pages = list(range(len(PdfReader(str(pdf)).pages))) if args.all_pages else empty_pages(pdf)
        if pages:
            todo.append((pdf, pages))
        else:
            print(f"跳过（有文字层）: {pdf.name}")

    if not todo:
        print("没有需要 OCR 的页")
        return 0

    print(f"待 OCR: {sum(len(p) for _, p in todo)} 页 / {len(todo)} 篇")
    t0 = time.time()
    for pdf, pages in todo:
        if args.max_pages:
            pages = pages[: args.max_pages]
        print(f"  处理 {pdf.name}（{len(pages)} 页）...", end=" ", flush=True)
        t1 = time.time()
        result = ocr_pdf(str(pdf), pages)
        out = save_cache(str(pdf), result)
        got = sum(len(v) for v in result.values())
        print(f"{len(result)} 页 / {got} 字 / {time.time() - t1:.1f}s -> {out}")
    print(f"总用时 {time.time() - t0:.1f}s；缓存目录 {OCR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
