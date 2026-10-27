"""章节树抽取的单元测试：用合成的"行"喂 `_sections`，不依赖 data/papers 里的真 PDF。

对应赛题 1.3.2 第 1 条（一级/二级标题做成树状导航），这批 PDF 里三种版式都要认：
1. 编号与标题同一行；2. 编号单独一行（加粗、与正文同字号）；3. 无编号的 run-in 小标题。
"""
from app.structure import _appendix, _sections

BODY = 10.0
LEFT = 108.0


def line(text: str, size: float = BODY, x: float = LEFT, bold: bool = False, page: int = 0) -> dict:
    return {"text": text, "size": size, "x": x, "bold": bold, "page": page}


def test_number_and_title_on_same_line():
    lines = [
        line("1 Introduction", size=14.0, page=0),
        line("body text " * 40, page=0),
        line("2 Background", size=14.0, page=1),
    ]
    secs = _sections(lines, BODY)
    assert [(s["number"], s["title"], s["level"]) for s in secs] == [
        ("1", "Introduction", 1),
        ("2", "Background", 1),
    ]


def test_number_alone_on_bold_body_size_line():
    """Attention 那篇：编号单独一行、字号与正文相同，只是加粗。"""
    lines = [
        line("3 Model Architecture", size=14.0, page=1),
        line("body text " * 30, page=1),          # 左边界靠它来估
        line("3.1", bold=True, page=2),
        line("Encoder and Decoder Stacks", page=2),
        line("3.2", bold=True, page=2),
        line("Attention", page=2),
        line("3.2.2", bold=True, page=3),
        line("Multi-Head Attention", page=3),
    ]
    secs = _sections(lines, BODY)
    got = [(s["number"], s["title"], s["level"]) for s in secs]
    assert ("3.1", "Encoder and Decoder Stacks", 2) in got
    assert ("3.2", "Attention", 2) in got
    assert ("3.2.2", "Multi-Head Attention", 3) in got


def test_number_without_bold_is_ignored():
    """正文里孤零零的 "3.2"（不加粗、和正文同字号）不能当标题。"""
    lines = [
        line("3 Model Architecture", size=14.0, page=1),
        line("3.2", bold=False, page=2),
        line("Attention", page=2),
    ]
    assert [s["number"] for s in _sections(lines, BODY)] == ["3"]


def test_toc_page_is_skipped():
    """GPT-3 那篇：目录页字号与正文相同，会伪造出一堆重复的 L1。"""
    lines = [
        line("Abstract", size=14.0, page=0),
        line("1 Introduction", size=14.0, page=3),
        line("Contents", size=14.0, page=1),
        line("1", bold=True, page=1),
        line("Introduction", page=1),
        line("3", page=1),
        line("2", bold=True, page=1),
        line("Meta Learning", page=1),
        line("5", page=1),
    ]
    secs = _sections(lines, BODY)
    # 目录页的三条（1/2 + 标题）都被丢掉；只剩正文里的 Introduction 和它前面的大标题
    assert [(s["number"], s["title"]) for s in secs] == [
        ("", "Abstract"),
        ("1", "Introduction"),
    ]
    assert [s["page"] for s in secs] == [0, 3]


def test_table_cell_number_is_not_a_section():
    """GPT-3 表格里的 "36 MWh"、"68.0 T5-11B" 不是章节。"""
    lines = [
        line("body text " * 30, page=1),
        line("4 Measuring", size=14.0, page=1),
        line("36", bold=True, page=2),
        line("MWh", page=2),
        line("68.0", bold=True, page=2),
        line("T5-11B+SSM", page=2),
    ]
    assert [s["number"] for s in _sections(lines, BODY)] == ["4"]


def test_runin_heading_is_promoted_to_level2():
    """ICLR 版式：加粗的 "Datasets." 后面直接接正文，没有编号。"""
    lines = [
        line("3 Experiments", size=14.0, page=1),
        line("body " * 60, page=1),
        line("Datasets.", bold=True, page=4),
        line("We train on ImageNet-21k. " * 20, page=4),
        line("Model Variants.", bold=True, page=5),
        line("body " * 60, page=5),
    ]
    got = [(s["number"], s["title"], s["level"]) for s in _sections(lines, BODY)]
    assert ("", "Datasets", 2) in got
    assert ("", "Model Variants", 2) in got


def test_runin_rejects_bold_sentences_and_figure_labels():
    """加粗的整句、以及图里的 "Encoder:" 标注，都不能当小标题。"""
    lines = [
        line("3 Experiments", size=14.0, page=1),
        line("body " * 60, page=1),
        line("WikiSQL is introduced in Zhong et al.", bold=True, page=2),
        line("Encoder:", bold=True, page=2),
        line("our method is faster", bold=True, page=2),
    ]
    got = [s["title"] for s in _sections(lines, BODY)]
    assert got == ["Experiments"]


def test_appendix_marks_late_numbered_sections():
    secs = [
        {"number": "1", "title": "Intro", "level": 1, "page": 0},
        {"number": "2", "title": "Method", "level": 1, "page": 2},
        {"number": "A", "title": "Appendix", "level": 1, "page": 9},
        {"number": "A.1", "title": "Details", "level": 2, "page": 9},
    ]
    assert [(s["number"], s["title"]) for s in _appendix(secs)] == [
        ("A", "Appendix"),
        ("A.1", "Details"),
    ]
