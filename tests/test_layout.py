"""版式判定（赛题 1.3.1）的测试。

判定结果直接决定阅读页的「双栏/单栏」徽章和「按栏看」要不要放大，
所以既要测算法（合成行，不依赖数据文件），也要测接口与真实 PDF。
data/papers 被 gitignore，缺数据时相关用例 skip。
"""

from __future__ import annotations

import fitz
import pytest
from fastapi.testclient import TestClient

from app.layout import detect_layout, layout_summary
from app.main import app
from app.reader import list_reader_papers

client = TestClient(app)


def _two_column_page(width: float = 612.0) -> list[tuple[float, float]]:
    """合成一页双栏的"行"：左栏 12 行、右栏 12 行，中缝在 300 附近。"""
    lines: list[tuple[float, float]] = []
    for _ in range(12):
        lines.append((55.0, 280.0))
        lines.append((307.0, 532.0))
    return lines


def _single_column_page() -> list[tuple[float, float]]:
    """合成一页单栏的行：每行从 55 铺到 532，只有一簇。"""
    return [(55.0, 532.0) for _ in range(24)]


def test_detect_layout_flags_two_column_page():
    """同一页里 x0 明显分成两簇、行数各占一半 → 判双栏。"""
    from app.layout import _columns_of

    columns, split, cols = _columns_of(_two_column_page(), 612.0)
    assert columns == 2
    assert split is not None
    assert 280.0 < split < 307.0   # 中缝落在左栏右边界与右栏左边界之间
    assert cols is not None and len(cols) == 2
    left, right = cols
    assert left[0] < left[1] <= split < right[0] < right[1]


def test_detect_layout_keeps_single_column_page():
    """单栏页只有一簇 → 判单栏，且不给中缝和栏范围。"""
    from app.layout import _columns_of

    columns, split, cols = _columns_of(_single_column_page(), 612.0)
    assert columns == 1
    assert split is None
    assert cols is None


def test_detect_layout_is_not_fooled_by_page_number():
    """单栏页里右对齐的页码/侧注只制造零星几行，凑不成"右栏"这一簇。"""
    from app.layout import _columns_of

    lines = _single_column_page()
    lines += [(470.0, 520.0) for _ in range(4)]   # 4 行靠右的短内容
    columns, _, _ = _columns_of(lines, 612.0)
    assert columns == 1


def test_detect_layout_needs_enough_lines():
    """行数太少（封面、扫描件）不敢判：返回 (None, None, None)，由上层记 unknown。"""
    from app.layout import _columns_of

    assert _columns_of([(55.0, 200.0)], 612.0) == (None, None, None)


def _papers() -> list[dict]:
    items = list_reader_papers()
    if not items:
        pytest.skip("data/papers 里没有 PDF（该目录被 gitignore），跳过需要真实文件的用例")
    return items


def test_layout_summary_has_mode_and_counts():
    paper = _papers()[0]
    summary = layout_summary(f"data/papers/{paper['filename']}")
    assert summary["mode"] in {"two-column", "single-column", "mixed", "unknown"}
    assert summary["total_pages"] >= 1
    assert summary["sampled_pages"] >= 1
    assert isinstance(summary["switches"], list)


def test_layout_endpoint_returns_pages():
    paper = _papers()[0]
    r = client.get(f"/paper/{paper['filename']}/layout")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] in {"two-column", "single-column", "mixed", "unknown"}
    assert body["pages"], "至少要有一页的判定结果"
    for page in body["pages"].values():
        assert page["columns"] in (1, 2)


def test_layout_endpoint_404_for_unknown_paper():
    assert client.get("/paper/not-here.pdf/layout").status_code == 404


def test_layout_endpoint_rejects_path_traversal():
    """/layout 走的是同一条 safe_paper_path 守卫，别绕过去读到 app/ 里的文件。"""
    assert client.get("/paper/..%2Fapp%2Fmain.py/layout").status_code == 404


def test_structure_has_layout_and_table_fields():
    """章节接口顺带给出的版式摘要、表格清单、公式页码：字段齐、页码是 1 基。"""
    paper = _papers()[0]
    meta = client.get(f"/paper/{paper['filename']}/structure").json()
    assert meta["layout"]["mode"] in {"two-column", "single-column", "mixed", "unknown"}
    for tab in meta["tables"]:
        assert tab["page"] >= 1
        assert tab["caption"]
    for row in meta["formula_pages"]:
        assert row["page"] >= 1
        assert row["count"] >= 1


def test_detect_layout_on_synthetic_pdf(tmp_path):
    """真的造一个双栏 PDF 跑一遍：文本框按两列放，判定应给出 2 栏。"""
    pdf_path = tmp_path / "synthetic_two_column.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for i in range(14):
        y = 120 + i * 34
        # 左栏
        page.insert_text((56, y), "Left column text line " * 2, fontsize=10)
        # 右栏
        page.insert_text((320, y), "Right column text line" * 2, fontsize=10)
    doc.save(pdf_path)
    doc.close()

    result = detect_layout(pdf_path)
    assert result["mode"] in {"two-column", "mixed"}
    assert any(p["columns"] == 2 for p in result["pages"].values())
