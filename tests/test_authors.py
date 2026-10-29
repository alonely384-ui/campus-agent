"""作者抽取的回归测试：四种反直觉的作者块写法。

这四个用例都是真实论文里踩到的坑，不是编出来的：
    1. LaTeX 模板的作者块没有逗号，靠上标数字分隔
       （CLIP / Codex / GPT-4 都是这种）；
    2. 名字后面直接跟数字，中间没有空格（"Jason Wei 1"）；
    3. 名字和编号粘在一起（"Haotian Liu1∗"），甚至没有逗号（"Bo Peng1,2∗Eric"）；
    4. 整行就是一个机构署名（"OpenAI∗" / "DeepSeek-AI"），它不是单位而是作者。

另外两条是防回归的：
    5. 跨多行的大标题不能落进作者区（DiT 的 "AN IMAGE IS WORTH ... AT SCALE"）；
    6. 单位行（"Google Brain"、"Tsinghua University"）不能被当作者。
"""

import pymupdf
import pytest

from app.structure import (
    _NAME_RE,
    _authors,
    _body_size,
    _lines,
    _title,
    _title_bottom,
    parse_structure,
)


def _page(*lines):
    """造一页：每行是 (y, text, size)，返回 (_lines 的结果, 标题块底部, 正文字号)。"""
    doc = pymupdf.open()
    page = doc.new_page()
    for y, text, size in lines:
        page.insert_text((72, y), text, fontsize=size)
    got = _lines(page, 1)
    body = _body_size(got)
    title = _title(got, body)
    doc.close()
    return got, _title_bottom(got, title, body), body


def test_latex_block_without_commas():
    """LaTeX 模板：作者之间没有逗号，只有 * 和上标数字。"""
    lines, title_y, body = _page(
        (90, "A Great Paper", 17),
        (130, "Alec Radford * 1 Jong Wook Kim * 1 Chris Hallacy 1 Aditya Ramesh 1", 10),
        (145, "Girish Sastry 1 Amanda Askell 1 Pamela Mishkin 1 Jack Clark 1", 10),
        (180, "Abstract", 12),
        (200, "We study many interesting things about the world and report them.", 10),
    )
    assert _authors(lines, title_y, body) == [
        "Alec Radford", "Jong Wook Kim", "Chris Hallacy", "Aditya Ramesh",
        "Girish Sastry", "Amanda Askell", "Pamela Mishkin", "Jack Clark",
    ]


def test_name_with_trailing_number():
    """名字后面直接跟上标编号，中间没有空格。"""
    lines, title_y, body = _page(
        (90, "Emergent Abilities", 17),
        (130, "Jason Wei 1", 10),
        (145, "Yi Tay 1", 10),
        (160, "Rishi Bommasani 2", 10),
        (195, "Abstract", 12),
        (215, "Capabilities emerge at scale in ways that are not predictable.", 10),
    )
    assert _authors(lines, title_y, body) == ["Jason Wei", "Yi Tay", "Rishi Bommasani"]


def test_name_glued_to_number_and_symbol():
    """名字和编号粘在一起："Haotian Liu1∗, Chunyuan Li2∗"。"""
    lines, title_y, body = _page(
        (90, "Visual Instruction Tuning", 17),
        (130, "Haotian Liu1∗, Chunyuan Li2∗, Qingyang Wu3, Yong Jae Lee1", 10),
        (160, "Abstract", 12),
        (180, "We introduce a large multimodal model trained end to end.", 10),
    )
    assert _authors(lines, title_y, body) == [
        "Haotian Liu", "Chunyuan Li", "Qingyang Wu", "Yong Jae Lee",
    ]


def test_lone_org_signature_is_an_author():
    """整行就是一个机构署名时，它是作者而不是单位。"""
    lines, title_y, body = _page(
        (90, "GPT-4 Technical Report", 17),
        (130, "OpenAI*", 10),
        (160, "Abstract", 12),
        (180, "We report the development of a large multimodal model here.", 10),
    )
    assert _authors(lines, title_y, body) == ["OpenAI"]

    lines, title_y, body = _page(
        (90, "DeepSeek-V3 Technical Report", 16),
        (130, "DeepSeek-AI", 11),
        (160, "research@deepseek.com", 11),
        (200, "Abstract", 15),
        (230, "We present a strong Mixture-of-Experts language model.", 11),
    )
    assert _authors(lines, title_y, body) == ["DeepSeek-AI"]


