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
    assert s == {"title": "", "authors": [], "sections": [], "pages": 0}
