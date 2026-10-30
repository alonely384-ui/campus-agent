"""1.6 写作助手的测试：框架生成 / 架构图 / 数据可视化 / 中译英。

## 这里测什么、不测什么

**不测**：LLM 输出质量、渲染器渲染质量。这两件事每次跑都不一样，
写进测试只会变成不稳定用例，且改不了文案就红一片。

**测**：所有**确定性逻辑**——引用校验、去重、分块、选图、字体注入。
这些才是会悄悄出错、而且出错了用户看不出来的地方。
"""

from __future__ import annotations

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
    assert "样本行数 = 3" in keys


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