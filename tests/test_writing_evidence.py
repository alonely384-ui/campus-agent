"""1.6.1 本地正文证据层（app/writing_evidence.py）的回归测试。

一个网络请求都不发、也不碰真实 chroma：``promote_from_evidence`` 内部
``from app.rag import pipeline`` 后调 ``pipeline.retrieve_scored``，
测试里把它换成假的，按 query / source 返回预设块。

每个 case 的 docstring 写明它守的是哪条真问题——这些判据都是从真实索引上
实测漏网样本倒推出来的，不是为了凑覆盖率写的。
"""

import pytest

from app import literature as lit
from app import writing as w
from app import writing_evidence as we
from app.literature import Candidate
from app.rag.pipeline import Chunk

IDEA = "研究一种用引用图谱约束的检索增强生成方法，缓解大模型幻觉并提升文献综述的可追溯性"

# 真实正文样本（实测过闸的那种）：含 retrieval 与 citation、graph，
# 并且把「幻觉」和「可追溯性」两个 topic_concept 都讲到了。
BODY_OK = (
    "Retrieval-augmented generation combines a parameterised language model with a "
    "non-parametric retriever. The citation network over the retrieved documents is then "
    "used as a graph constraint during decoding, which reduces hallucinated spans and "
    "makes every generated sentence traceable back to a real source paper in the corpus."
)
# 只谈「引用 + 图谱」但不谈 RAG 的样本（真实漏网内容，来自 GNN 教科书）
BODY_OFFTOPIC = (
    "In contrast, the citation network in figure 13.2b contains directed edges. Each paper "
    "cites other papers, and this relationship is inherently one-way. The graph is also much "
    "sparser than a dense network, which makes it harder to learn from."
)
# 参考文献条目（真实漏网内容）
REF_BLOCK = (
    "References Julien Abadji, Pedro Javier Ortiz Suarez, Laurent Romary, and Benoit Sagot. "
    "Ungoliant: An optimized pipeline for the generation of a very large-scale multilingual "
    "web corpus. In Proceedings of the Workshop on Challenges in the Management of Large Corpora."
)
# 表格残片（真实漏网内容）
TABLE_FRAG = (
    "Table 24| FRAMES (Factuality, Retrieval, And reasoning MEasurement Set) is a "
    "comprehensive benchmark designed to evaluate core components of retrieval-augmented "
    "generation (RAG) systems. Our evaluation employs the benchmark across three tasks and "
    "reports the accuracy of each system on every subset of the benchmark."
)


def _cand(key: str, title: str, local_file: str = "", **kw) -> Candidate:
    return Candidate(
        key=key,
        title=title,
        source=lit.SOURCE_LOCAL if local_file else lit.SOURCE_ARXIV,
        local_file=local_file,
        **kw,
    )


def _chunk(source: str, text: str, page: int = 3, chunk_id: int = 3001) -> Chunk:
    return Chunk(text=text, source=source, page=page, chunk_id=chunk_id)


@pytest.fixture
def fake_index(monkeypatch):
    """把 pipeline.retrieve_scored 换掉：按 (query, source) 查预设表。"""
    from app.rag import pipeline

    table: dict[tuple[str, str | None], list[tuple[Chunk, float, list[str]]]] = {}

    def _fake(question, top_k=4, source=None, use_dense=True):
        return list(table.get((question, source), []))[:top_k]

    monkeypatch.setattr(pipeline, "retrieve_scored", _fake)
    return table


@pytest.fixture(autouse=True)
def _fake_catalog(monkeypatch):
    """catalog 与作者表换成可控的小样本，避免依赖 data/ 的真实内容。"""
    rows = [
        {"file": "a.pdf", "title": "Graph Constrained Retrieval", "year": 2024,
         "arxiv_id": "2401.00001", "category": "检索增强"},
        {"file": "b.pdf", "title": "Hallucination Survey", "year": 2023,
         "arxiv_id": "2301.00002", "category": "对齐与推理"},
    ]
    monkeypatch.setattr(lit, "load_catalog", lambda: rows)
    monkeypatch.setattr(lit, "load_local_authors", lambda: {"a.pdf": ["Ada"], "b.pdf": ["Bob"]})
    monkeypatch.setattr(we, "_local_abstract", lambda fname: f"{fname} 的摘要")


# --------------------------------------------------------------------------- G1 / G2 / G3 判据