def test_multiline_title_does_not_leak_into_authors():
    """跨两行的大标题不能落进作者区。"""
    lines, title_y, body = _page(
        (90, "AN IMAGE IS WORTH 16X16 WORDS:", 17),
        (112, "TRANSFORMERS FOR IMAGE RECOGNITION AT SCALE", 17),
        (150, "Alexey Dosovitskiy*, Lucas Beyer*, Alexander Kolesnikov*, Dirk Weissenborn*,", 10),
        (165, "Xiaohua Zhai*, Thomas Unterthiner, Mostafa Dehghani", 10),
        (190, "Google Research, Brain Team", 10),
        (215, "Abstract", 12),
        (235, "While the Transformer architecture has become the de facto standard.", 10),
    )
    got = _authors(lines, title_y, body)
    assert got == [
        "Alexey Dosovitskiy", "Lucas Beyer", "Alexander Kolesnikov", "Dirk Weissenborn",
        "Xiaohua Zhai", "Thomas Unterthiner", "Mostafa Dehghani",
    ]
    # 标题碎片和单位都不能混进来
    assert not any(w in " ".join(got) for w in ("TRANSFORMERS", "IMAGE RECOGNITION", "Brain"))


def test_affiliation_lines_are_skipped():
    """单位行不进作者列表。"""
    lines, title_y, body = _page(
        (90, "Some Paper", 17),
        (130, "Jane Doe", 10),
        (145, "John Smith", 10),
        (160, "Tsinghua University", 10),
        (175, "Microsoft Research", 10),
        (200, "Abstract", 12),
        (220, "This abstract explains the paper in one long sentence here.", 10),
    )
    assert _authors(lines, title_y, body) == ["Jane Doe", "John Smith"]


@pytest.mark.parametrize(
    "line",
    [
        "Jong Wook Kim",
        "Christopher D. Manning",
    ],
)
def test_name_variants_survive(line):
    """多段名 / 中间名缩写的作者都要能认出来。"""
    lines, title_y, body = _page(
        (90, "Some Paper", 17),
        (130, line, 10),
        (160, "Abstract", 12),
        (180, "This abstract explains the paper in one long sentence here.", 10),
    )
    assert _authors(lines, title_y, body) == [line]


def test_ligature_in_name_is_normalised():
    """名字里的连字要先归一："Raﬀel"（ﬀ = U+FB00）→ "Raffel"。

    不走 PDF：内置 Helvetica 不认 U+FB00，insert_text 后再抽会变成 U+00B7，
    测的就不是连字逻辑了。这里直接喂 _lines 结构，专测归一这一步。
    """
    lines = [
        {"text": "Colin Ra\ufb00el", "size": 10.0, "x": 72.0, "y": 130.0, "bold": False, "page": 1},
        {"text": "Ashish Vaswani", "size": 10.0, "x": 72.0, "y": 145.0, "bold": False, "page": 1},
    ]
    assert _authors(lines, 100.0, 10.0) == ["Colin Raffel", "Ashish Vaswani"]


def test_parse_structure_end_to_end_authors(tmp_path):
    """端到端：走 parse_structure，作者区边界同样成立。"""
    path = tmp_path / "p.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Attention Is All You Need", fontsize=18)
    page.insert_text((72, 125), "Ashish Vaswani", fontsize=10)
    page.insert_text((72, 140), "Noam Shazeer", fontsize=10)
    page.insert_text((72, 155), "Google Brain", fontsize=10)
    page.insert_text((72, 185), "Abstract", fontsize=12)
    page.insert_text((72, 205), "The dominant sequence transduction models are based on complex recurrent networks.",
                     fontsize=10)
    doc.save(str(path))
    doc.close()
    assert parse_structure(str(path))["authors"] == ["Ashish Vaswani", "Noam Shazeer"]


