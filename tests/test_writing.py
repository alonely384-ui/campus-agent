"""1.6 写作助手的测试：框架生成 / 架构图 / 数据可视化 / 中译英。

## 这里测什么、不测什么

**不测**：LLM 输出质量、渲染器渲染质量。这两件事每次跑都不一样，
写进测试只会变成不稳定用例，且改不了文案就红一片。

**测**：所有**确定性逻辑**——引用校验、去重、分块、选图、字体注入。
这些才是会悄悄出错、而且出错了用户看不出来的地方。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from app import chart as ch
from app import diagram as dg
from app import writing as w
from app import zh2en as z
from app.literature import Candidate


def _cand(key: str, title: str, source: str = "local", **kw) -> Candidate:
    return Candidate(
        key=key,
        title=title,
        authors=kw.get("authors", ["A B"]),
        year=kw.get("year", 2020),
        venue=kw.get("venue", ""),
        abstract=kw.get("abstract", ""),
        url=kw.get("url", ""),
        source=source,
        arxiv_id=kw.get("arxiv_id", ""),
        doi=kw.get("doi", ""),
        local_file=kw.get("local_file", ""),
    )


# --------------------------------------------------------------------------- 1.6.1 引用校验


def test_single_marker_is_found():
    pool = [_cand("C1", "A"), _cand("C2", "B")]
    check = w.check_citations("Some claim [C1].", pool)
    assert check.cited_keys == ["C1"]
    assert check.unknown_keys == []
    assert check.ok


def test_group_marker_splits_every_key():
    """``[C2, C3]`` 和 ``[C7;C8]`` 都必须把每个 key 拆出来。

    早先的实现先匹配单个 ``[Cn]`` 再匹配组，结果整组只取到第一个，
    其余 key 被静默丢掉——正文引用了却进不了参考文献列表。
    """
    pool = [_cand(f"C{i}", f"T{i}") for i in range(1, 10)]
    check = w.check_citations("A [C2, C3] and B [C7;C8] and C [C1].", pool)
    assert check.cited_keys == ["C2", "C3", "C7", "C8", "C1"]


def test_unknown_key_is_exposed_not_dropped():
    """模型引用了候选池里没有的编号时，必须暴露出来而不是悄悄删掉。"""
    pool = [_cand("C1", "A")]
    check = w.check_citations("Real [C1] and fake [C99].", pool)
    assert check.cited_keys == ["C1"]
    assert check.unknown_keys == ["C99"]
    assert check.ok is False


def test_duplicate_markers_counted_once():
    pool = [_cand("C1", "A")]
    check = w.check_citations("[C1] ... [C1] ... [C1]", pool)
    assert check.cited_keys == ["C1"]


def test_unused_candidates_reported():
    pool = [_cand("C1", "A"), _cand("C2", "B")]
    check = w.check_citations("only [C1]", pool)
    assert check.unused_candidates == ["C2"]


def test_model_written_references_list_is_not_suspicious():
    """模型常自己补一段 ``## References``，按行罗列 ``[C1]``。

    这些标记前面紧挨换行，取句子会拿到空串，词面重叠必然为 0——
    早先的实现因此把整篇的真实引用集体判成可疑（实测 8/8 全中）。
    """
    pool = [
        _cand("C1", "Attention Is All You Need", abstract="attention mechanism transformer"),
        _cand("C2", "Sparse Attention for Long Documents", abstract="sparse attention long document"),
    ]
    md = (
        "## Abstract\n\n长文档问答面临计算开销问题。基于 Transformer 的自注意力机制[C1]代价高昂，"
        "稀疏注意力可以降低这部分开销[C2]。\n\n## References\n\n[C1]\n[C2]\n"
    )
    check = w.check_citations(md, pool)
    assert check.suspicious_keys == []
    assert check.cited_keys == ["C1", "C2"]


def test_bare_marker_line_does_not_convict():
    """整行只有标记时取不到句子，不能凭「空句子零重叠」定罪。"""
    pool = [_cand("C1", "Attention Is All You Need", abstract="attention mechanism")]
    check = w.check_citations("上一段讲稀疏注意力[C1]。\n\n[C1] 单独一行\n", pool)
    assert check.suspicious_keys == []


def test_genuinely_off_topic_citation_still_flagged():
    """真正的错配引用仍要抓出来：编号合法但内容完全无关。"""
    pool = [_cand("C1", "Attention Is All You Need", abstract="attention mechanism transformer")]
    check = w.check_citations("本章讨论餐饮外卖平台的配送路径优化问题[C1]。", pool)
    assert check.suspicious_keys == ["C1"]


def test_reference_list_only_uses_pool_data():
    """参考文献条目必须由候选池数据产生——这是"无幻觉引用"的结构保证。"""
    pool = [_cand("C3", "Sparse Attention Revisited", authors=["Jane Doe", "John Roe"], year=2021)]
    refs = w.build_reference_list(["C3"], pool)
    assert len(refs) == 1
    assert "Jane Doe" in refs[0]["text"]
    assert "Sparse Attention Revisited" in refs[0]["text"]
    assert "2021" in refs[0]["text"]


def test_reference_list_skips_unknown_key():
    pool = [_cand("C1", "A")]
    assert w.build_reference_list(["C1", "C99"], pool) == [
        {
            "key": "C1",
            "text": w.format_reference(pool[0]),
            "source": "local",
            "title": "A",
            "year": 2020,
            "arxiv_id": "",
            "doi": "",
            "url": "",
            "local_file": "",
        }
    ]


def test_prompt_forbids_fabrication_and_lists_every_candidate():
    pool = [_cand("C1", "Alpha"), _cand("C2", "Beta")]
    msgs = w.build_framework_prompt("test idea", pool)
    assert msgs[0]["role"] == "system"
    assert "编造" in msgs[0]["content"] or "不允许" in msgs[0]["content"]
    user = msgs[1]["content"]
    assert "[C1]" in user and "[C2]" in user


def test_prompt_handles_empty_pool():
    """一个候选都没有时，prompt 里不能说"池里有 0 篇但随便引"。"""
    msgs = w.build_framework_prompt("idea", [])
    assert "没有检索到候选文献" in msgs[1]["content"]


def test_extract_queries_from_chinese_and_english():
    q = w.extract_idea_queries("用 attention mechanism 改进 Transformer 的长文档摘要")
    assert any("attention" in x.lower() for x in q)
    assert any("Transformer" in x for x in q)
    assert len(q) <= 3


def test_extract_queries_does_not_start_with_function_word():
    """中文检索词不能以「的」「基于」「想做一个」这类虚词开头。

    旧实现按 4-12 字硬切，「想做一个基于引用关系图谱的论文推荐系统」会被切成
    ['想做一个基于引用关系图谱', '的论文推荐系统', ...]，第二段以「的」开头，
    拿去检索只能靠字面命中，捞回来的是别的领域的老论文。
    """
    idea = "想做一个基于引用关系图谱的论文推荐系统，让用户可以顺着参考文献链条发现高价值文献"
    q = w.extract_idea_queries(idea)
    assert q, "不能切出空检索词"
    for x in q:
        assert not x.startswith("的"), f"检索词以虚词开头: {x}"
        assert not x.startswith("想让"), f"检索词以虚词开头: {x}"
        assert "想做一个" not in x, f"检索词含虚词: {x}"
    # 关键实义词块要能切出来
    assert "引用关系图谱" in q
    assert "论文推荐系统" in q


def test_extract_queries_keeps_english_terms_before_chinese():
    """英文术语检索最准，必须排在中文块前面（build_candidates 按顺序消费）。"""
    q = w.extract_idea_queries("基于 LoRA 的大模型微调效率优化")
    assert q[0] == "LoRA"
    assert "大模型微调效率优化" in q
    # 「的」不能被带进中文块
    assert all(not x.startswith("的") for x in q)


def test_extract_queries_drops_short_generic_blocks():
    """2 字块（用户/改进/系统）太泛，检索只会把结果拉偏，应被丢掉。"""
    q = w.extract_idea_queries("用 attention mechanism 改进 Transformer 的长文档摘要")
    assert "改进" not in q
    assert "长文档摘要" in q


def test_extract_queries_blank_idea_returns_empty():
    assert w.extract_idea_queries("   ") == []


def test_format_reference_includes_arxiv_when_available():
    c = _cand("C1", "T", source="arxiv", arxiv_id="2101.00001")
    assert "arXiv:2101.00001" in w.format_reference(c)


def test_format_reference_falls_back_when_author_missing():
    c = _cand("C1", "T", authors=[], year=None)
    text = w.format_reference(c)
    assert "作者信息缺失" in text
    assert "n.d." in text


def test_long_author_list_is_truncated_with_et_al():
    """T5 有十几位作者，全列出来一条占三行，按 IEEE 惯例截到 6 位 + et al.。"""
    names = [f"Author{i} X" for i in range(9)]
    text = w.format_reference(_cand("C1", "T", authors=names))
    assert ", et al." in text
    assert "Author6" not in text  # 第 7 位之后应被截掉
    assert "Author5" in text  # 第 6 位保留


def test_short_author_list_is_kept_in_full():
    """不超过阈值时一位都不能少——截断只在超限时发生。"""
    names = [f"Author{i} X" for i in range(w.MAX_REF_AUTHORS)]
    text = w.format_reference(_cand("C1", "T", authors=names))
    assert "et al." not in text
    assert all(n in text for n in names)


def test_reference_does_not_produce_double_period():
    """"et al." 和 "n.d." 自带句点，再拼 "." 会成 "et al.."，读起来像笔误。"""
    many = w.format_reference(_cand("C1", "T", authors=[f"A{i} X" for i in range(9)]))
    assert ".." not in many
    no_year = w.format_reference(_cand("C2", "T", authors=["A B"], year=None))
    assert ".." not in no_year
    assert no_year.endswith(".")  # 但仍要以句点收尾


def test_local_authors_index_reads_structures(monkeypatch, tmp_path):
    """catalog.json 只有 authors_count，作者名得从 structures.json 补。"""
    import json

    from app import literature as lit

    fake = tmp_path / "structures.json"
    fake.write_text(
        json.dumps(
            {
                "a.pdf": {"authors": ["Alice", "Bob"]},
                "b.pdf": {"authors": []},
                "c.pdf": {"authors": ["  Carol  ", ""]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lit, "STRUCTURES_FILE", fake)
    idx = lit.load_local_authors()
    assert idx == {"a.pdf": ["Alice", "Bob"], "c.pdf": ["Carol"]}


def test_local_authors_index_returns_empty_when_file_missing(monkeypatch, tmp_path):
    """structures.json 缺失不能把整条检索链带崩，只降级为没有作者。"""
    from app import literature as lit

    monkeypatch.setattr(lit, "STRUCTURES_FILE", tmp_path / "nope.json")
    assert lit.load_local_authors() == {}


def test_norm_title_keeps_chinese_characters():
    """中文标题归一化后不能是空串，否则 _dedupe 的判重键全为空。

    旧实现 ``re.sub(r"[^0-9a-z]+", "", ...)`` 把汉字全删了，纯中文标题归一化
    得到 ''，而 _dedupe 里 ``if k`` 会把空 key 过滤掉 —— 判重对中文文献完全失效，
    同一篇论文被两个查询词各捞一次就进池两次，最终参考文献里出现三条一模一样的条目。
    """
    from app import literature as lit

    n = lit._norm_title("关于拟阵基图一个猜想的证明")
    assert n, "中文标题归一化后不能为空"
    assert "拟阵基图" in n
    # 全角/半角标点、多余空格不影响结果
    assert lit._norm_title("关于拟阵基图一个猜想的证明。") == n
    assert lit._norm_title("  关于拟阵基图一个猜想的证明  ") == n


def test_norm_title_handles_mixed_and_english_titles():
    from app import literature as lit

    # 中英混排：中文和英文都要保留，破折号变体归一后一致
    a = lit._norm_title("中高职一体化课程体系构建——基于技能图谱视角的实践探索")
    b = lit._norm_title("中高职一体化课程体系构建—基于技能图谱视角的实践探索")
    assert a == b and a
    # 英文标题大小写与空格归一
    assert lit._norm_title("Attention Is All You Need") == lit._norm_title("attention is all you need")


def test_dedupe_removes_duplicate_chinese_titles():
    """同一篇中文论文被多个查询词各捞一次，去重后只能留一条。"""
    from app import literature as lit

    def mk(title: str, source: str = lit.SOURCE_CROSSREF, score: float = 1.0) -> lit.Candidate:
        return lit.Candidate(key="", title=title, source=source, score=score)

    rows = [
        mk("关于拟阵基图一个猜想的证明"),
        mk("关于Riemann曲面长度谱的一个猜想"),
        mk("关于拟阵基图一个猜想的证明"),  # 重复
        mk("关于拟阵基图一个猜想的证明。"),  # 重复（只有标点不同）
        mk("中高职一体化课程体系构建——基于技能图谱视角的实践探索"),
        mk("中高职一体化课程体系构建—基于技能图谱视角的实践探索"),  # 重复
    ]
    out = lit._dedupe(rows)
    titles = [c.title for c in out]
    assert len(out) == 3, f"应去重到 3 条，实际 {len(out)}: {titles}"
    assert "关于拟阵基图一个猜想的证明" in titles
    assert "关于Riemann曲面长度谱的一个猜想" in titles
    assert len(out) == len({lit._norm_title(t) for t in titles}), "去重后仍有同键条目"


def test_dedupe_prefers_local_over_crossref():
    """本库条目能跳原文，优先级最高。"""
    from app import literature as lit

    rows = [
        lit.Candidate(key="", title="Same Paper Title", source=lit.SOURCE_CROSSREF),
        lit.Candidate(key="", title="same paper title", source=lit.SOURCE_LOCAL),
    ]
    out = lit._dedupe(rows)
    assert len(out) == 1
    assert out[0].source == lit.SOURCE_LOCAL


# --------------------------------------------------------------------------- 1.6.2 架构图


def test_strip_fence_removes_markdown_wrapper():
    assert dg.strip_fence("```mermaid\nflowchart TD\n A-->B\n```") == "flowchart TD\n A-->B"


def test_strip_fence_keeps_plain_code():
    assert dg.strip_fence("flowchart TD\n A-->B") == "flowchart TD\n A-->B"


def test_graphviz_font_injected_for_chinese():
    """不注入字体时 Graphviz 会把中文渲染成方框（实测已复现）。"""
    code = 'digraph G {\n  A [label="输入"];\n}'
    out = dg._ensure_font(code)
    assert "Microsoft YaHei" in out
    assert out.startswith("digraph G {")


def test_graphviz_font_not_overridden_when_author_specified():
    """模型自己指定了 fontname 时不应该再插一条去打架。"""
    code = 'digraph G {\n  node [fontname="SimSun"];\n}'
    assert dg._ensure_font(code) == code


def test_graphviz_font_leaves_invalid_code_alone():
    """不是 digraph 开头的乱码不能被插坏，否则错误信息会变得莫名其妙。"""
    assert dg._ensure_font("flowchart TD\n A-->B") == "flowchart TD\n A-->B"


def test_mermaid_font_injected_for_chinese():
    """Mermaid 走 Chromium，字体由 CSS 决定，不注入在 Linux 上就是一堆方框。

    实测：生产服务器 Ubuntu 24.04 未装中文字体时，节点文字全部渲染成 □□。
    """
    out = dg._ensure_mermaid_font("flowchart TD\n    A[输入] --> B[编码器]")
    assert out.startswith("%%{init:")
    assert "flowchart TD" in out


def test_mermaid_font_directive_is_valid_json():
    """fontFamily 的值自带引号，拼进 JSON 必须转义，否则 Mermaid 直接报语法错。

    回归背景：初版用 f-string 拼接产出 ``"fontFamily": ""Microsoft YaHei""``，
    这是非法 JSON，渲染时整条指令被丢弃。
    """
    out = dg._ensure_mermaid_font("flowchart TD\n A-->B")
    first = out.splitlines()[0]
    m = re.match(r"%%\{init: (.*)\}%%$", first)
    assert m, f"init 指令格式不对: {first}"
    cfg = json.loads(m.group(1))
    font = cfg["themeVariables"]["fontFamily"]
    # 值应当是完整的 CSS 字体族串，多个候选按顺序列出
    assert "Microsoft YaHei" in font
    assert "Noto Sans CJK SC" in font


def test_mermaid_font_not_injected_when_author_specified():
    """模型自己写了 init 主题就尊重它，不再插一条去打架。"""
    code = '%%{init: {"theme": "dark"}}%%\nflowchart TD\n A-->B'
    assert dg._ensure_mermaid_font(code) == code


def test_mermaid_font_injected_before_graph_keyword():
    """init 指令必须在图类型关键字之前，插到后面 Mermaid 不认。"""
    out = dg._ensure_mermaid_font("graph LR\n  A --> B")
    lines = out.splitlines()
    assert lines[0].startswith("%%{init:")
    assert lines[1].startswith("graph LR")


def test_prompt_differs_between_renderers():
    mm = dg.build_prompt("a system", "mermaid")[1]["content"]
    gv = dg.build_prompt("a system", "graphviz")[1]["content"]
    assert "Mermaid" in mm and "Graphviz" not in mm
    assert "Graphviz" in gv and "fontsize" in gv


def test_prompt_rejects_unknown_renderer():
    with pytest.raises(ValueError):
        dg.build_prompt("x", "plantuml")


def test_tikz_prompt_forbids_document_wrapper():
    """TikZ 的 preamble 由代码补，模型不该写 \\documentclass，
    否则我们只得原样返回它的整份文档，出错面就变大了。"""
    tk = dg.build_prompt("a system", "tikz")[1]["content"]
    assert "TikZ" in tk
    assert "tikzpicture" in tk
    assert "不要写 \\documentclass" in tk


def test_tikz_preamble_is_added_around_bare_environment():
    src = dg.build_tikz_source("\\begin{tikzpicture}\n\\node {A};\n\\end{tikzpicture}")
    assert src.startswith("\\documentclass")
    assert "\\usepackage{tikz}" in src
    assert "ctex" in src               # 没有 ctex 中文节点会编译失败
    assert src.rstrip().endswith("\\end{document}")


def test_tikz_body_without_environment_is_wrapped():
    """模型只给了环境体（漏掉 \\begin{tikzpicture}）也要能编译。"""
    src = dg.build_tikz_source("\\node[box] (a) {输入};")
    assert "\\begin{tikzpicture}" in src
    assert "\\end{tikzpicture}" in src


def test_tikz_full_document_is_not_double_wrapped():
    """模型已经写了完整文档时不要再包一层，否则两个 \\documentclass 直接编译失败。"""
    code = "\\documentclass{article}\n\\begin{document}\n\\begin{tikzpicture}\n\\end{tikzpicture}\n\\end{document}\n"
    assert dg.build_tikz_source(code) == code


def test_tikz_render_reports_missing_latex(monkeypatch, tmp_path):
    """本机没有 pdflatex 时必须回可读错误，不能抛异常。"""
    monkeypatch.setattr(dg, "_which", lambda name: None)
    png, err = dg.render_tikz("\\begin{tikzpicture}\\end{tikzpicture}", tmp_path)
    assert png is None
    assert "pdflatex" in err


def test_renderer_available_reports_tikz():
    av = dg.renderer_available()
    assert "tikz" in av and "mermaid" in av and "graphviz" in av
    # 赛题 1.6.2 点名四种格式，matplotlib 不能漏
    assert "matplotlib" in av


def test_matplotlib_prompt_injects_out_variable():
    """脚本必须把图存到系统注入的 OUT，否则我们收不到图。"""
    mp = dg.build_prompt("a system", "matplotlib")[1]["content"]
    assert "Matplotlib" in mp
    assert "OUT" in mp
    assert "plt.show()" in mp          # 得明确禁止，否则无显示环境里卡住
    assert "savefig" in mp


def test_matplotlib_render_outputs_png(tmp_path):
    code = (
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.plot([1, 2, 3], [3, 2, 4])\n"
        "fig.savefig(OUT, dpi=100)\n"
    )
    png, err = dg.render_matplotlib(code, tmp_path)
    if not dg.renderer_available()["matplotlib"]:
        pytest.skip("本机没装 matplotlib")
    assert err == ""
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_matplotlib_render_reports_script_error(tmp_path):
    """脚本报错必须返回 None + 可读错误，不能假装成功。"""
    if not dg.renderer_available()["matplotlib"]:
        pytest.skip("本机没装 matplotlib")
    png, err = dg.render_matplotlib("raise RuntimeError('boom')\n", tmp_path)
    assert png is None
    assert "boom" in err


def test_matplotlib_render_flushes_stale_image(tmp_path):
    """上一张图残留在同一目录时，失败不能把它当成本次结果。"""
    if not dg.renderer_available()["matplotlib"]:
        pytest.skip("本机没装 matplotlib")
    (tmp_path / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\nstale")
    png, err = dg.render_matplotlib("raise RuntimeError('boom')\n", tmp_path)
    assert png is None and "boom" in err


def test_matplotlib_render_strips_bom(tmp_path):
    """BOM 会让 Python 报 invalid non-printable character。"""
    if not dg.renderer_available()["matplotlib"]:
        pytest.skip("本机没装 matplotlib")
    code = "\ufeffimport matplotlib.pyplot as plt\nfig = plt.figure()\nfig.savefig(OUT, dpi=80)\n"
    png, err = dg.render_matplotlib(code, tmp_path)
    assert err == ""
    assert png is not None


def test_radar3d_option_has_three_axes():
    """三维雷达正好 3 个指标，多给的列要截掉，否则画上去挤。"""
    df = ch.load_csv("a,b,c,d\n1,2,3,4\n2,3,4,5\n3,4,5,6\n")
    opt = ch.build_echarts(df, {"kind": "radar3d", "x": "", "y": "a,b,c,d"})
    assert [i["name"] for i in opt["radar"]["indicator"]] == ["a", "b", "c"]
    assert opt["radar"]["shape"] == "polygon"


def test_radar3d_falls_back_when_less_than_three_axes():
    """少于 3 个数值列时不能硬画三角雷达，退化成普通雷达。"""
    df = ch.load_csv("a,b\n1,2\n2,3\n3,4\n")
    opt = ch.build_echarts(df, {"kind": "radar3d", "x": "", "y": "a,b"})
    assert [i["name"] for i in opt["radar"]["indicator"]] == ["a", "b"]


def test_radar3d_export_is_a_png():
    df = ch.load_csv("x,y,z\n1,2,3\n2,4,6\n3,6,9\n")
    png = ch.export_png(df, {"kind": "radar3d", "x": "", "y": "x,y,z"}, "三维")
    assert png.startswith(b"\x89PNG")


def test_find_paper_figures_returns_empty_for_unknown_paper():
    assert dg.find_paper_figures("no-such-paper-xyz") == []


def test_find_paper_figures_hits_transformer_architecture():
    """库里 1706.03762 的架构图 caption 就是 "The Transformer - model architecture."。"""
    figs = dg.find_paper_figures("1706.03762v7.pdf", keyword="architecture")
    assert figs, "应该能找到 Transformer 架构图"
    assert all("/paper/" in f["image_url"] for f in figs)
    assert any("architecture" in f["caption"].lower() for f in figs)


# --------------------------------------------------------------------------- 1.6.3 数据可视化


def test_load_csv_decodes_gbk():
    """Excel 导出的中文 CSV 常是 GBK，按 utf-8 读会崩。"""
    raw = "模型,得分\nBERT,88\nGPT,91\n".encode("gbk")
    df = ch.load_csv(raw)
    assert list(df.columns) == ["模型", "得分"]
    assert len(df) == 2


def test_load_csv_truncates_to_max_rows():
    big = "a,b\n" + "\n".join(f"{i},{i * 2}" for i in range(ch.MAX_ROWS + 50))
    assert len(ch.load_csv(big)) == ch.MAX_ROWS


def test_profile_finds_correlations():
    df = ch.load_csv("x,y\n1,10\n2,20\n3,30\n")
    info = ch.profile(df)
    assert info["numeric"] == ["x", "y"]
    assert info["correlations"][0]["r"] == pytest.approx(1.0)


def test_choose_chart_monotonic_column_becomes_axis():
    """params 递增那种数据本质是自变量-因变量，不该选雷达图。"""
    df = ch.load_csv("params,acc,loss\n1,0.42,2.1\n2,0.55,1.8\n4,0.68,1.4\n8,0.77,1.1\n")
    spec = ch.choose_chart(ch.profile(df), df)
    assert spec["kind"] == "line"
    assert spec["x"] == "params"
    assert "params" not in spec["y"], "自变量不能同时当因变量"


def test_choose_chart_category_plus_value_is_bar():
    df = ch.load_csv("model,score\nBERT,88\nGPT,91\nT5,86\n")
    assert ch.choose_chart(ch.profile(df), df)["kind"] == "bar"


def test_choose_chart_many_categories_switch_to_line():
    rows = "\n".join(f"e{i},{i}" for i in range(20))
    df = ch.load_csv("epoch,acc\n" + rows)
    assert ch.choose_chart(ch.profile(df), df)["kind"] == "line"


def test_choose_chart_pure_numeric_matrix_is_radar():
    df = ch.load_csv("m1,m2,m3,m4\n0.8,0.6,0.9,0.7\n0.5,0.85,0.4,0.95\n")
    assert ch.choose_chart(ch.profile(df), df)["kind"] == "radar"


def test_choose_chart_all_text_is_pie():
    df = ch.load_csv("label\nA\nB\nA\n")
    assert ch.choose_chart(ch.profile(df), df)["kind"] == "pie"


def test_needs_log_for_wide_positive_range():
    import pandas as pd

    assert ch._needs_log(pd.Series([0.34, 175, 540])) is True
    assert ch._needs_log(pd.Series([86.7, 88.4, 93.1])) is False
    assert ch._needs_log(pd.Series([0, 1, 100])) is False, "含 0 不能取对数"


def test_radar_normalizes_each_dimension():
    """雷达图各维度量纲不同，不归一化会让大数值维度吃掉整个图。"""
    df = ch.load_csv("m1,m2,m3\n1,1000,0.1\n3,3000,0.3\n")
    opt = ch.build_echarts(df, {"kind": "radar", "x": "", "y": "m1,m2,m3"})
    values = opt["series"][0]["data"][0]["value"]
    assert all(0 <= v <= 1 for v in values), values


def test_scatter_option_shape():
    df = ch.load_csv("a,b\n1,2\n3,4\n")
    opt = ch.build_echarts(df, {"kind": "scatter", "x": "a", "y": "b"})
    assert opt["series"][0]["type"] == "scatter"
    assert opt["xAxis"]["name"] == "a"


def test_scatter_uses_log_axis_when_range_is_wide():
    """参数 0.34~540 跨 1600 倍，线性轴会把小模型全挤在原点。

    export_png 早就按 _needs_log 切了对数轴，前端预览原来没切，
    导致同一份数据网页上挤成一坨、下载的 PNG 却舒展——两边必须一致。
    """
    df = ch.load_csv("params,acc\n0.34,88.4\n175,91.2\n540,93.1\n")
    opt = ch.build_echarts(df, {"kind": "scatter", "x": "params", "y": "acc"})
    assert opt["xAxis"]["type"] == "log"
    assert "log scale" in opt["xAxis"]["name"]


def test_scatter_keeps_linear_axis_for_narrow_range():
    """88.4~93.1 只差 5%，不该用对数轴——那会误导读者以为差距很大。"""
    df = ch.load_csv("params,acc\n175,91.2\n540,93.1\n")
    opt = ch.build_echarts(df, {"kind": "scatter", "x": "params", "y": "acc"})
    assert opt["xAxis"]["type"] == "value"


def test_line_log_axis_on_y_only():
    """折线图的 x 是类别，只有 y 可能跨数量级。"""
    df = ch.load_csv("epoch,loss\n1,1200\n2,95\n3,8\n")
    opt = ch.build_echarts(df, {"kind": "line", "x": "epoch", "y": "loss"})
    assert opt["xAxis"]["type"] == "category"
    assert opt["yAxis"]["type"] == "log"


def test_bar_log_axis_skipped_when_zero_present():
    df = ch.load_csv("model,cnt\nA,0\nB,5000\nC,12000\n")
    opt = ch.build_echarts(df, {"kind": "bar", "x": "model", "y": "cnt"})
    assert opt["yAxis"]["type"] == "value", "含 0 不能取对数"


def test_padded_range_hugs_data():
    """y 轴要贴着数据，不能像 ECharts 默认那样从 0 起。

    准确率 86.7~93.1 若从 0 画起，五个点全挤在顶部 7% 的窄条里；
    而 matplotlib 那张是自适应的，两张图并排看着不像同一份数据。
    """
    r = ch._padded_range([86.7, 88.4, 91.2, 92.5, 93.1])
    assert r["min"] > 86.0, r
    assert r["max"] < 94.0, r
    assert r["min"] < 86.7 and r["max"] > 93.1, "数据点必须全部落在范围内"


def test_padded_range_handles_constant_series():
    """全部相同的值时 span 为 0，不能除出 NaN 或上下界相等。"""
    r = ch._padded_range([5, 5, 5])
    assert r["min"] < 5 < r["max"]


def test_padded_range_ignores_nan():
    import math

    r = ch._padded_range([1, float("nan"), 3])
    assert not math.isnan(r["min"])


def test_scatter_y_axis_not_pinned_to_zero():
    df = ch.load_csv("params,acc\n0.34,88.4\n175,91.2\n540,93.1\n")
    opt = ch.build_echarts(df, {"kind": "scatter", "x": "params", "y": "acc"})
    assert opt["yAxis"]["min"] > 80, opt["yAxis"]


def test_export_png_produces_png_bytes():
    df = ch.load_csv("model,score\nBERT,88\nGPT,91\n")
    png = ch.export_png(df, {"kind": "bar", "x": "model", "y": "score"})
    assert png.startswith(b"\x89PNG")
    assert len(png) > 1000


def test_export_png_handles_comma_separated_y():
    """``y`` 是逗号分隔多列时也必须出图。

    choose_chart 给折线/柱状图的就是 ``"train_loss,val_loss,accuracy"`` 这种多列写法。
    旧实现直接 ``df[y]``，多列时 ``y in df.columns`` 为假、返回空列表，matplotlib 报
    ``x and y must have same first dimension, but have shapes (8,) and (0,)``，
    PNG 静默失败、png_url 为空 —— 用户勾了柱状图，导出的图却不存在。
    """
    df = ch.load_csv(
        "epoch,train_loss,val_loss,accuracy\n"
        "1,2.31,2.28,0.512\n2,1.87,1.92,0.634\n3,1.52,1.71,0.702\n"
    )
    for kind in ("line", "bar"):
        png = ch.export_png(df, {"kind": kind, "x": "epoch", "y": "train_loss,val_loss,accuracy"})
        assert png.startswith(b"\x89PNG"), f"{kind} 没出图"
        assert len(png) > 1000, f"{kind} 图太小"


def test_series_takes_first_column_of_multi_y():
    df = ch.load_csv("epoch,a,b\n1,2.0,9.0\n2,3.0,8.0\n")
    xs, ys = ch._series(df, "epoch", "a,b")
    assert xs == [1, 2]
    assert ys == [2.0, 3.0]
    # 不存在的列返回空，而不是抛异常
    assert ch._series(df, "epoch", "nope")[1] == []


def test_all_chart_kinds_export_png():
    """六种图型逐个出图，任何一个是空的都说明导出链路断了。"""
    png_magic = bytes([0x89]) + b"PNG"
    df = ch.load_csv(
        "epoch,train_loss,val_loss,accuracy\n"
        "1,2.31,2.28,0.512\n2,1.87,1.92,0.634\n3,1.52,1.71,0.702\n"
        "4,1.28,1.65,0.741\n5,1.09,1.66,0.768\n"
    )
    info = ch.profile(df)
    for kind in ch.CHART_KINDS:
        spec = ch.choose_chart(info, df)
        spec["kind"] = kind
        if kind in ("radar", "radar3d"):
            nums = info.get("numeric", [])
            spec["y"] = ",".join(nums[:3] if kind == "radar3d" else nums)
        png = ch.export_png(df, spec)
        assert png[:4] == png_magic, f"{kind} 没出图"
        assert len(png) > 1000, f"{kind} 图太小"


def test_key_numbers_are_preformatted_strings():
    """关键数值必须摊平成可整段复制的字符串。

    动机：实测模型把 stats 里的 min=0.34 / max=540 拼成了 "0.348 to 5408"。
    不方便复制的嵌套结构是这种错误的温床。
    """
    df = ch.load_csv("params,acc\n0.34,88.4\n175,91.2\n540,93.1\n")
    info = ch.profile(df)
    spec = {"kind": "scatter", "x": "params", "y": "acc"}
    keys = ch._key_numbers(info, spec)
    assert "params.min = 0.34" in keys
    assert "params.max = 540.0" in keys or "params.max = 540" in keys
    # 行数这一项必须是英文键：caption 是英文，模型会原样照抄这几个词
    assert "row_count = 3" in keys


def test_key_numbers_contain_no_chinese():
    """关键数值会成为 caption 的一部分，不能含中文。"""
    df = ch.load_csv("name\nAlice\nBob\nCarol\n")  # 没有数值列，stats 为空
    info = ch.profile(df)
    keys = ch._key_numbers(info, {"kind": "pie", "x": "name", "y": ""})
    joined = " ".join(keys)
    assert not any("\u4e00" <= ch2 <= "\u9fff" for ch2 in joined), joined


def test_unsupported_numbers_accepts_values_from_stats():
    df = ch.load_csv("params,acc\n0.34,88.4\n175,91.2\n540,93.1\n")
    info = ch.profile(df)
    spec = {"kind": "scatter", "x": "params", "y": "acc"}
    bad = ch.unsupported_numbers("Accuracy ranges from 0.34 to 540 across 3 models.", info, spec)
    assert bad == [], bad


def test_unsupported_numbers_flags_glued_digits():
    """0.348 / 5408 这种被拆开又重组的数必须被标出来。"""
    df = ch.load_csv("params,acc\n0.34,88.4\n175,91.2\n540,93.1\n")
    info = ch.profile(df)
    spec = {"kind": "scatter", "x": "params", "y": "acc"}
    bad = ch.unsupported_numbers("Ranging from 0.348 to 5408 parameters.", info, spec)
    assert "0.348" in bad
    assert "5408" in bad


def test_canon_number_keeps_trailing_zero_of_integers():
    """540 不能被裁成 54。

    最初用 ``n.rstrip("0")`` 做归一化，结果 caption 里正确的 "540"
    被削成 "54" 从而判成可疑数字——归一化本身造出了假警报。
    """
    assert ch._canon_num("540") == "540"
    assert ch._canon_num("540.0") == "540"
    assert ch._canon_num("540.00") == "540"
    assert ch._canon_num("0.340") == "0.34"
    assert ch._canon_num("100") == "100"


# --------------------------------------------------------------------------- 1.6.4 中译英


def test_chunks_do_not_split_sentences():
    text = "\n\n".join(["这是第一段。" * 20, "第二段较短。", "这是第三段。" * 30])
    chunks = z._chunks(text, size=300)
    assert len(chunks) > 1
    assert all(c.rstrip().endswith("。") for c in chunks)


def test_chunks_hard_split_overlong_paragraph():
    out = z._chunks("无标点" * 200, size=100)
    assert len(out) > 1
    assert all(len(c) <= 100 for c in out)


def test_parse_glossary_supports_both_separators():
    g = z.parse_glossary("注意力机制=attention mechanism\nTransformer, Transformer\n# 注释\n坏行")
    assert g == {"注意力机制": "attention mechanism", "Transformer": "Transformer"}


def test_glossary_text_empty_when_no_terms():
    assert z._glossary_text({}) == ""


def test_glossary_text_lists_every_term():
    t = z._glossary_text({"残差连接": "residual connection"})
    assert "残差连接" in t and "residual connection" in t


# --------------------------------------------------------------------------- 1.6.1 正文标题与引用标注


def test_normalize_headings_adds_hashes_to_bare_section_names():
    # 实测模型经常只写裸标题，前端只认 `^## `，不补井号整篇就没有章节结构
    text = (
        "Abstract\n正文一段。\n\nIntroduction\n正文二段。\n\n"
        "Related Work\n### 子节\n正文。\n\nReferences\n[C1]\n[C2]"
    )
    out = w.normalize_headings(text)
    assert "## Abstract" in out
    assert "## Introduction" in out
    assert "## Related Work" in out
    assert "## References" in out
    # 子节本来就是 ### ，不能被改成 ##
    assert "### 子节" in out


def test_normalize_headings_leaves_inline_mentions_alone():
    # 正文里提到 Abstract 不算标题：只有独占一行才补
    text = "本文的 Abstract 部分写了问题陈述。\nIntroduction 通常从背景讲起。"
    assert w.normalize_headings(text) == text


def test_normalize_headings_does_not_double_prefix():
    text = "## Abstract\n正文。\n## Introduction\n正文。"
    assert w.normalize_headings(text) == text


def test_normalize_headings_empty_text():
    assert w.normalize_headings("") == ""


def test_annotate_appends_title_to_every_marker():
    pool = [_cand("C1", "HierDoc: Hierarchical Page-to-Region Evidence Routing")]
    out = w.annotate_citations("该方法使用层级路由 [C1]。", pool)
    assert "[C1·HierDoc]" in out


def test_annotate_keeps_group_markers_together():
    pool = [_cand("C1", "RWKV: Reinventing RNNs"), _cand("C2", "R-Transformer: Recurrent")]
    out = w.annotate_citations("线性注意力 [C1, C2] 都改了主干。", pool)
    assert "[C1·RWKV, C2·R-Transformer]" in out


def test_annotate_uses_filename_for_library_papers():
    # 本库论文带 local_file，文件名是唯一能落到本地 PDF 的标识，要一并摊出来
    pool = [_cand("C1", "Attention Is All You Need", local_file="1706.03762v1.pdf")]
    out = w.annotate_citations("自注意力 [C1]。", pool)
    assert "[C1·Attention Is·1706.03762v1]" in out


def test_annotate_picks_short_word_for_short_english_title():
    pool = [_cand("C1", "GPT-4")]
    assert "[C1·GPT-4]" in w.annotate_citations("大模型 [C1]。", pool)


def test_annotate_truncates_chinese_title_without_a_colon():
    # 中文标题没有空格，按词数截等于不截，必须按字数截
    pool = [_cand("C1", "基于知识图谱约束与检索增强生成协同的大模型工程造价智能生成方法研究")]
    out = w.annotate_citations("相关研究 [C1]。", pool)
    assert "…" in out
    assert len(out) < 60


def test_annotate_unknown_key_is_left_as_bare_number():
    # 池外编号不能编名字出来，保持纯编号，前端会染色划掉
    out = w.annotate_citations("幻觉引用 [C9]。", [_cand("C1", "A")])
    assert "[C9]" in out


def test_annotate_leaves_reference_section_untouched():
    # References 节紧跟着代码生成的真实条目，再摊一遍名字是纯噪音
    pool = [_cand("C1", "HierDoc: Routing")]
    text = "正文 [C1]。\n\n## References\n[C1]"
    out = w.annotate_citations(text, pool)
    head, _, tail = out.partition("## References")
    assert "[C1·HierDoc]" in head
    assert tail.strip() == "[C1]"


def test_annotate_is_idempotent():
    pool = [_cand("C1", "HierDoc: Routing")]
    once = w.annotate_citations("正文 [C1]。", pool)
    assert w.annotate_citations(once, pool) == once


def test_annotate_empty_text():
    assert w.annotate_citations("", [_cand("C1", "A")]) == ""


def test_prompt_requires_refs_to_support_the_claim():
    # 编号合法但论断对不上的错配，只能靠 prompt 约束，这条把要求钉住
    assert "编号必须与论断对得上" in w.FRAMEWORK_SYSTEM
    assert "典型错配" in w.FRAMEWORK_SYSTEM


def test_prompt_mandates_output_language_follows_idea():
    assert "正文语言跟随【研究 idea】的语言" in w.FRAMEWORK_SYSTEM


async def _fake_chat(messages, temperature=0.3, max_tokens=None):
    return (
        "Abstract\n本文研究长文档问答。\n\nIntroduction\n自注意力带来开销 [C1]。\n\n"
        "Related Work\n### 效率优化\n分块计算降低显存 [C1]。\n\nReferences\n[C1]"
    )


def test_generate_framework_outputs_headings_and_annotated_citations(monkeypatch):
    # 端到端：模型给的是裸标题 + 纯编号，返回给前端的必须是 ## 标题 + 带名引用
    pool = [_cand("C1", "HierDoc: Hierarchical Routing", local_file="1706.03762v1.pdf")]
    monkeypatch.setattr(w.lit, "build_candidates", lambda *_a, **_k: pool)

    async def _no_english(_q):
        return None

    monkeypatch.setattr(w, "_english_queries", _no_english)
    monkeypatch.setattr(w, "chat", _fake_chat)

    async def run():
        return await w.generate_framework("长文档问答中的稀疏注意力")

    out = asyncio.run(run())
    md = out["markdown"]
    assert "## Abstract" in md and "## Introduction" in md and "### 效率优化" in md
    assert "[C1·HierDoc·1706.03762v1]" in md
    # References 节保持纯编号
    assert md.rstrip().endswith("[C1]")
    # 校验结果按改写前的原文算，不能因为正文多了名字就漏判
    assert out["check"]["cited_keys"] == ["C1"]
    assert out["check"]["unknown_keys"] == []


# ---------------- 引用句回译（「读原文」定位用） ----------------

def test_strip_claim_markers_removes_numbers_and_markdown():
    """回译前要把 [C1] 和行首的 ## / - / 1. 剥掉，否则模型会把编号当正文译出去。"""
    assert w._strip_claim_markers("自注意力开销大 [C1]。") == "自注意力开销大 。"
    assert w._strip_claim_markers("## 效率优化") == "效率优化"
    assert w._strip_claim_markers("- 分块计算降低显存[C12·Flash]") == "分块计算降低显存"