def test_g1_rejects_reference_section_head():
    """G1 第三判据 / G2：以 References 起头的整段参考文献必须挡住。

    真问题：实测有块以 ``References Julien Abadji, ...`` 开头，全是**全名**作者串，
    pipeline 的 ``_looks_like_citation`` 要 ≥2 个 ``et al.``/``arXiv:``/会议名，
    缩写人名判据要 ``S. Krishna,`` 那种形态，两个都认不出来。
    """
    assert we._looks_like_reference_block(REF_BLOCK) is True
    assert we._looks_like_ref_fragment(REF_BLOCK) is False  # 缩写判据确实漏


def test_g2_rejects_table_fragment():
    """G2：表格残片必须挡住。G3 挡不住它——残片真的「谈到了 RAG」。"""
    assert we._looks_like_table_fragment(TABLE_FRAG) is True


def test_g2_accepts_real_body_text():
    """G2 不能误杀正文：三个真实正文样本都必须放行。"""
    for text in (BODY_OK, BODY_OFFTOPIC):
        assert we._looks_like_reference_block(text) is False
        assert we._looks_like_table_fragment(text) is False
        assert len(text) >= we.MIN_TEXT_CHARS


def test_g3_rejects_chunk_hitting_one_concept_only():
    """G3 规则 3：只命中一个普通概念词不通过，且要如实记录命中了哪个词。

    样本只提到 retrieval（「检索增强」这一个概念），长度过关、无引用特征，
    所以它一定是被 G3 挡下、而不是被 G1/G2 顺手拦掉的。
    """
    text = (
        "The retriever fetches passages at inference time and the model reads them before "
        "answering. We compare several retrieval strategies and report the accuracy of each "
        "one on the development set, together with the latency of a single decoding step."
    )
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(text, concepts)
    assert ok is False
    assert info["reason"] == ""
    assert len(info["hits"]) == 1
    assert info["matched"] == ["retrieval"]


def test_g3_passes_body_with_two_topic_concepts():
    """G3 规则 3：topic_concept 与其他不同主题概念共同命中才放行。

    真问题：``BODY_OK`` 同时含 retrieval / citation / graph / hallucinat，
    2026-11-12 第二轮把 hallucination 降为 topic_concept 之后，
    单靠它不再放行；能与它配对的是「可追溯性」这个 topic_concept
    （traceable）。两者概念 id 不同 → ``topic_concept_with_support``。
    对照组：``BODY_OFFTOPIC``（citation + graph 两个泛词）必须拒绝。
    """
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(BODY_OK, concepts)
    assert ok is True
    assert info["reason"] == "topic_concept_with_support"
    assert "hallucinat" in info["matched"]
    assert info["anchor_ids"] == []
    assert len(info["topic_ids"]) == 2
    assert info["generic_ids"]  # citation/graph/retrieval 都在，但不算数


def test_g3_counts_shared_rag_alias_once():
    """G3 规则 4：同一个英文词经两个中文映射只能计一次。

    真问题：``检索增强`` → (retrieval, rag)、``增强生成`` → (generation, rag)，
    按命中词计数会让一段只提 RAG 的表格残片点亮两个「概念」。
    合并后它们共享同一个 concept_id，rag 只贡献一个概念。
    """
    concepts = we._concept_terms(IDEA)
    rag_only = (
        "We evaluate a RAG system on three tasks. The rag pipeline retrieves then reads. "
        + "filler " * 25
    )
    ok, info = we._g3_verdict(rag_only, concepts)
    ids = {h["concept_id"] for h in info["hits"]}
    assert len(ids) == 1
    assert ok is False  # 单个概念不够格


def test_concept_terms_follow_idea_order_not_word_length():
    """议题词按 idea 里首次出现的位置排，不按词长。

    真问题：按长度排得到 ['检索增强','增强生成','引用','图谱']，截到 4 个时
    把「幻觉」挤掉了——而「检索增强/增强生成」本是同一件事的两种写法。
    """
    terms = we._concept_terms(IDEA)
    zh = [t["zh"] for t in terms]
    assert zh[0] == "引用"
    assert "幻觉" in zh
    assert len(terms) <= we.MAX_CONCEPTS


def test_concept_terms_detects_proper_noun():
    """字典覆盖不到时，专有名词（Mamba）也要被认成议题词。"""
    terms = we._concept_terms("探索 Mamba 这类状态空间模型处理长文档问答的能力边界")
    kinds = [t["kind"] for t in terms]
    assert "proper" in kinds
    assert any(t["zh"] == "Mamba" for t in terms)


