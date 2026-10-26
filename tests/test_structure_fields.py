"""细粒度解析的字段级测试（赛题 1.1.2）。

用真实 PDF 断言，因为排版细节（弯引号、无冒号题注、作者-年份式参考文献）
只有在真文件上才会暴露。
"""

from pathlib import Path

import pytest

from app.structure import parse_structure

PAPERS = Path(__file__).resolve().parent.parent / "data" / "papers"
V7 = PAPERS / "1706.03762v7.pdf"          # 编号式参考文献 + 标准 Abstract
LLAMA3 = PAPERS / "2407.21783v3.pdf"      # 无 Abstract 标题 + 作者-年份式参考文献
SCANNED = PAPERS / "1706.03762v1_img.pdf"  # 扫描件，没有文字层

pytestmark = pytest.mark.skipif(not V7.exists(), reason="需要 data/papers 里的论文")


def test_fields_shape():
    info = parse_structure(str(V7))
    expected = {
        "title", "authors", "affiliations", "emails", "abstract", "keywords",
        "sections", "tables", "figures", "formulas", "references", "appendix", "pages",
    }
    assert expected <= set(info)
    assert isinstance(info["tables"], list) and isinstance(info["figures"], list)
    assert set(info["references"]) == {"page", "count", "first", "last"}


def test_title_abstract_affiliations():
    info = parse_structure(str(V7))
    assert "Attention Is All You Need" in info["title"]
    assert "sequence transduction" in info["abstract"].lower()
    assert any("Google" in a for a in info["affiliations"])


def test_numbered_references_and_captions():
    info = parse_structure(str(V7))
    assert info["references"]["page"] == 9
    assert info["references"]["count"] >= 30
    assert info["references"]["first"].startswith("Jimmy Lei Ba")
    assert len(info["tables"]) >= 2 and len(info["figures"]) >= 2
    assert all({"number", "caption", "page"} == set(t) for t in info["tables"])
    assert len(info["formulas"]) >= 10


def test_author_year_references_without_abstract_heading():
    """LLaMA 3：正文摘要没有标题、参考文献是作者-年份式。"""
    if not LLAMA3.exists():
        pytest.skip("缺 2407.21783v3.pdf")
    info = parse_structure(str(LLAMA3))
    assert info["abstract"], "无 Abstract 标题时应从首页正文兜底"
    assert info["references"]["count"] >= 20
    assert len(info["tables"]) >= 5


def test_scanned_pdf_returns_empty_shape_without_raising():
    """扫描件没有文字层：返回同结构空值，不能抛异常。"""
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    info = parse_structure(str(SCANNED))
    assert info["title"] == "" or isinstance(info["title"], str)
    assert info["references"]["count"] == 0
    assert isinstance(info["sections"], list)
