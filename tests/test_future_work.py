"""A-2 未来工作探索（赛题 1.5.4）的测试。

这里锁的都是「宁可保守也不能吹」的地方：
    · 抽不到作者原话就如实记 no_section，不许编句子；
    · 方向必须带得出作者原话和图谱依据，两者对不上的坑（节点 id vs 文件名）要拦住；
    · 相似性判断只有三种结论，任何路径都不许出现「原创」类断言；
    · 外部检索失败、模型异常、预算耗尽都要能走到 done，不能让整条流挂死。
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from app import future_work

# --------------------------------------------------------------------------- 造数据


def _graph(nodes):
    return {"nodes": nodes}


def _node(nid, title, role="cornerstone", cited_by=1, cites=1, copies=None, community=0):
    return {
        "id": nid,
        "title": title,
        "role": role,
        "cited_by": cited_by,
        "cites": cites,
        "copies": copies or [],
        "community": community,
        "betweenness": 0.25,
    }


def _extraction(source, status="success", sentences=None, kind="future_work", section="Conclusion"):
    return {
        "source": source,
        "title": source,
        "status": status,
        "kind": kind,
        "section": section,
        "sentences": sentences or [],
    }


def _collect(events):
    async def run():
        return [e async for e in events]

    return asyncio.run(run())


# --------------------------------------------------------------------------- 抽取状态


def test_extract_without_sections_reports_no_section():
    """章节树里没有候选节，必须如实标 no_section，不能抓几句正文凑数。"""
    out = future_work.extract_future_work("x.pdf", {"title": "X", "sections": []}, {1: "hello world"})
    assert out["status"] == "no_section"
    assert out["sentences"] == []


def test_extract_marks_parse_error_without_raising():
    """structure 是脏数据也不能抛，异常要变成 parse_error。"""
    out = future_work.extract_future_work("x.pdf", {"sections": "not-a-list"}, {})
    assert out["status"] in ("parse_error", "no_section")


def test_harvest_rejects_email_and_reference_noise():
    """页眉邮箱残留、参考文献编号残留都不算作者自述——这是真实库上踩到的坑。"""
    body = (
        "hinton@cs.toronto.edu tion cannot be applied to online learning tasks or to extremely "
        "large distributed models where the minibatches have to be small. "
        "[2015] in that each passage is limited to 4 sentences. "
        "We plan to extend the Transformer to problems involving other modalities."
    )
    picked = future_work._harvest_sentences(body)
    assert all("@" not in s for s in picked), picked
    assert all("[" not in s for s in picked), picked
    assert any("We plan to extend" in s for s in picked), picked


def test_extraction_summary_counts_every_status():
    items = [
        _extraction("a.pdf", "success", ["We plan to do X."]),
        _extraction("b.pdf", "no_section"),
        _extraction("c.pdf", "no_valid_sentence"),
        _extraction("d.pdf", "parse_error"),
    ]
    s = future_work.extraction_summary(items)
    assert s["total"] == 4
    assert s["counts"] == {
        "success": 1,
        "no_section": 1,
        "no_valid_sentence": 1,
        "parse_error": 1,
    }
    assert s["coverage"] == 0.25
    assert s["sentence_total"] == 1


# --------------------------------------------------------------------------- 方向归纳


def test_direction_matches_evidence_by_filename_not_node_id():
    """图谱节点 id 是 arxiv:xxx，extractions 的键是文件名，必须走 copies 才能对上。

    这是真实库上出现「5 个方向全是证据 0 条」的根因，锁死它。
    """
    g = _graph([_node("arxiv:1706.03762", "Attention Is All You Need", role="bridge", copies=["1706.03762v7.pdf"])])
    ex = [_extraction("1706.03762v7.pdf", sentences=["We plan to extend the Transformer."])]
    dirs = future_work.build_directions(g, ex)
    assert len(dirs) == 1
    d = dirs[0]
    assert d["anchor"] == "arxiv:1706.03762"
    assert d["anchor_file"] == "1706.03762v7.pdf"
    assert d["evidence"] == ["We plan to extend the Transformer."]
    assert d["evidence_status"] == "success"


def test_direction_uses_community_field_not_cluster():
    """簇号字段叫 community；读错键会退化成问号。"""
    g = _graph([_node("arxiv:1", "P", role="bridge", copies=["1.pdf"], community=3)])
    dirs = future_work.build_directions(g, [])
    assert "簇 3" in dirs[0]["basis"]
    assert "?" not in dirs[0]["basis"]


def test_direction_without_evidence_is_honest():
    """对不上的论文不编句子，如实留空并带上原因。"""
    g = _graph([_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])])
    dirs = future_work.build_directions(g, [_extraction("1.pdf", status="no_section")])
    # 只取 success 的，所以这里查不到证据
    assert dirs[0]["evidence"] == []
    assert dirs[0]["evidence_status"] == "no_section"
    assert "没有引用它" in dirs[0]["basis"] or "中介中心性" in dirs[0]["basis"]


def test_directions_prefer_papers_with_author_future_work():
    """有作者原话的方向排在前面，普通 Conclusion 次之，没证据的最后。"""
    g = _graph(
        [
            _node("arxiv:a", "AAA", role="bridge", copies=["a.pdf"]),
            _node("arxiv:b", "BBB", role="bridge", copies=["b.pdf"]),
            _node("arxiv:c", "CCC", role="bridge", copies=["c.pdf"]),
        ]
    )
    ex = [
        _extraction("a.pdf", sentences=["We plan A."], kind="conclusion"),
        _extraction("c.pdf", sentences=["We plan C."], kind="future_work"),
    ]
    dirs = future_work.build_directions(g, ex)
    titles = [d["anchor_title"] for d in dirs]
    assert titles[0] == "CCC", titles   # 明确写了 Future Work 的排第一
    assert titles[1] == "AAA", titles   # 普通 Conclusion 其次
    assert titles[2] == "BBB", titles   # 没证据的垫底


def test_build_directions_respects_limit():
    g = _graph([_node(f"arxiv:{i}", f"P{i}", role="bridge", copies=[f"{i}.pdf"]) for i in range(8)])
    dirs = future_work.build_directions(g, [], limit=3)
    assert len(dirs) == 3


def test_empty_graph_yields_no_directions():
    assert future_work.build_directions(_graph([]), []) == []


def _two_theme_nodes():
    """两篇论文各属不同主题（主题来自 node_meta，按 arxiv_id 查 catalog）。

    刻意用两种缺口类型（bridge + isolated）：同类缺口有配额（MAX_PER_GAP_KIND），
    两个都造 bridge 的话第二个会被配额挤掉，测不到主题过滤本身。
    """
    return _graph(
        [
            _node("arxiv:1706.03762", "Attention Is All You Need", role="bridge", copies=["a.pdf"]),
            _node("arxiv:2501.12948", "DeepSeek-R1", role="isolated", cited_by=0, cites=0, copies=["r.pdf"]),
        ]
    )


def test_directions_filter_by_theme():
    """主题自选：只保留锚点论文属于所选主题的方向。"""

    def fake_meta(canonical_id, title, arxiv_id=""):
        # 按节点 id 判主题：真实 node_meta 会从 canonical_id 里拆 arxiv 号去查 catalog，
        # 桩里不能依赖 arxiv_id 参数（调用方三个位置参数，第三个通常是空串）。
        theme = "模型架构" if "1706.03762" in canonical_id else "对齐与推理"
        return {"year": 2020, "category": theme, "abstract": ""}

    with mock.patch("app.graph.node_meta", fake_meta):
        graph = _two_theme_nodes()
        all_dirs = future_work.build_directions(graph, [])
        only = future_work.build_directions(graph, [], themes=["对齐与推理"])

    assert {d["theme"] for d in all_dirs} == {"模型架构", "对齐与推理"}
    assert len(only) == 1
    assert only[0]["theme"] == "对齐与推理"
    assert only[0]["anchor_title"] == "DeepSeek-R1"


def test_empty_themes_means_all_themes():
    """不勾主题 = 全部主题，与「不勾论文 = 全库」同一口径，不能变成「一个都不要」。"""
    fake = lambda *a, **k: {"year": 2020, "category": "模型架构", "abstract": ""}  # noqa: E731
    with mock.patch("app.graph.node_meta", fake):
        graph = _two_theme_nodes()
        assert future_work.build_directions(graph, [], themes=None) == future_work.build_directions(graph, [])
        assert future_work.build_directions(graph, [], themes=[]) == future_work.build_directions(graph, [])


def test_unknown_theme_yields_empty_not_fallback():
    """勾了个库里没有的主题：如实返回空，不能悄悄放宽成全库让用户以为筛选生效了。"""
    fake = lambda *a, **k: {"year": 2020, "category": "模型架构", "abstract": ""}  # noqa: E731
    with mock.patch("app.graph.node_meta", fake):
        assert future_work.build_directions(_two_theme_nodes(), [], themes=["不存在的主题"]) == []


async def test_direction_events_reports_empty_theme_choice():
    fake = lambda *a, **k: {"year": 2020, "category": "模型架构", "abstract": ""}  # noqa: E731
    events = []
    with mock.patch("app.graph.node_meta", fake):
        async for ev in future_work.direction_events(_two_theme_nodes(), [], themes=["不存在的主题"]):
            events.append(ev)
    errors = [e for e in events if e["type"] == "error"]
    assert errors, events
    assert "不存在的主题" in errors[0]["message"]
    assert events[-1]["type"] == "done"


def test_gap_kinds_do_not_starve_each_other():
    """桥接论文的中介中心性天然偏高，不设配额会把孤岛/低被引全挤掉。

    真实库上第一次跑出来 5 个方向有 3 个是「跨簇交界」，只剩两类线索，
    答辩时看起来像只做了一种分析。这条锁住配额。
    """
    nodes = [
        _node(f"arxiv:b{i}", f"Bridge{i}", role="bridge", copies=[f"b{i}.pdf"], community=0) for i in range(4)
    ]
    nodes.append(_node("arxiv:i0", "Island", role="isolated", cited_by=0, cites=0, copies=["i.pdf"]))
    nodes.append(_node("arxiv:l0", "Lowcite", role="leaf", cited_by=0, cites=3, copies=["l.pdf"]))
    dirs = future_work.build_directions(_graph(nodes), [])
    kinds = [d["kind"] for d in dirs]
    assert len(dirs) == future_work.MAX_DIRECTIONS, kinds
    assert "island" in kinds, kinds
    assert "lowcite" in kinds, kinds
    # 第一轮受配额约束；第二轮补位会把剩余名额给排名最前的桥接，
    # 所以 bridge 允许超过配额，但另外两类各至少要出现一次。
    assert kinds.index("island") < len(dirs)
    assert kinds.index("lowcite") < len(dirs)


# --------------------------------------------------------------------------- 相似性判断


def test_parse_judge_only_allows_whitelisted_verdicts():
    ok = future_work.parse_judge(
        '{"verdict": "likely_done", "similar": [{"title": "2501.1", "reason": "实验设定一致"}], "reason": "x"}'
    )
    assert ok["verdict"] == "likely_done"
    assert ok["similar"] == [{"title": "2501.1", "reason": "实验设定一致"}]

    weird = future_work.parse_judge('{"verdict": "definitely_original", "similar": [], "reason": "y"}')
    assert weird["verdict"] in ("unclear", "not_found"), weird


def test_sanitize_claim_replaces_originality_words():
    """服务端硬替换：prompt 叮嘱不能当保证，模型真写了也得洗掉。"""
    got = future_work.parse_judge(
        '{"verdict": "not_found", "similar": [], "reason": "这是完全原创的工作，首次提出"}'
    )
    assert "原创" not in got["reason"]
    assert "首次提出" not in got["reason"]
    assert "尚待核实" in got["reason"]


def test_sanitize_claim_keeps_negated_disclaimer():
    """否定句要原样保留——「不能据此认为没人做过」正是我们要求模型写的话。"""
    raw = "只检索了 arXiv，不能据此认为没人做过。"
    got = future_work.parse_judge(f'{{"verdict": "not_found", "similar": [], "reason": "{raw}"}}')
    assert "不能据此认为没人做过" in got["reason"], got["reason"]


def test_parse_judge_garbage_falls_back_to_unclear():
    got = future_work.parse_judge("模型今天不想输出 JSON")
    assert got["verdict"] == "unclear"
    assert got["similar"] == []


def test_judge_prompt_forbids_originality_claims():
    """prompt 里必须明确禁止「原创」类断言，这是赛题口径。"""
    d = {
        "id": "dir-1",
        "anchor_title": "P",
        "basis": "b",
        "evidence": ["We plan to extend X."],
    }
    _, msgs = None, future_work.build_judge_prompt(d, [])
    blob = "".join(str(m.get("content", "")) for m in msgs)
    assert "原创" in blob
    assert "排雷" in blob or "不是认证" in blob


# --------------------------------------------------------------------------- 事件流


def _fake_stream(text):
    async def gen(_messages):
        for ch in text:
            yield ch

    return gen


def _run_events(nodes, extractions, *, judge_text='{"verdict":"not_found","similar":[],"reason":"没检索到"}',
                search=None, stream_judge=None, timeout=5.0):
    return _collect(
        future_work.direction_events(
            _graph(nodes),
            extractions,
            stream_judge=stream_judge or _fake_stream(judge_text),
            search=search,
            timeout=timeout,
        )
    )


def test_direction_events_emit_roles_and_done():
    nodes = [_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])]
    ex = [_extraction("1.pdf", sentences=["We plan to extend X."])]
    evs = _run_events(nodes, ex, search=lambda q, limit=5: [])
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "role"
    assert evs[0]["role"] == "extractor"
    assert "mapper" in [e.get("role") for e in evs if e["type"] == "role"]
    assert kinds[-1] == "done"
    assert kinds.count("direction") == 1


def test_direction_events_never_claim_originality():
    """整条流里任何字段都不许出现「原创」类断言——即使模型硬要这么说。"""
    nodes = [_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])]
    ex = [_extraction("1.pdf", sentences=["We plan to extend X."])]
    evil = '{"verdict": "not_found", "similar": [], "reason": "这是完全原创的工作，首次提出"}'
    evs = _run_events(nodes, ex, judge_text=evil, search=lambda q, limit=5: [])
    blob = repr(evs)
    for w in ("原创", "首次提出", "没人做过"):
        assert w not in blob, f"事件流里出现了禁用词：{w}"


def test_search_failure_does_not_break_stream():
    """arXiv 检索抛异常也要走到 done，只是候选为空。"""

    def boom(_q, limit=5):
        raise RuntimeError("network down")

    nodes = [_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])]
    ex = [_extraction("1.pdf", sentences=["We plan to extend X."])]
    evs = _run_events(nodes, ex, search=boom)
    assert evs[-1]["type"] == "done"
    dirs = [e for e in evs if e["type"] == "direction"]
    assert dirs and dirs[0]["item"]["candidates"] == []


def test_judge_exception_marks_unclear_but_keeps_direction():
    async def broken(_messages):
        raise RuntimeError("model down")
        yield ""  # 让这个函数成为异步生成器；上面先抛，这行永远到不了

    nodes = [_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])]
    ex = [_extraction("1.pdf", sentences=["We plan to extend X."])]
    evs = _run_events(nodes, ex, stream_judge=broken, search=lambda q, limit=5: [])
    assert evs[-1]["type"] == "done"
    item = [e for e in evs if e["type"] == "direction"][0]["item"]
    assert item["verdict"] == "unclear"


def test_judge_timeout_marks_unchecked():
    async def slow(_messages):
        await asyncio.sleep(5)
        yield "never"

    nodes = [_node("arxiv:1", "P", role="bridge", copies=["1.pdf"])]
    ex = [_extraction("1.pdf", sentences=["We plan to extend X."])]
    evs = _run_events(nodes, ex, stream_judge=slow, search=lambda q, limit=5: [], timeout=0.01)
    item = [e for e in evs if e["type"] == "direction"][0]["item"]
    assert item["verdict"] == "unchecked"


def test_no_directions_yields_error_and_done():
    evs = _run_events([], [], search=lambda q, limit=5: [])
    kinds = [e["type"] for e in evs]
    assert "error" in kinds
    assert kinds[-1] == "done"


def test_budget_exhausted_stops_and_reports(monkeypatch):
    calls = {"n": 0}

    def fake_reserve(n=1):
        calls["n"] += 1
        if calls["n"] > 1:
            raise future_work.BudgetExceeded("今天额度用完了")

    monkeypatch.setattr(future_work, "reserve_llm_call", fake_reserve)
    nodes = [
        _node("arxiv:1", "P1", role="bridge", copies=["1.pdf"]),
        _node("arxiv:2", "P2", role="bridge", copies=["2.pdf"]),
    ]
    ex = [
        _extraction("1.pdf", sentences=["We plan A."]),
        _extraction("2.pdf", sentences=["We plan B."]),
    ]
    evs = _run_events(nodes, ex, search=lambda q, limit=5: [])
    done = evs[-1]
    assert done["type"] == "done"
    assert done["stats"]["budget_exhausted"] is True
    # 预算用完就停，不会把剩下的方向也硬跑一遍
    assert done["stats"]["directions"] <= 2


def test_encode_event_is_one_sse_frame():
    frame = future_work.encode_event({"type": "done", "stats": {"a": 1}})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n\n") == 1


def test_topic_query_only_uses_english_content_words():
    d = {"anchor_title": "The Llama 3 Herd of Models", "basis": "", "evidence": []}
    q = future_work.build_topic_query(d)
    assert "Llama" in q
    assert "Herd" in q
    assert len(q.split()) <= 8


@pytest.mark.parametrize("status", list(future_work.EXTRACTION_STATUS))
def test_every_status_is_representable(status):
    """四个状态都必须是合法值，答辩时要能逐个展示。"""
    assert status in future_work.EXTRACTION_STATUS