# --------------------------------------------------------------------------- 主流程


def test_promotes_paper_found_only_by_body_evidence(fake_index):
    """英文全局检索命中本库论文 → 补进候选池并挂证据。"""
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", BODY_OK, page=7), 6.1, ["dense", "rerank"]),
    ]
    pool = [_cand("C1", "Some External Paper")]
    info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)

    assert info["status"] == "ok"
    assert "a.pdf" in info["promoted"]
    assert len(pool) == 2
    promoted = next(c for c in pool if c.local_file == "a.pdf")
    assert promoted.key == "C2"  # 接在原池最大编号之后
    assert len(promoted.evidence) == 1
    assert promoted.evidence[0]["page"] == 7
    assert promoted.evidence[0]["score_type"] == "rerank"
    assert promoted.entry_reason["kind"] == "evidence_promoted"


def test_evidence_keeps_full_text_and_truncates_only_at_render(fake_index):
    """Evidence 存完整原文；截断只发生在 prompt 渲染时。"""
    long_text = BODY_OK + " " + "extra sentence about retrieval graphs. " * 20
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", long_text), 5.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)

    assert pool[0].evidence[0]["text"] == long_text  # 完整保留
    rendered = w.render_pool(pool)
    assert "本库原文" in rendered
    assert "…" in rendered  # 渲染时截断
    assert len(rendered) < len(long_text) + 400


def test_missing_page_never_renders_as_page_zero(fake_index):
    """页码缺失时渲染「页码未定位」，绝不写成第 0 页。"""
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", BODY_OK, page=None, chunk_id=1), 5.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)
    assert pool[0].evidence[0]["page"] is None
    rendered = w.render_pool(pool)
    assert "页码未定位" in rendered
    assert "第 0 页" not in rendered


def test_chinese_discovery_without_evidence_does_not_enter_pool(fake_index):
    """中文只发现 source、拿不到合格英文块时，不得入池。"""
    fake_index[("引用图谱约束的检索增强生成", None)] = [
        (_chunk("b.pdf", "无关内容" * 30, page=2), 3.0, ["dense"]),
    ]
    # 逐篇补检也拿不到合格块
    fake_index[("citation graph retrieval", "b.pdf")] = [
        (_chunk("b.pdf", TABLE_FRAG, page=4), 2.0, ["dense"]),
    ]
    pool = [_cand("C1", "External")]
    info = we.promote_from_evidence(
        IDEA,
        ['all:"citation graph" AND all:"retrieval"'],
        pool,
        8,
        ["引用图谱约束的检索增强生成"],
    )

    assert "b.pdf" in info["discovered_without_evidence"]
    assert "b.pdf" not in info["promoted"]
    assert [c.local_file for c in pool] == [""]


def test_english_source_restricted_supplement_promotes(fake_index):
    """中文发现 + 英文 source 限定补检拿到合格块 → 才补入池。"""
    fake_index[("引用图谱约束的检索增强生成", None)] = [
        (_chunk("a.pdf", "无关内容" * 30, page=2), 3.0, ["dense"]),
    ]
    fake_index[("citation graph retrieval", "a.pdf")] = [
        (_chunk("a.pdf", BODY_OK, page=9), 7.5, ["dense", "rerank"]),
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(
        IDEA,
        ['all:"citation graph" AND all:"retrieval"'],
        pool,
        8,
        ["引用图谱约束的检索增强生成"],
    )

    assert "a.pdf" in info["promoted"]
    assert info["discovered_without_evidence"] == []
    assert pool[0].evidence[0]["page"] == 9


def test_title_query_used_when_no_english_query(fake_index):
    """没有英文 query 时，用 catalog 真实标题构造 query 再做 source 限定检索。"""
    fake_index[("引用图谱约束的检索增强生成", None)] = [
        (_chunk("a.pdf", "无关内容" * 30, page=2), 3.0, ["dense"]),
    ]
    fake_index[("Graph Constrained Retrieval", "a.pdf")] = [
        (_chunk("a.pdf", BODY_OK, page=5), 4.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, [], pool, 8, ["引用图谱约束的检索增强生成"])

    assert "a.pdf" in info["promoted"]
    assert pool[0].title == "Graph Constrained Retrieval"


def test_existing_paper_in_pool_gets_evidence_without_renumbering(fake_index):
    """已在池里的本库论文只绑证据：不新建、不改 key。"""
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", BODY_OK, page=8), 6.0, ["dense"]),
    ]
    pool = [_cand("C1", "Graph Constrained Retrieval", local_file="a.pdf")]
    info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)

    assert len(pool) == 1
    assert pool[0].key == "C1"
    assert pool[0].title == "Graph Constrained Retrieval"
    assert len(pool[0].evidence) == 1
    assert info["gates"]["papers_in_pool"] == 1