# ---- 以下四条对应对真实库的下钻：BLOOM 和 Codex 里踩到的问题 ----


def test_five_word_name_is_one_author():
    """西语全名不能被切成两条。

    Codex（2107.03374v2）里 "Henrique Ponde de Oliveira Pinto" 原来被切成
    "Henrique Ponde" + "Oliveira Pinto" 两个人——名字正则上限只有 3 个词，
    且不认 de/van/von 这类小写虚词。
    """
    assert _NAME_RE.match("Henrique Ponde de Oliveira Pinto")
    assert _NAME_RE.match("Albert Villanova del Moral")
    assert _NAME_RE.match("Leandro Von Werra")
    lines, title_y, body = _page(
        (90, "Evaluating Large Language Models Trained on Code", 17),
        (130, "Mark Chen * 1 Jerry Tworek * 1 Heewoo Jun * 1", 10),
        (145, "Henrique Ponde de Oliveira Pinto * 1 Jared Kaplan * 2", 10),
        (175, "Abstract", 12),
        (195, "We introduce Codex a GPT language model fine tuned on GitHub.", 10),
    )
    assert _authors(lines, title_y, body) == [
        "Mark Chen", "Jerry Tworek", "Heewoo Jun",
        "Henrique Ponde de Oliveira Pinto", "Jared Kaplan",
    ]


def test_contribution_group_heading_is_not_author():
    """贡献分组小标题不是人名。

    BLOOM（2211.05100v4）把作者按贡献分组，每组前面一行小标题：
    "BigScience Workshop∗" / "Major Contributors" / "Dataset" / "Tokenization"。
    这些行字形和真人名一样（两个首字母大写的词），姓名正则拦不住。
    """
    lines, title_y, body = _page(
        (90, "BLOOM", 14),
        (130, "BigScience Workshop*", 11),
        (145, "Major Contributors", 11),
        (160, "Teven Le Scao, Angela Fan, Christopher Akiki", 10),
        (175, "Dataset", 11),
        (190, "Aaron Gokaslan, Adi Simhi", 10),
        (215, "Abstract", 12),
        (235, "Large language models have been shown to achieve strong results.", 10),
    )
    got = _authors(lines, title_y, body)
    assert got == [
        "Teven Le Scao", "Angela Fan", "Christopher Akiki",
        "Aaron Gokaslan", "Adi Simhi",
    ]


def test_name_broken_by_hyphen_is_rejoined():
    """行尾硬连字符断词要接回去："Bhattachar-" + "jee" → "Bhattacharjee"。"""
    lines = [
        {"text": "Joseph Tobing, Joydeep Bhattachar-", "size": 10.0, "x": 72.0, "y": 130.0,
         "bold": False, "page": 1},
        {"text": "jee, Khalid Almubarak, Kimbo Chen", "size": 10.0, "x": 72.0, "y": 145.0,
         "bold": False, "page": 1},
    ]
    assert _authors(lines, 100.0, 10.0) == [
        "Joseph Tobing", "Joydeep Bhattacharjee", "Khalid Almubarak", "Kimbo Chen",
    ]


def test_incomplete_name_at_line_end_is_completed():
    """行尾没写完的名字要用下一行开头补全："Alexander M." + "Rush" → "Alexander M. Rush"。"""
    lines = [
        {"text": "Roman Castagné, Alexandra Sasha Luccioni, Jonathan Tow, Alexander M.", "size": 10.0,
         "x": 72.0, "y": 130.0, "bold": False, "page": 1},
        {"text": "Rush, Stella Biderman, Albert Webson", "size": 10.0, "x": 72.0, "y": 145.0,
         "bold": False, "page": 1},
    ]
    assert _authors(lines, 100.0, 10.0) == [
        "Roman Castagné", "Alexandra Sasha Luccioni", "Jonathan Tow",
        "Alexander M. Rush", "Stella Biderman", "Albert Webson",
    ]


def test_accented_names_survive():
    """重音字母的名字不能被姓名正则漏掉（BLOOM 是多国作者表）。"""
    for name in ("María Grandury", "François Yvon", "Gérard Dupont", "Benoît Sagot"):
        assert _NAME_RE.match(name), name