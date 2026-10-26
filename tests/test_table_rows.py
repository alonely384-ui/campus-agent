"""表格行抽取的单元测试。

只测两件不依赖真实 PDF 的性质：
1. 质量规则能不能把"碎片拼成的假表"挡掉；
2. 文件不存在 / 没装 pdfplumber 时不能崩。

真实论文（GPT-3 那张 Sizes 表）的集成断言放在最后，只有 data/papers 存在时才跑，
因为 data/papers/ 不进仓库。
"""

from pathlib import Path

import pytest

from app.structure import _rows_ok, _table_rows

BASE = Path(__file__).resolve().parent.parent
GPT3 = BASE / "data" / "papers" / "2005.14165v4.pdf"


def test_rows_ok_accepts_clean_table():
    """规整的 3 列 3 行表应当被接受。"""
    rows = [
        ["Model", "Params", "Layers"],
        ["GPT-3 Small", "125M", "12"],
        ["GPT-3 Large", "760M", "24"],
    ]
    assert _rows_ok(rows) is True


def test_rows_ok_rejects_two_columns():
    """少于 3 列的表不算结构化表格（两列多半是排版碎片）。"""
    rows = [["A", "B"], ["1", "2"], ["3", "4"]]
    assert _rows_ok(rows) is False


def test_rows_ok_rejects_fragmented_text():
    """单字符占多数的表，基本是把正文当表格了。"""
    rows = [
        ["T", "a", "b", "l"],
        ["e", "2", ":", " "],
        ["x", "y", "z", "w"],
    ]
    assert _rows_ok(rows) is False


def test_rows_ok_rejects_inconsistent_widths():
    """列数忽多忽少，说明格子没对齐。"""
    rows = [
        ["A", "B", "C", "D"],
        ["1", "2"],
        ["3", "4", "5"],
    ]
    assert _rows_ok(rows) is False


def test_table_rows_missing_file_returns_empty():
    """文件不存在时安静返回空字典，不抛异常。"""
    assert _table_rows(str(BASE / "data" / "papers" / "不存在的文件.pdf"), [0, 1]) == {}


def test_table_rows_no_pages_returns_empty():
    assert _table_rows(str(GPT3), []) == {}


@pytest.mark.skipif(not GPT3.exists(), reason="data/papers 不在（未下载论文数据集）")
def test_gpt3_sizes_table_extracted():
    """GPT-3 论文第 8 页的 Sizes 表要能被抠出来，且带参数规模。"""
    out = _table_rows(str(GPT3), [7])
    tables = out.get(7, [])
    assert tables, "第 8 页应当至少抽出一张表"
    flat = [c for rows in tables for row in rows for c in row]
    assert "nparams" in flat
    assert "125M" in flat