def test_orphan_source_not_in_catalog_is_rejected(fake_index):
    """chroma 里有、catalog 里没有的孤儿 source 必须挡在门外。"""
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("orphan.pdf", BODY_OK), 9.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)

    assert info["gates"]["no_catalog"] == 1
    assert pool == []


def test_pool_budget_caps_promotion(fake_index):
    """补池预算 = total_limit // 4，超出记 papers_skipped_cap。"""
    items = []
    for i in range(8):
        items.append((_chunk(f"p{i}.pdf", BODY_OK, chunk_id=i + 1), 9.0 - i, ["dense"]))
    fake_index[("citation graph retrieval", None)] = items
    cat = [
        {"file": f"p{i}.pdf", "title": f"Paper {i}", "year": 2024, "arxiv_id": "", "category": ""}
        for i in range(8)
    ]
    import app.writing_evidence as m

    old = lit.load_catalog
    lit.load_catalog = lambda: cat
    try:
        pool: list[Candidate] = []
        info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)
    finally:
        lit.load_catalog = old

    assert we.MAX_PROMOTED_DIVISOR == 4
    assert len(info["promoted"]) == 2  # 8 // 4
    assert info["gates"]["papers_skipped_cap"] == 6
    assert len(pool) == 2
    _ = m


def test_keys_remain_unique_after_promotion(fake_index):
    """补候选接在原池最大编号之后，key 全池唯一。"""
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", BODY_OK, chunk_id=1), 6.0, ["dense"]),
        (_chunk("b.pdf", BODY_OK, chunk_id=2), 5.0, ["dense"]),
    ]
    pool = [_cand("C1", "X"), _cand("C2", "Y"), _cand("C3", "Z")]
    we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 32)

    keys = [c.key for c in pool]
    assert len(keys) == len(set(keys))
    assert keys == ["C1", "C2", "C3", "C4", "C5"]


def test_empty_pool_still_gets_evidence_candidates(fake_index):
    """空池时补候选照跑——这正是这一层存在的意义（元数据检索 0 命中也能救回来）。

    放在 ``build_candidates`` 之后、``pool_coverage_gaps`` 和空池早退**之前**，
    否则早退一 return，补候选永远不执行。
    """
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", BODY_OK), 6.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 32)

    assert info["status"] == "ok"
    assert len(pool) == 1


def test_gate_ledger_has_all_fields_and_short_circuits(fake_index):
    """台账 12 个字段齐全；每个块只被第一个拦住它的门计数。

    2026-11-12 新增 ``g3_generic_only``：它是 ``g3_topic`` 的**细分**，
    两者同时 +1（拒绝数仍记在 g3_topic，语义不变、旧统计可比），
    所以这里的断言是 ``g3_topic == 1`` 且 ``g3_generic_only == 0``——
    offtopic_one 只命中 retrieval 一个泛概念，属于单概念拒绝而不是泛词凑数。
    """
    offtopic_one = (
        "The retriever fetches passages at inference time and the model reads them before "
        "answering. We compare several retrieval strategies and report the accuracy of each "
        "one on the development set, together with the latency of a single decoding step."
    )
    fake_index[("citation graph retrieval", None)] = [
        (_chunk("a.pdf", REF_BLOCK, chunk_id=1), 1.0, ["dense"]),      # G2 refblock
        (_chunk("a.pdf", TABLE_FRAG, chunk_id=2), 2.0, ["dense"]),     # G2 table
        (_chunk("a.pdf", offtopic_one, chunk_id=3), 3.0, ["dense"]),   # G3 reject
        (_chunk("a.pdf", BODY_OK, chunk_id=4), 4.0, ["dense"]),        # pass
        (_chunk("a.pdf", BODY_OK, chunk_id=4), 4.0, ["dense"]),        # dup
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, ['all:"citation graph" AND all:"retrieval"'], pool, 8)
    gates = info["gates"]

    expected = {
        "retrieved", "dup_skipped", "no_catalog", "g1_citation", "g2_quality",
        "g3_topic", "g3_generic_only", "passed", "papers_seen", "papers_in_pool",
        "papers_promoted", "papers_skipped_cap",
    }
    assert expected <= set(gates)
    assert gates["retrieved"] == 5
    assert gates["dup_skipped"] == 1
    assert gates["g2_quality"] == 2
    assert gates["g3_topic"] == 1
    assert gates["g3_generic_only"] == 0
    assert gates["passed"] == 1


