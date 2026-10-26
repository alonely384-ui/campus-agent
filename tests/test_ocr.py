"""扫描件 OCR 缓存链路测试（赛题 1.1.2：要能吃扫描版 PDF）。

测试依赖 data/papers/ocr/<stem>.json（scripts/ocr_scanned.py 生成）；
没生成时跳过，不让 CI 因为缺缓存红掉。
"""

from pathlib import Path

import pytest

from app.ocr import cache_path, cached_page, load_cache
from app.rag.pipeline import load_pdf

PAPERS = Path(__file__).resolve().parent.parent / "data" / "papers"
SCANNED = PAPERS / "1706.03762v1_img.pdf"

pytestmark = pytest.mark.skipif(
    not SCANNED.exists() or not cache_path(str(SCANNED)).exists(),
    reason="需要扫描件与 OCR 缓存",
)


def test_cache_has_all_pages():
    pages = load_cache(str(SCANNED))
    assert len(pages) >= 10
    assert "Attention" in pages["0"]


def test_cached_page_by_index():
    text = cached_page(str(SCANNED), 0)
    assert "Attention Is All You Need" in text


def test_missing_cache_is_empty(tmp_path):
    fake = tmp_path / "nope.pdf"
    fake.write_bytes(b"%PDF-1.4")
    assert cached_page(str(fake), 0) == ""


def test_load_pdf_uses_ocr_cache():
    chunks = load_pdf(str(SCANNED))
    assert len(chunks) >= 10
    assert chunks[0].page == 1
    assert "Attention" in chunks[0].text
