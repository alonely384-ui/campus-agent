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
    # tables 现在多了 rows 字段（结构化抽取成功时填充），所以只断言必要键都存在
    assert all({"number", "caption", "page"} <= set(t) for t in info["tables"])
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
    """扫描件抽不到时也必须返回同结构空值，不能抛异常。

    注意：这条测的是"字段形状永远齐全"，不是"扫描件解析不出来"。
    扫描件能被 OCR 缓存救回来之后，`references.count` 可能 > 0，
    所以这里只断言类型与键齐全。
    """
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    info = parse_structure(str(SCANNED))
    assert isinstance(info["title"], str)
    assert isinstance(info["authors"], list)
    assert isinstance(info["sections"], list)
    assert isinstance(info["references"], dict)
    assert set(info["references"]) == {"page", "count", "first", "last"}


# ---- 扫描件走 OCR 缓存（赛题 1.1.2 点名的那篇）----

def test_scanned_pdf_uses_ocr_cache():
    """1706.03762v1_img.pdf 没有文字层，解析必须落到 OCR 缓存那条路上。"""
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    from app.ocr import cache_path

    if not cache_path(str(SCANNED)).exists():
        pytest.skip("没有 OCR 缓存（先跑 scripts/ocr_scanned.py）")
    info = parse_structure(str(SCANNED))
    assert info["source"] == "ocr"
    assert info["title"] == "Attention Is All You Need"
    assert len(info["sections"]) >= 15
    assert info["references"]["count"] >= 30


def test_scanned_pdf_authors_reconstructed_from_ocr():
    """OCR 把名字粘成 "AshishVaswani*"，要还原成带空格的人名，顺序保持原样。"""
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    from app.ocr import cache_path

    if not cache_path(str(SCANNED)).exists():
        pytest.skip("没有 OCR 缓存")
    info = parse_structure(str(SCANNED))
    authors = info["authors"]
    assert len(authors) == 8, authors
    assert authors[0] == "Ashish Vaswani"
    assert "Lukasz Kaiser" in authors
    # 不能把机构当作者
    assert not any("Google" in a for a in authors)


def test_scanned_pdf_affiliations_paired_by_column():
    """单位按作者列轮转配对：8 位作者 4 列，3 个单位依次分配。"""
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    from app.ocr import cache_path

    if not cache_path(str(SCANNED)).exists():
        pytest.skip("没有 OCR 缓存")
    info = parse_structure(str(SCANNED))
    assert "Google Brain" in info["affiliations"]
    assert "University of Toronto" in info["affiliations"]
    mapping = info["author_affiliations"]
    assert len(mapping) == len(info["authors"])
    assert all(len(v) == 1 for v in mapping.values())


def test_scanned_pdf_summary_skips_arxiv_watermark():
    """arXiv 水印（"arXiv:1706.03762v1"、"[cs.CL]"）不能混进摘要正文。"""
    if not SCANNED.exists():
        pytest.skip("缺扫描件")
    from app.ocr import cache_path

    if not cache_path(str(SCANNED)).exists():
        pytest.skip("没有 OCR 缓存")
    abstract = parse_structure(str(SCANNED))["abstract"]
    assert abstract
    assert "arxiv" not in abstract.lower()
    assert "cs." not in abstract


def test_text_pdf_marks_source_text():
    """有文字层的 PDF 走主路径，source 标成 text，不受 OCR 逻辑影响。"""
    info = parse_structure(str(V7))
    assert info["source"] == "text"
    assert info["title"]


def test_despace_ocr_restores_split_words():
    """OCR 丢空格是扫描件的通病，`_despace_ocr` 要把能靠形状判断的粘连补回来。

    能修的是有**大写边界**的粘连（编号后、驼峰处）；纯小写中缝
    （"trainedourmodels"）没有任何信号可依据，谁来了也只能靠词表猜，
    所以这里不断言那种情况——只保证"该拆的拆开、不该动的别动"。
    """
    from app.structure import _despace_ocr

    assert _despace_ocr("5.2Hardware andSchedule") == "5.2 Hardware and Schedule"
    assert _despace_ocr("GoogleBrain") == "Google Brain"
    assert _despace_ocr("MultiHead") == "Multi Head"
    assert _despace_ocr("SelfAttention") == "Self Attention"
    assert _despace_ocr("ScaledDotProductAttention") == "Scaled Dot Product Attention"
    # 本来就有空格的不能被改坏
    assert _despace_ocr("Attention Is All You Need") == "Attention Is All You Need"
    # 单词不能被拆碎
    assert _despace_ocr("Softmax") == "Softmax"
    assert _despace_ocr("Transformer") == "Transformer"


def test_ocr_heading_shape_rules():
    """扫描件没有字号，只能靠形状认章节标题。"""
    from app.structure import _ocr_looks_like_heading

    assert _ocr_looks_like_heading("7Conclusion")
    assert _ocr_looks_like_heading("1 Introduction")
    assert _ocr_looks_like_heading("5.2Hardware andSchedule")
    # 正文长句、带行号的残片都不算
    assert not _ocr_looks_like_heading(
        "2014English-Frenchdatasetconsisting of36Msentences andsplit tokensintoa32000word-piece"
    )
    assert not _ocr_looks_like_heading("34.")


def test_ocr_leading_i_is_repaired():
    """OCR 把行首 I 认成 l/|（"llia Polosukhin"），要修回 I。"""
    from app.structure import _fix_ocr_leading_i

    assert _fix_ocr_leading_i("llia Polosukhin") == "Ilia Polosukhin"
    assert _fix_ocr_leading_i("|llia Polosukhin") == "Illia Polosukhin"
    # 正常名字不能被改（"Long" 前面没有空格分隔，不该动）
    assert _fix_ocr_leading_i("Llion Jones") == "Llion Jones"