def test_gate_counts_generic_only_separately(fake_index):
    """``g3_generic_only`` 只在「泛词凑数」时 +1，与单概念拒绝区分开。

    真问题：Understanding Deep Learning 第 255 页那句靠 citation + graph
    两个泛词凑数，分级后落在 generic_only。这一格就是用来盯住这类块的。
    """
    fake_index[("citation graph", None)] = [
        (_chunk("a.pdf", BODY_OFFTOPIC, chunk_id=9), 5.0, ["dense"]),
    ]
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, ['all:"citation graph"'], pool, 8)

    gates = info["gates"]
    assert gates["g3_topic"] == 1
    assert gates["g3_generic_only"] == 1
    assert gates["passed"] == 0
    assert pool == []


def test_retrieval_failure_degrades_instead_of_raising(fake_index, monkeypatch):
    """检索抛异常时降级：记 errors、返回状态，不把整条写作链路炸掉。"""
    from app.rag import pipeline

    def _boom(*a, **kw):
        raise RuntimeError("chroma 挂了")

    monkeypatch.setattr(pipeline, "retrieve_scored", _boom)
    pool: list[Candidate] = []
    info = we.promote_from_evidence(IDEA, ['all:"citation graph"'], pool, 8)

    assert info["errors"]
    assert info["status"] in {"no_hit", "no_promote"}


def test_empty_queries_skip_without_retrieval(fake_index):
    """没有任何 query 时直接 skipped，不发检索。"""
    info = we.promote_from_evidence(IDEA, [], [], 8)
    assert info["status"] == "skipped"
    assert info["gates"]["retrieved"] == 0


# --------------------------------------------------------------------------- 与既有机制的不变量


def test_citation_validation_unchanged_by_evidence():
    """引用校验逻辑不受证据层影响：仍只认池内 key，悬空引用照样报 unknown。"""
    pool = [
        _cand("C1", "Graph Constrained Retrieval", local_file="a.pdf"),
        _cand("C2", "Hallucination Survey"),
    ]
    md = "正文一句。[C1]\n\n另一句。[C9]\n"
    check = w.check_citations(md, pool)

    assert check.cited_keys == ["C1"]
    assert check.unknown_keys == ["C9"]
    assert check.ok is False


def test_prompt_mentions_evidence_only_when_present():
    """池里没有证据时不追加说明段，避免教模型去找不存在的原文。"""
    plain = [_cand("C1", "External Paper")]
    assert w._EVIDENCE_NOTE not in w.build_framework_prompt("idea", plain)[1]["content"]

    with_ev = [
        _cand(
            "C1",
            "Graph Constrained Retrieval",
            local_file="a.pdf",
            evidence=[{"source_file": "a.pdf", "chunk_id": 1, "page": 2,
                       "text": BODY_OK, "score": 1.0, "score_type": "rrf",
                       "channels": ["dense"], "query": "q"}],
        )
    ]
    prompt = w.build_framework_prompt("idea", with_ev)[1]["content"]
    assert w._EVIDENCE_NOTE in prompt
    assert "逐句验证" not in prompt or "并没有被逐句验证过" in prompt


# --------------------------------------------------------------------------- G3 概念分级（2026-11-12）
#
# 这一组守的是同一个真问题：**泛词凑数**。旧的 two_concepts 只数「命中几个
# 不同概念」，于是 citation + graph 这种任何论文都有的组合就能冒充
# 「跟我的选题有关」。实测代价是一本深度学习教科书的图神经网络章节
# （Understanding Deep Learning p255/p256）被补进了 CG-RAG 的候选池。

