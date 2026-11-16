"""自动综述的大模型论述层（赛题 1.5.3）的单元测试。

这个文件专门守四件事，任何一条破了都会让演示当场翻车：

1. **citable 必须等于参与分析的论文全集**。
   综述能引用的论文如果只有 Top 5，模型会写不出「主要技术路线」这种需要横向比较的段落；
   反过来如果 citable 里混进没参与分析的条目（比如低置信度待审核记录），
   就会出现「引用了一个图谱里根本没有的编号」。

2. **没通过校验的段落绝不能出现在 answer_delta 里**。
   这是用户提的最硬的要求：越界引用的内容如果先流到屏幕上再收回，
   用户已经看见了错误内容。所以这里逐个事件检查「answer_delta 的文本里
   出现的每个编号都必须在该段的白名单内」。

3. **单段失败/超时/预算耗尽都不能炸掉整条综述**，
   而且必须留下可解释的 section_rejected（带原因），
   否则页面上只会少一段，没人知道为什么少。

4. **空论文集合要给出明确错误，而不是写一篇空综述。**

全部用例都用假的 streamer，不联网、不碰真实模型，也不读真实图谱文件——
除了 citable 那条要跑真实图谱，那条在没有图谱文件时会自动 skip。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app import narrative
from app.graph import load_graph, survey
from app.narrative import (
    MAX_NARRATIVE_LLM_CALLS,
    SECTIONS,
    build_facts,
    build_plan,
    build_prompt,
    citable_ids,
    encode_event,
    extract_citations,
    narrative_events,
    verify_section,
)
from app.narrative import (
    SECTIONS as NARRATIVE_SECTIONS,
)
from app.security import BudgetExceeded

# --------------------------------------------------------------------------- 测试数据


def _make_survey(papers: int = 6, *, with_years: bool = True) -> dict:
    """造一份与 survey() 同形状的最小数据。

    只保留 narrative.py 真正用到的键：stats / citable / representatives / clusters /
    timeline / topics / overview。故意不造 ready=False 的情况（另有专门用例）。
    """
    citable = []
    for i in range(papers):
        citable.append(
            {
                "id": f"arxiv:2000.00{i:02d}",
                "title": f"Paper Number {i}",
                "year": (2018 + i) if with_years else None,
                "role": "cornerstone" if i == 0 else ("hub" if i < 3 else "middle"),
                "cited_by": max(0, papers - i),
                "category": "预训练与基础模型" if i % 2 == 0 else "模型架构",
                "abstract_lead": f"We present a method that improves result number {i}.",
            }
        )
    return {
        "ready": True,
        "focus": "",
        "stats": {"papers": papers, "edges": papers * 2, "clusters": 2, "years": [2018, 2025]},
        "overview": "测试用综述概览",
        "sections": [],
        "clusters": [
            {
                "id": 0,
                "size": max(papers - 1, 0),
                "top_title": citable[0]["title"] if citable else "",
                "top_id": citable[0]["id"] if citable else "",
            },
            {
                "id": 1,
                "size": 1 if papers > 1 else 0,
                "top_title": citable[1]["title"] if papers > 1 else "",
                "top_id": citable[1]["id"] if papers > 1 else "",
            },
        ],
        "timeline": [],
        "representatives": [
            {
                "id": c["id"],
                "title": c["title"],
                "year": c["year"],
                "role": c["role"],
                "score": 1.0 / (i + 1),
                "cited_by": c["cited_by"],
                "abstract_lead": c["abstract_lead"],
                "reader_url": f"/ui/reader.html?paper={c['id']}",
            }
            for i, c in enumerate(citable[:5])
        ],
        "topics": [],
        "citable": citable,
    }


def _fake_streamer(text_by_key: dict[str, str], *, default: str | None = None):
    """按 prompt 里出现的段头部返回固定文本；返回的假 streamer 逐字符吐。"""

    async def _stream(messages: list[dict[str, str]]) -> AsyncIterator[str]:
        blob = "\n".join(m.get("content", "") for m in messages)
        piece = default
        for heading, text in text_by_key.items():
            if heading in blob:
                piece = text
                break
        if piece is None:
            piece = ""
        for ch in piece:
            yield ch
            await asyncio.sleep(0)

    return _stream


async def _collect(survey_data: dict, **kwargs) -> list[dict]:
    return [e async for e in narrative_events(survey_data, **kwargs)]


# --------------------------------------------------------------------------- 1. citable


def test_citable_count_equals_papers_in_real_graph():
    """citable 的条数必须等于本次参与图谱分析的论文数。

    这条会读真实图谱缓存；机器上没跑过 scripts/build_graph.py 时跳过，
    而不是伪造一份数据假装通过。
    """
    raw = load_graph()
    if not raw:
        pytest.skip("本机还没有图谱缓存（data/meta/graph.json），跳过真实图谱断言")
    from app.graph import graph_view

    data = survey(graph_view(raw), "")
    if not data.get("ready"):
        pytest.skip(f"图谱未就绪：{data.get('detail')}")
    citable = data.get("citable") or []
    assert len(citable) == data["stats"]["papers"], (
        f"citable 有 {len(citable)} 条，但参与分析的论文是 {data['stats']['papers']} 篇——"
        "综述能引用的范围必须覆盖全部论文，不能只剩 Top 5"
    )


def test_citable_ids_are_unique_and_non_empty():
    data = _make_survey(6)
    ids = citable_ids(data)
    assert len(ids) == len(data["citable"])
    assert all(i for i in ids), "citable 里不允许出现空 id"


def test_planner_analyst_do_not_call_llm():
    """Planner 与 Analyst 是确定性代码：同样的输入必须给出同样的输出。

    这也是「四个角色」不等于「四个 AI Agent」的依据之一——
    只有 Writer 会调模型，其余三段可以单测到每个字段。
    """
    data = _make_survey(6)
    plan_a, plan_b = build_plan(data), build_plan(data)
    assert plan_a == plan_b
    facts_a = build_facts(data, plan_a)
    facts_b = build_facts(data, plan_b)
    assert facts_a == facts_b
    assert [s["key"] for s in plan_a["sections"]] == [s["key"] for s in SECTIONS]


# --------------------------------------------------------------------------- 2. 校验


def test_extract_citations_ignores_source_markers():
    """`[来源: 论文.pdf 第3页]` 这种中文角标不是引用，不能算越界。"""
    text = "说明见 [arxiv:1706.03762]，另见【来源: BERT.pdf 第3页】以及 [hash:abc123def456]。"
    ids = extract_citations(text)
    assert ids == ["arxiv:1706.03762", "hash:abc123def456"]


def test_out_of_scope_citation_rejects_whole_section():
    """引用了白名单外的编号 → 整段不通过，且原因写清是哪个编号。"""
    data = _make_survey(6)
    plan = build_plan(data)
    facts = build_facts(data, plan)
    step = plan["sections"][0]
    allowed = {p["id"] for p in facts[step["key"]]["picks"]}

    bad = f"{step['heading']}：这里引用了 [arxiv:9999.99999]。"
    result = verify_section(step["key"], step["heading"], bad, facts[step["key"]], allowed)
    assert result["ok"] is False
    assert any("9999.99999" in r["id"] for r in result["rejected"]), result["rejected"]


def test_in_scope_citation_passes():
    data = _make_survey(6)
    plan = build_plan(data)
    facts = build_facts(data, plan)
    step = plan["sections"][0]
    pick = facts[step["key"]]["picks"][0]
    allowed = {p["id"] for p in facts[step["key"]]["picks"]}

    # 论断里带上标题实词，保证第三层（论断是否有依据）也能过
    good = f"{step['heading']}：{pick['title']} 被库内 {pick['cited_by']} 篇论文引用 [{pick['id']}]。"
    result = verify_section(step["key"], step["heading"], good, facts[step["key"]], allowed)
    assert result["ok"] is True, result
    assert result["citations"] and result["citations"][0] == pick["id"]


def test_unverified_claim_is_reported_but_does_not_drop_section():
    """第三层校验（论述是否有事实依据）判不出时只标记，不丢段。

    为什么这样设计：这一层是规则近似，误杀代价比漏标高得多——
    整段被丢会让用户以为系统坏了，而标记出来只是提示「这句需要人工看一眼」。
    """

    class _FakePick(dict):
        pass

    facts = {
        "picks": [
            {"id": "arxiv:1111.1111", "title": "Alpha Beta Gamma", "year": 2020,
             "role": "cornerstone", "cited_by": 9, "claim": "Alpha Beta Gamma 提出一种方法"},
        ],
        "stats": {},
        "clusters": [],
        "timeline": [],
    }
    # 这句话提到了编号，但完全没有标题/年份/被引次数的任何痕迹
    text = "本段讨论完全无关的内容，只挂了一个编号 [arxiv:1111.1111]。"
    result = verify_section("background", "研究背景", text, facts, {"arxiv:1111.1111"})
    assert result["ok"] is True
    assert result["unverified"], "应记为待人工留意"


# --------------------------------------------------------------------------- 3. 流式与失败


def test_answer_delta_never_contains_unverified_text():
    """核心断言：屏幕上出现的每一段，引用编号都必须在该段白名单里。

    做法：让「主要技术路线」段故意写越界编号，其余段正常；
    然后检查所有 answer_delta 文本里抽出的编号，全部落在该段白名单内，
    并且越界那一段的事件是 section_rejected 而不是 answer_delta。
    """
    data = _make_survey(6)
    plan = build_plan(data)
    facts = build_facts(data, plan)
    bad_key = SECTIONS[1]["key"]  # routes
    bad_heading = SECTIONS[1]["heading"]
    pick = facts[bad_key]["picks"][0]

    texts = {}
    for step in plan["sections"]:
        first = facts[step["key"]]["picks"][0]
        texts[step["heading"]] = (
            f"{step['heading']}：{first['title']} 被库内 {first['cited_by']} 篇引用 [{first['id']}]。"
        )
    texts[bad_heading] = f"{bad_heading}：编造一条 {pick['title']} [arxiv:9999.99999]。"

    events = asyncio.run(_collect(data, stream_section=_fake_streamer(texts)))

    deltas = [e for e in events if e["type"] == "answer_delta"]
    assert deltas, "至少要有通过的段落"
    assert bad_heading not in [d.get("key") for d in deltas]
    rejected = [e for e in events if e["type"] == "section_rejected"]
    assert [e["key"] for e in rejected] == [bad_key]
    assert "9999.99999" in rejected[0]["reason"]

    # 逐段核对：announce 的每个编号都在白名单内
    for d in deltas:
        allowed = {p["id"] for p in facts[d["key"]]["picks"]}
        for cid in extract_citations(d["text"]):
            assert cid in allowed, f"段 {d['key']} 流出白名单外编号 {cid}"


def test_empty_paper_set_returns_error_not_empty_review():
    data = _make_survey(0)
    data["citable"] = []
    events = asyncio.run(_collect(data))
    assert events[0]["type"] == "error"
    assert "没有可引用的论文" in events[0]["message"]
    assert events[-1]["type"] == "done"
    assert not [e for e in events if e["type"] == "answer_delta"]


def test_model_exception_only_drops_that_section():
    """模型对某一段抛异常，只丢这一段；其余段照常写出并校验。"""

    calls = {"n": 0}

    async def _boom(messages):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模型 500")
        step_key = None
        blob = "\n".join(m.get("content", "") for m in messages)
        data = _make_survey(6)
        for s in SECTIONS:
            if s["heading"] in blob:
                step_key = s["key"]
        fact = build_facts(data, build_plan(data))[step_key]["picks"][0]
        for ch in f"{fact['title']} 被引用了 {fact['cited_by']} 次 [{fact['id']}]。":
            yield ch

    data = _make_survey(6)
    events = asyncio.run(_collect(data, stream_section=_boom))
    ok = [e for e in events if e["type"] == "answer_delta"]
    bad = [e for e in events if e["type"] == "section_rejected"]
    assert len(bad) == 1 and "模型返回异常" in bad[0]["reason"]
    assert len(ok) == len(SECTIONS) - 1


def test_section_timeout_is_reported():
    async def _slow(messages):
        await asyncio.sleep(5)
        yield "永远不会到这里"

    data = _make_survey(6)
    events = asyncio.run(_collect(data, stream_section=_slow, timeout=0.01))
    bad = [e for e in events if e["type"] == "section_rejected"]
    assert len(bad) == len(SECTIONS)
    assert all("秒" in b["reason"] for b in bad)


def test_budget_exhausted_stops_remaining_sections(monkeypatch):
    """预算耗尽：只丢当前这一段并停止，不再继续烧后面的调用。"""
    used = {"n": 0}

    def _fake_reserve(n: int = 1) -> None:
        used["n"] += 1
        if used["n"] >= 2:
            raise BudgetExceeded("今日额度已用完")

    monkeypatch.setattr(narrative, "reserve_llm_call", _fake_reserve)

    async def _ok(messages):
        blob = "\n".join(m.get("content", "") for m in messages)
        for ch in blob[:0]:  # 空输出，走「模型没给内容」分支
            yield ch

    data = _make_survey(6)
    events = asyncio.run(_collect(data, stream_section=_ok))
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["stats"]["budget_exhausted"] is True
    rejected = [e for e in events if e["type"] == "section_rejected"]
    assert len(rejected) == 1, "预算耗尽后应立刻停，不再尝试后面的段"
    assert "额度" in rejected[0]["reason"]


def test_llm_call_count_never_exceeds_cap():
    """一次综述最多用 MAX_NARRATIVE_LLM_CALLS 次模型调用。

    用户明确要求：一次综述不能把整天预算吃光。这里把每段都成功的情况跑一遍，
    统计实际进入模型的次数。
    """
    data = _make_survey(6)
    plan = build_plan(data)
    facts = build_facts(data, plan)
    texts = {}
    for step in plan["sections"]:
        first = facts[step["key"]]["picks"][0]
        texts[step["heading"]] = f"{first['title']} 被 {first['cited_by']} 篇引用 [{first['id']}]。"
    seen = {"n": 0}
    inner = _fake_streamer(texts)

    async def _counting(messages):
        seen["n"] += 1
        async for piece in inner(messages):
            yield piece

    events = asyncio.run(_collect(data, stream_section=_counting))
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["stats"]["llm_calls"] == len(SECTIONS)
    assert seen["n"] <= MAX_NARRATIVE_LLM_CALLS


def test_cancelled_error_propagates():
    """客户端断开时 CancelledError 必须抛出去，不能被当成「单段失败」吞掉。"""

    async def _cancel(messages):
        raise asyncio.CancelledError()
        yield  # pragma: no cover

    data = _make_survey(6)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_collect(data, stream_section=_cancel))


def test_local_graph_batch_is_accepted_as_review_source(tmp_path, monkeypatch):
    """局域图谱场景：整批论文能原样喂给综述，不会被「没有 citable」挡掉。

    用户点名要测这条，因为局域图谱是演示主线（用户上传一批论文 → 图谱 → 综述）：
    局域返回的节点用的是 `reader.html?paper=文件名` 这种相对链接，
    而全库用的是 `/ui/reader.html?...`，两套字段不同，必须由调用方归一成
    survey 那份形状再进来。这里验证「归一之后能跑通，且引用的论文不超出这批」。
    """
    from pathlib import Path

    from app import local_graph
    from app.graph import ReferenceEntry
    from app.local_graph import LocalPaper, build_local_graph

    specs = [
        ("a.pdf", "Paper A", "1000.0001", [("Base Paper", "2000.0001")]),
        ("b.pdf", "Paper B", "1000.0002", [("Base Paper", "2000.0001")]),
        ("base.pdf", "Base Paper", "2000.0001", []),
    ]
    table: dict[str, list[tuple[str, str]]] = {}
    papers: list[LocalPaper] = []
    for name, title, arxiv, refs in specs:
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4\n% fake\n")
        table[name] = refs
        papers.append(LocalPaper(filename=name, path=path, title=title, arxiv_id=arxiv))

    def _fake_extract(path):
        name = Path(path).name
        return [
            ReferenceEntry(index=i + 1, raw=f"{t} {a}", title=t, arxiv_id=a)
            for i, (t, a) in enumerate(table.get(name, []))
        ]

    monkeypatch.setattr(local_graph, "extract_references", _fake_extract)

    local = build_local_graph(list(papers))
    nodes = local["nodes"]
    assert len(nodes) == 3

    # 把局域图谱归一成 survey() 的输出形状（这就是前端/上层要做的映射）
    citable = [
        {
            "id": n["id"],
            "title": n["title"],
            "year": 2020,
            "role": n["role"],
            "cited_by": n["cited_by"],
            "category": "",
            "abstract_lead": f"{n['title']} 的摘要首句。",
        }
        for n in nodes
    ]
    data = _make_survey(3)
    data["citable"] = citable
    data["stats"] = {"papers": 3, "edges": local["summary"]["edges"], "clusters": 1, "years": [2020, 2020]}

    events = asyncio.run(_collect(data, stream_section=_fake_streamer({})))
    roles = {e["role"]: e["output"] for e in events if e["type"] == "role"}
    assert roles["analyst"]["citable"] == 3
    # 所有被挑出来引用的论文，必须都在这批 3 篇里
    batch_ids = {c["id"] for c in citable}
    for sec in roles["analyst"]["sections"]:
        for pick in sec["picks"]:
            assert pick["id"] in batch_ids


# --------------------------------------------------------------------------- 4. 交接物与协议


def test_role_handoffs_carry_real_content():
    """四阶段的交接物必须是真内容，不是只有角色名的空壳。"""
    data = _make_survey(6)
    events = asyncio.run(_collect(data, stream_section=_fake_streamer({})))

    roles = {e["role"]: e["output"] for e in events if e["type"] == "role"}
    assert set(roles) == {"planner", "analyst", "writer", "verifier"}

    plan = roles["planner"]
    assert [s["heading"] for s in plan["sections"]] == [s["heading"] for s in NARRATIVE_SECTIONS]
    assert plan["stats"]["papers"] == 6

    analyst = roles["analyst"]
    assert analyst["citable"] == 6
    first_pick = analyst["sections"][0]["picks"][0]
    for field in ("id", "title", "year", "role", "cited_by"):
        assert field in first_pick, f"Analyst 交接物缺字段 {field}"

    assert "sections_ok" in roles["verifier"]
    assert "rejected" in roles["verifier"]


def test_prompt_forbids_ids_outside_whitelist():
    data = _make_survey(6)
    plan = build_plan(data)
    facts = build_facts(data, plan)
    step = plan["sections"][0]
    allowed = {p["id"] for p in facts[step["key"]]["picks"]}
    messages = build_prompt(step, facts[step["key"]], allowed)
    blob = "\n".join(m["content"] for m in messages)
    assert "只能" in blob or "不许" in blob or "不得" in blob
    for pid in allowed:
        assert pid in blob, "白名单里的编号必须逐个出现在 prompt 里"


def test_encode_event_is_one_sse_frame():
    frame = encode_event({"type": "done", "stats": {"papers": 3}})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n\n") == 1


def test_global_budget_names_are_registered():
    """新端点必须同时登在限流表与公开路径表里，否则启动就对账失败。"""
    from app import security

    assert security.bucket_for("/graph/survey/narrative") is security.BUCKET_NARRATIVE
    assert "/graph/survey/narrative" in security.PUBLIC_PATHS
    # 不能和 /graph/survey（CPU 密集档）同档：一次综述内部要连 5 次模型调用
    assert security.BUCKET_NARRATIVE is not security.BUCKET_CPU