def test_claim_to_english_rejects_fragment(monkeypatch):
    """太短的残句不值得花一次模型调用，直接返回空串走标题兜底。"""

    async def _boom(*_a, **_k):
        raise AssertionError("残句不该调用模型")

    monkeypatch.setattr(w, "chat", _boom)
    assert asyncio.run(w.claim_to_english("[C1]")) == ""
    assert asyncio.run(w.claim_to_english("")) == ""


def test_claim_to_english_takes_first_line(monkeypatch):
    """模型多回了开场白时只取第一行，别把整段塞进 PDF 搜索词。"""

    async def _chat(messages, temperature=0.3, max_tokens=None):
        assert temperature == 0.0, "回译要确定性，温度不能顺着默认值走"
        return '"Self-attention allows direct dependencies between positions."\n说明：以上是译文'

    monkeypatch.setattr(w, "chat", _chat)
    got = asyncio.run(w.claim_to_english("自注意力允许位置之间建立直接依赖 [C1]"))
    assert got == "Self-attention allows direct dependencies between positions."


def test_claim_to_english_returns_empty_when_model_echoes_chinese(monkeypatch):
    """模型没翻译、原样回了中文时，必须返回空串而不是把中文当英语去搜。"""

    async def _chat(messages, temperature=0.3, max_tokens=None):
        return "自注意力允许位置之间建立直接依赖"

    monkeypatch.setattr(w, "chat", _chat)
    assert asyncio.run(w.claim_to_english("自注意力允许位置之间建立直接依赖 [C1]")) == ""


def test_claim_to_english_never_raises(monkeypatch):
    """翻译失败不能断掉「读原文」整条链路，只能降级成标题定位。"""

    async def _boom(*_a, **_k):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(w, "chat", _boom)
    assert asyncio.run(w.claim_to_english("自注意力允许位置之间建立直接依赖 [C1]")) == ""


def test_claim_to_english_prompt_demands_original_wordings():
    """提示词必须要求贴近原论文措辞，否则回译出来的英文跟原文对不上。"""
    sys_prompt = (w._CLAIM_TO_EN_SYSTEM or "").lower()
    assert "措辞" in w._CLAIM_TO_EN_SYSTEM or "wording" in sys_prompt
    assert "英文" in w._CLAIM_TO_EN_SYSTEM