# Understanding Deep Learning 第 255 页的真实文本（真实索引取回，未改写）
UDL_P255 = (
    "In contrast, the citation network in figure 13.2b contains directed edges. "
    "Each paper cites other papers, and this relationship is inherently one-way"
)
# 第 256 页的真实文本（同上）
UDL_P256 = (
    "242 13 Graph neural networks Figure 13.2 Types of graphs. a) A social network is an "
    "undirected graph; the connections between people are symmetric. b)"
)


def _text_of(sentence: str) -> str:
    """把一句样本补到 G2 能过的长度，并保证不引入额外的议题词。

    只用无意义填充词：如果填充里混进了 retrieval / graph 之类，
    测的就不是「这句样本过不过 G3」而是「填充词过不过 G3」了。
    """
    return sentence + " " + "filler " * 30


def test_g3_rejects_citation_plus_graph():
    """**核心回归**：citation + graph 两个泛词凑数必须拒绝。

    真问题：这两个词是任何论文都有的通用词，凑两个不等于谈了你研究的选题。
    """
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(_text_of("The citation network is a graph of directed edges."), concepts)
    assert ok is False
    assert info["reason"] == "generic_only"
    assert set(info["matched"]) >= {"citation", "graph"}
    assert info["anchor_ids"] == []


def test_g3_rejects_attention_alone():
    """Attention 是通用技术词，不能当主题锚点。"""
    concepts = we._concept_terms("研究 Attention 机制的作用")
    ok, info = we._g3_verdict(_text_of("We apply attention over the encoder states."), concepts)
    assert ok is False
    assert info["anchor_ids"] == []


def test_g3_rejects_transformer_alone():
    """Transformer 是通用架构词，不能当主题锚点。"""
    concepts = we._concept_terms("改进 Transformer 架构效率")
    ok, info = we._g3_verdict(_text_of("The transformer encoder stacks six layers."), concepts)
    assert ok is False
    assert info["anchor_ids"] == []


def test_g3_rejects_rag_alone():
    """RAG 单独命中不能通过——它只是「检索增强」这一个泛概念的缩写。"""
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(_text_of("We evaluate a RAG system on three tasks."), concepts)
    assert ok is False
    assert len({h["concept_id"] for h in info["hits"]}) == 1


@pytest.mark.parametrize(
    ("idea", "sentence", "expect_word"),
    [
        ("探索 Mamba 这类状态空间模型的能力边界", "Mamba is a selective state space model.", "mamba"),
        ("用 FlashAttention 降低显存占用", "FlashAttention is IO-aware and exact.", "flashattention"),
        ("用 BERT 做语义匹配", "We fine-tune BERT on the task.", "bert"),
    ],
)
def test_g3_passes_named_model_anchor(idea, sentence, expect_word):
    """具体模型名（Mamba / FlashAttention / BERT）必须仍然能通过，且走 strong_anchor。"""
    concepts = we._concept_terms(idea)
    ok, info = we._g3_verdict(_text_of(sentence), concepts)
    assert ok is True
    assert info["reason"] == "strong_anchor"
    assert expect_word in info["matched"]


def test_g3_gpt_is_topic_concept_not_strong_anchor():
    """GPT 形态像模型名，但含义过泛，只能算 topic_concept，不能单独放行。

    真问题：GPT-2/3/4、GPTs、以及大量「基于 GPT 的 XX」都在用这个词，
    单写 GPT 不指向某篇具体工作。真实索引复测里 GPT-4 Technical Report
    就是靠这类泛词补进候选池的。
    """
    concepts = we._concept_terms("基于 GPT 的零样本推理")
    gpt = next(c for c in concepts if "gpt" in c["frags"])
    assert gpt["tier"] == "topic_concept"
    assert gpt["why"] == "ambiguous_proper:gpt"

    ok, info = we._g3_verdict(_text_of("GPT is a decoder-only language model."), concepts)
    assert ok is False
    assert info["reason"] == "topic_only"
    assert info["anchor_ids"] == []


def test_g3_rejects_hallucination_alone():
    """**核心回归**：Hallucination 降为 topic_concept，单独命中必须拒绝。

    真问题：真实索引复测里补入的三篇（LLaVA / GPT-4 / Llama 3）
    **全部**只靠 hallucination 一个词过关，而它们讲的是通用幻觉缓解，
    不是「引用图谱约束的 RAG」。幻觉是每篇大模型论文都会提的词。
    """
    concepts = we._concept_terms(IDEA)
    halluc = next(c for c in concepts if "hallucinat" in c["frags"])
    assert halluc["tier"] == "topic_concept"

    ok, info = we._g3_verdict(_text_of("Large models suffer from hallucination."), concepts)
    assert ok is False
    assert info["reason"] == "topic_only"
    assert "hallucinat" in info["matched"]
    assert info["anchor_ids"] == []
    assert info["topic_ids"] == [halluc["concept_id"]]


