"""结构化解析测试：用 pymupdf 现造一份"像论文"的小 PDF，验证三件事。

测的是行为：
    1. 最大字号那行 = 标题；
    2. 标题下方的"人名样"行 = 作者（按原顺序、不重样）；
    3. "编号单独一行 + 标题另起一行"的版式也能拼成章节。
"""

import pymupdf
import pytest

from app.structure import parse_structure, section_tree_text


@pytest.fixture()
def paper_pdf(tmp_path):
    path = tmp_path / "demo.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Attention Is All You Need", fontsize=18)
    page.insert_text((72, 125), "Ashish Vaswani", fontsize=10)
    page.insert_text((72, 140), "Noam Shazeer", fontsize=10)
    page.insert_text((72, 170), "Google Brain", fontsize=10)
    page.insert_text((72, 210), "1", fontsize=12)
    page.insert_text((72, 225), "Introduction", fontsize=12)
    page.insert_text((72, 260), "The dominant sequence transduction models are based on complex "
                                "recurrent or convolutional neural networks in an encoder-decoder "
                                "configuration. The best performing models also connect the encoder "
                                "and decoder through an attention mechanism.", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_title_is_largest_line(paper_pdf):
    s = parse_structure(str(paper_pdf))
    assert s["title"].strip() == "Attention Is All You Need"
    assert s["pages"] == 1


def test_authors_keep_order_and_skip_affiliations(paper_pdf):
    s = parse_structure(str(paper_pdf))
    assert s["authors"] == ["Ashish Vaswani", "Noam Shazeer"]


def test_section_number_on_its_own_line(paper_pdf):
    s = parse_structure(str(paper_pdf))
    nums = [x["number"] for x in s["sections"]]
    assert "1" in nums
    assert "Introduction" in section_tree_text(s)


def test_broken_pdf_returns_empty_not_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    s = parse_structure(str(bad))
    assert s["title"] == "" and s["authors"] == [] and s["sections"] == [] and s["pages"] == 0
    assert s["references"] == {"page": None, "count": 0, "first": "", "last": ""}
    assert s["tables"] == [] and s["figures"] == [] and s["formulas"] == []


def _make_ref_pdf(tmp_path, name, pages, ref_page, heading, entries, tail_texts=()):
    """造一份参考文献在第 ref_page 页的 PDF，正文页用占位文本填满。"""
    path = tmp_path / name
    doc = pymupdf.open()
    for pno in range(pages):
        page = doc.new_page()
        if pno == 0:
            page.insert_text((72, 90), "A Study Of Something", fontsize=18)
            page.insert_text((72, 125), "Jane Doe", fontsize=10)
        if pno == ref_page:
            y = 90
            page.insert_text((72, y), heading, fontsize=12)
            for e in entries:
                y += 14
                page.insert_text((72, y), e, fontsize=9)
            for t in tail_texts:
                y += 14
                page.insert_text((72, y), t, fontsize=9)
        else:
            page.insert_text((72, 200), "body text for this page, nothing special here.", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


_AUTHOR_YEAR = "Amro Abbas, Kushal Tirumala, Dániel Simig, and Ari S Morcos. Semdedup {i}. Proc, 2020."


def _ref_entries(n):
    """造 n 条「作者-年份式」条目（_REF_ENTRY_PATTERNS 认得的真实格式）。"""
    return [_AUTHOR_YEAR.format(i=i) for i in range(1, n + 1)]


def test_references_found_past_page_90(tmp_path):
    """参考文献在第 95 页的书不能返回 0 条。

    旧实现写死 `limit=90`，541 页的书（参考文献在第 525 页）永远扫不到。
    """
    p = _make_ref_pdf(tmp_path, "book.pdf", 100, 95, "References", _ref_entries(20))
    s = parse_structure(str(p))
    assert s["references"]["page"] == 95
    assert s["references"]["count"] >= 15


def test_references_found_in_short_paper(tmp_path):
    """14 页的论文，参考文献在第 10 页，也不能返回 0 条。

    旧实现从 `range(25, pages)` 起扫，短文档上是空循环。
    """
    refs = [
        "[1] Alex Krizhevsky, Ilya Sutskever. ImageNet classification. NIPS, 2012.",
        "[2] Geoffrey Hinton, Li Deng. Deep neural networks for acoustic modeling. 2012.",
    ]
    p = _make_ref_pdf(tmp_path, "short.pdf", 14, 10, "References", refs)
    s = parse_structure(str(p))
    assert s["references"]["page"] == 10
    assert s["references"]["count"] == 2


def test_bibliography_heading_counts_but_toc_entry_does_not(tmp_path):
    """整本书正文标题是 Bibliography；目录里同名条目不能被当成参考文献页。

    目录条目的特征是同名行后面紧跟一行纯页码。
    """
    p = _make_ref_pdf(tmp_path, "toc.pdf", 12, 8, "Bibliography", _ref_entries(10))
    doc = pymupdf.open(str(p))
    toc = doc[2]
    toc.insert_text((72, 300), "Bibliography", fontsize=10)
    toc.insert_text((72, 314), "463", fontsize=10)
    doc.saveIncr()
    doc.close()

    s = parse_structure(str(p))
    # 命中第 8 页的正文标题，而不是第 2 页的目录条目
    assert s["references"]["page"] == 8
    assert s["references"]["count"] == 10


def test_reference_key_on_its_own_line_still_yields_first(tmp_path):
    """ACL/NeurIPS 的 [ACDE12] 编号独占一行时，first 要从下一行取到正文。"""
    refs = [
        "[ACDE12]",
        "Eduardo G Altmann and Mirko Degli Esposti. On the origin of long-range correlations.",
        "[AS17]",
        "Madhu S. Advani and Andrew M. Saxe. High-dimensional dynamics of generalization.",
    ]
    p = _make_ref_pdf(tmp_path, "keys.pdf", 6, 5, "References", refs)
    s = parse_structure(str(p))
    assert s["references"]["count"] == 2
    assert s["references"]["first"].startswith("Eduardo G Altmann")
