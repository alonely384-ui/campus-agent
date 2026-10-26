"""扫描件 OCR（赛题 1.1.2 要求能吃扫描版 PDF）。

本地引擎 = RapidOCR（onnxruntime，纯离线，模型随包安装），不依赖 tesseract。
OCR 结果按页缓存成 JSON，检索时只读缓存，不在请求路径里跑 OCR（否则一次问答要几十秒）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pymupdf

OCR_DIR = Path(os.getenv("RAG_OCR_DIR", "data/papers/ocr"))
DPI = int(os.getenv("RAG_OCR_DPI", "200"))

_ENGINE = None
_ENGINE_FAILED = False


def _engine():
    """懒加载 OCR 引擎（首次加载约 5–9 秒，之后每页 1–2 秒）。"""
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None or _ENGINE_FAILED:
        return _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    except Exception:  # noqa: BLE001 - 缺依赖时降级为空结果
        _ENGINE_FAILED = True
    return _ENGINE


def available() -> bool:
    return _engine() is not None


def render_png(pdf_path: str, page_no: int, dpi: int = DPI) -> bytes:
    with pymupdf.open(pdf_path) as doc:
        return doc[page_no].get_pixmap(dpi=dpi).tobytes("png")


def ocr_page(pdf_path: str, page_no: int, dpi: int = DPI) -> str:
    engine = _engine()
    if engine is None:
        return ""
    result, _ = engine(render_png(pdf_path, page_no, dpi))
    lines = [item[1] for item in (result or []) if len(item) > 1]
    return "\n".join(lines).strip()


def ocr_pdf(pdf_path: str, pages: list[int] | None = None, dpi: int = DPI) -> dict[str, str]:
    """对整篇（或指定页）做 OCR，返回 {页号字符串: 文字}。页号沿用 0 基，与 pypdf 一致。"""
    with pymupdf.open(pdf_path) as doc:
        targets = pages if pages is not None else list(range(len(doc)))
    out: dict[str, str] = {}
    for pno in targets:
        text = ocr_page(pdf_path, pno, dpi)
        if text:
            out[str(pno)] = text
    return out


def cache_path(pdf_path: str) -> Path:
    return OCR_DIR / f"{Path(pdf_path).stem}.json"


def load_cache(pdf_path: str) -> dict[str, str]:
    path = cache_path(pdf_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缓存坏了就当没有
        return {}


def save_cache(pdf_path: str, pages: dict[str, str]) -> Path:
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(pdf_path)
    path.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return path


def cached_page(pdf_path: str, page_no: int) -> str:
    """取缓存里某一页的 OCR 文本；没有缓存返回空串。"""
    return load_cache(pdf_path).get(str(page_no), "")