def test_g3_passes_hallucination_with_another_topic_concept():
    """topic_concept 必须与另一个**不同且有意义的**主题概念共同命中才放行。

    这里 idea 同时含「幻觉」和「校验」两个 topic_concept，
    正文把两件事都讲了——这是真正的两个研究方向，应当通过。
    对照组：只讲幻觉、或幻觉 + citation 这种万金油组合，都不算。
    """
    concepts = we._concept_terms("研究幻觉检测与大模型输出校验的方法")
    ok, info = we._g3_verdict(
        _text_of("We verify each claim against evidence and detect hallucination."),
        concepts,
    )
    assert ok is True
    assert info["reason"] == "topic_concept_with_support"
    assert len(info["topic_ids"]) == 2


def test_g3_knowledge_graph_anchor_only_when_full_phrase():
    """「知识图谱」的锚点身份靠 ``knowledge graph`` 整串，光一个 graph 不算。

    真问题：``knowledge graph`` 是多词片段，``_frag_in`` 要求两词都出现。
    正文只写 ``graph`` 时，这个概念压根不算命中——
    落到的是「图谱」这个泛概念（generic），所以是单泛概念拒绝。
    """
    concepts = we._concept_terms("研究知识图谱增强的检索")

    ok_full, info_full = we._g3_verdict(
        _text_of("A knowledge graph stores structured facts about entities."), concepts
    )
    assert ok_full is True
    assert info_full["reason"] == "strong_anchor"

    ok_partial, info_partial = we._g3_verdict(_text_of("The graph has many nodes and edges."), concepts)
    assert ok_partial is False
    assert info_partial["anchor_ids"] == []
    # 「知识图谱」没命中（缺 knowledge），只剩「图谱」这一个泛概念
    assert info_partial["topic_ids"] == []
    assert len(info_partial["generic_ids"]) == 1


def test_g3_passes_two_distinct_strong_anchors():
    """两个真正不同的强锚点同时命中 → two_strong_anchors。"""
    concepts = we._concept_terms("研究 Mamba 与 FlashAttention 在长文档上的效率")
    text = _text_of("Mamba is a state space model; FlashAttention is an IO-aware attention.")
    ok, info = we._g3_verdict(text, concepts)
    assert ok is True
    assert info["reason"] == "two_strong_anchors"
    assert len(info["anchor_ids"]) == 2


def test_g3_rejects_single_generic_concept():
    """单个普通概念词不通过（原文语：宁可保守拒绝普通概念词）。"""
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(_text_of("We compare several retrieval strategies."), concepts)
    assert ok is False
    assert info["anchor_ids"] == []


def test_shared_rag_alias_counts_once_after_tiering():
    """同一英文词经多个中文映射合并后只计一次，分级后依然成立。"""
    concepts = we._concept_terms(IDEA)
    hits = we._topic_hits("We use rag for everything.", concepts)
    assert len(hits) == 1
    # 合并后「检索增强」吞掉了「增强生成」，两者共享 concept_id
    assert hits[0]["concept_id"] == 3


def test_rejects_understanding_deep_learning_page_255():
    """**验收样本**：Understanding Deep Learning 第 255 页真实文本必须拒绝。"""
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(UDL_P255, concepts)
    assert ok is False
    assert info["reason"] == "generic_only"
    assert info["anchor_ids"] == []


def test_rejects_understanding_deep_learning_page_256():
    """**验收样本**：第 256 页真实文本同样必须拒绝。"""
    concepts = we._concept_terms(IDEA)
    ok, info = we._g3_verdict(UDL_P256, concepts)
    assert ok is False
    assert info["anchor_ids"] == []


def test_real_body_text_not_mass_rejected():
    """真实正文不能被大面积误杀。

    真问题：分级提高了门槛，最怕的是把正常的 RAG 正文也挡掉。
    这里取三句真实的、讲 RAG 的正文（含 hallucination + 可追溯/校验），
    必须都能过。断言只要求「全部通过」，**不写死任何比例阈值**——
    写死阈值等于把某次采样的偶然数字固化成标准。
    """
    concepts = we._concept_terms(IDEA)
    bodies = [
        "Retrieval-augmented generation reduces hallucination by grounding generation "
        "in retrieved documents, which makes every sentence traceable to a real source.",
        "We observe that models still hallucinate even when the retrieved passage "
        "contains the answer; our tracing module records which source each sentence used "
        "and marks the untraceable spans for manual review.",
        "Our method detects hallucinated spans by comparing each generated sentence "
        "against the retrieved evidence and traces every claim back to its source paper.",
    ]
    for body in bodies:
        ok, info = we._g3_verdict(body, concepts)
        assert ok is True, f"误杀真实正文：{body[:40]}... reason={info['reason']}"


def test_concept_tier_assignments_are_stable():
    """真实 idea + 真实模型的 tier 分配必须与审计记录一致。

    这条锁的是「分级规则本身」，将来谁改了词表会立刻红。
    """
    tiers = {t["zh"]: t["tier"] for t in we._concept_terms(IDEA)}
    assert tiers["引用"] == "generic"
    assert tiers["图谱"] == "generic"
    assert tiers["检索增强"] == "generic"
    # 幻觉降为 topic_concept（2026-11-12 第二轮）：能承认「这块在谈幻觉」，
    # 但不能靠它一个词放行。
    assert tiers["幻觉"] == "topic_concept"


def test_root_matching_uses_word_boundary():
    """词根只按词边界 + 白名单后缀匹配，不做双向子串匹配。

    真问题：旧实现是 ``root in word or word in root``，于是 ``code``
    命中了 ``encode`` / ``decode``，``math`` 命中 ``aftermath``。
    这些词在 PDF 正文里到处都是，等于给任何论文开了一扇后门。
    """
    # 该命中的词形
    assert we._root_matches("hallucinat", "hallucination") is True
    assert we._root_matches("hallucinat", "hallucinations") is True
    assert we._root_matches("hallucinat", "hallucinated") is True
    assert we._root_matches("hallucinat", "hallucinating") is True
    assert we._root_matches("verif", "verification") is True
    assert we._root_matches("verif", "verify") is True
    assert we._root_matches("knowledge", "knowledge") is True
    # 词边界：encode / decode 不能算 code
    assert we._root_matches("code", "encode") is False
    assert we._root_matches("code", "decode") is False
    assert we._root_matches("math", "aftermath") is False
    # 词根必须出现在词首
    assert we._root_matches("verif", "unverified") is False
    # 词根 + 任意长尾巴不收
    assert we._root_matches("hallucinat", "hallucinatexyz") is False


def test_code_math_reason_not_strong_anchor():
    """code / math / reason 指向任务类型而非研究对象，不得作强锚点。

    真问题：这三词根在上一轮里是锚点，任何讲代码生成或数学推理的论文
    都能靠它们单独过 G3。
    """
    terms = we._concept_terms("研究代码生成与数学推理能力的评测")
    for t in terms:
        assert t["tier"] != "strong_anchor", f"{t['zh']} 不该是强锚点：{t['why']}"


def test_generic_partial_branch_is_gone():
    """``generic_partial`` 已删除：它是死分支，任何输入都触发不到。

    真问题：它想表达「锚点只命中片段里的泛词部分」，但 ``_frag_in``
    对多词片段要求全部词出现，这种「部分命中」根本进不了 ``hits``。
    """
    concepts = we._concept_terms(IDEA)
    _, info = we._g3_verdict(_text_of("The graph has nodes."), concepts)
    assert "partial_ids" not in info
    assert info["reason"] != "generic_partial"
    assert we._new_tally().get("g3_generic_partial", 0) == 0


def test_multiword_frag_matches_split_words():
    """多词片段要拆词全含才算命中，不能整串子串匹配。

    真问题：``knowledge graph`` / ``long document`` / ``question answering``
    三个字典词都是多词片段，整串匹配在真实正文里几乎打不中
    （PDF 断行、单复数都会变），等于这三个词一直没生效过。
    """
    assert we._frag_in("knowledge graph", "a knowledge graph stores facts") is True
    assert we._frag_in("knowledge graph", "the graph has nodes") is False
    assert we._frag_in("long document", "long-document qa works") is True
    assert we._frag_in("question answering", "question answering over documents") is True
    assert we._frag_in("hallucinat", "hallucination is common") is True