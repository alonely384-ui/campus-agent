"""1.6.1 文献检索层（app/literature.py）的回归测试。

覆盖三处实测驱动的改动，全部有真实数据支撑，改动理由见各 case 的 docstring：

* P0 概念 AND 表达式 + 降级放宽（``_english_expr`` / ``_relax_api_expr`` /
  ``search_arxiv`` 打空后的逐级放宽）
* P1 覆盖缺口判据（``pool_coverage_gaps`` 曾恒返回 ``[]``）
* P3 OpenAlex 第四来源（``search_openalex`` / ``_openalex_abstract``）

一个网络请求都不发：``_client`` 被换成假对象，缓存目录也被指到 tmp_path。
"""

import json

import pytest

from app import literature as lit
from app.literature import Candidate


def _cand(title: str, source: str = lit.SOURCE_ARXIV, **kw) -> Candidate:
    return Candidate(key="", title=title, source=source, **kw)


class _FakeResp:
    def __init__(self, payload=None, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """最小 httpx.Client 替身：记录请求参数，按预设返回。"""

    def __init__(self, responses: list[_FakeResp]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get(self, url: str, params: dict | None = None) -> _FakeResp:
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            return _FakeResp({}, 500)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch, tmp_path):
    """缓存指到临时目录，避免测试之间互相喂缓存、也避免污染 data/meta/online。"""
    monkeypatch.setattr(lit, "CACHE_DIR", tmp_path / "online")


# --------------------------------------------------------------------- P0 表达式


def test_english_exprs_keeps_phrases_whole() -> None:
    """概念短语必须整段进引号，不能按空格拆成散词。

    散词版本实测只有 5/12 命中，且会被 ``_arxiv_expr`` 截断丢词。
    三个概念会产出 C(3,2)=3 条两两组合，而不是一条三概念 AND——
    实测三概念 AND 几乎必然全灭（见 app/writing.py 里 _english_exprs 的注释）。
    """
    from app import writing as w

    exprs = w._english_exprs(
        ["retrieval augmented generation", "citation graph", "hallucination"]
    )
    assert exprs == [
        'all:"retrieval augmented generation" AND all:"citation graph"',
        'all:"retrieval augmented generation" AND all:"hallucination"',
        'all:"citation graph" AND all:"hallucination"',
    ]


def test_english_exprs_normalizes_hyphens_to_spaces() -> None:
    """连字符必须换成空格：arXiv 对 ``retrieval-augmented`` 查不到东西。

    实测 ``all:"retrieval-augmented generation" AND all:"citation graph"`` → 0 条，
    换成空格 → 5 条（命中 GRAG / CG-RAG / SciRAG 三篇正主）。
    """
    from app import writing as w

    exprs = w._english_exprs(["retrieval-augmented generation", "citation-graph"])
    assert exprs == ['all:"retrieval augmented generation" AND all:"citation graph"']


def test_english_exprs_are_always_pairs() -> None:
    """每条表达式恰好两个概念：三概念实测 0 条，所以组合只做两两配对。"""
    from app import writing as w

    assert w._CONCEPT_MAX == 2
    exprs = w._english_exprs(["alpha beta", "gamma delta", "epsilon zeta", "eta theta"])
    # 4 个概念 → C(4,2) = 6 条，每条 1 个 AND
    assert len(exprs) == 6
    for e in exprs:
        assert e.count(" AND ") == 1


def test_english_exprs_caps_source_concepts() -> None:
    """参与配对的概念数有上限，否则组合数爆炸拖慢生成。"""
    from app import writing as w

    exprs = w._english_exprs(
        [f"concept{i} word{i}" for i in range(10)]
    )
    assert len(exprs) == w._PAIR_SOURCE_MAX * (w._PAIR_SOURCE_MAX - 1) // 2


def test_english_exprs_dedupes_and_drops_stopwords() -> None:
    from app import writing as w

    # 只剩一个可用概念时退化成单概念查询（AND 需要两个操作数）
    assert w._english_exprs(["citation graph", "Citation Graph", "framework"]) == [
        'all:"citation graph"'
    ]


def test_english_exprs_empty_when_nothing_usable() -> None:
    from app import writing as w

    assert w._english_exprs([]) == []
    assert w._english_exprs(["", "  ", "???"]) == []


def test_arxiv_expr_passes_boolean_expression_through() -> None:
    """已经拼好的布尔表达式必须原样透传，再切词会把它拆坏。"""
    expr = 'all:"RAG" AND all:"citation graph"'
    assert lit._arxiv_expr(expr) == expr


def test_arxiv_expr_still_builds_expression_for_plain_keywords() -> None:
    """普通关键词查询保持老行为：首词限标题 + 其余 all:。"""
    expr = lit._arxiv_expr("citation graph recommendation")
    assert expr.startswith('ti:"citation"')
    assert 'all:"graph"' in expr


def test_relax_api_expr_drops_concepts_one_at_a_time() -> None:
    """降级链：3 个概念 → 2 个概念。不到 2 个就不再降。"""
    expr = 'all:"a b" AND all:"c d" AND all:"e f"'
    assert lit._relax_api_expr(expr) == ['all:"a b" AND all:"c d"']


def test_relax_api_expr_returns_empty_for_single_concept() -> None:
    """只剩一个宽泛概念时不降级——那会把候选池冲成无关论文，比空池更糟。"""
    assert lit._relax_api_expr('all:"retrieval augmented generation"') == []


def test_search_arxiv_relaxes_when_primary_expression_is_empty(monkeypatch) -> None:
    """实测：3 概念 AND 打空（0 条），放宽到 2 概念后能拿回 5 条，其中 3 篇正主。"""
    empty = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    )
    hit = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><id>http://arxiv.org/abs/2401.00001v1</id>"
        "<title>CG-RAG: Research Question Answering by Citation Graph Retrieval</title>"
        "<published>2024-01-01T00:00:00Z</published></entry></feed>"
    )
    fake = _FakeClient([_FakeResp(text=empty), _FakeResp(text=hit)])
    monkeypatch.setattr(lit, "_client", lambda: fake)

    rows = lit.search_arxiv('all:"a b" AND all:"c d" AND all:"e f"', limit=5)

    assert [c.title for c in rows] == [
        "CG-RAG: Research Question Answering by Citation Graph Retrieval"
    ]
    sent = [call[1]["search_query"] for call in fake.calls]
    assert sent == ['all:"a b" AND all:"c d" AND all:"e f"', 'all:"a b" AND all:"c d"']


def test_search_arxiv_does_not_relax_plain_keyword_query(monkeypatch) -> None:
    """普通关键词查询打空就是打空，不要为它跑一次多余的降级请求。"""
    empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    fake = _FakeClient([_FakeResp(text=empty)])
    monkeypatch.setattr(lit, "_client", lambda: fake)

    assert lit.search_arxiv("citation graph", limit=5) == []
    assert len(fake.calls) == 1


# --------------------------------------------------------------------- P1 覆盖缺口


def test_pool_coverage_gaps_reports_missing_concept() -> None:
    """池子全是不相关论文时，必须把 idea 的概念都报成缺口。"""
    idea = "基于检索增强生成的学术问答系统：引入引用图谱约束以降低幻觉"
    pool = [
        _cand("Attention Is All You Need"),
        _cand("FlashAttention: Fast and Memory-Efficient Exact Attention"),
    ]
    assert lit.pool_coverage_gaps(idea, pool) != []


def test_pool_coverage_gaps_empty_when_a_single_paper_covers_it() -> None:
    """有一篇标题同时命中文档概念的所有词，就算覆盖。"""
    idea = "引入引用图谱约束以降低幻觉"
    pool = [
        _cand(
            "CG-RAG: Research Question Answering by Citation Graph Retrieval-Augmented Generation"
        )
    ]
    assert lit.pool_coverage_gaps(idea, pool) == []


def test_covered_requires_all_words_in_the_same_title() -> None:
    """复合概念不能「citation 在 A 篇、graph 在 B 篇」就算覆盖。

    这是 P1 的核心：旧实现把所有标题拼成一个大字符串再查，
    于是任何一篇含 ``retrieval`` 就等于「检索增强已覆盖」，函数恒为真。
    """
    titles = ["citation networks revisited", "graph neural networks"]
    assert lit._covered("citation graph", titles) is False
    assert lit._covered("citation graph", ["citation graph retrieval"]) is True


def test_pool_coverage_gaps_ignores_abstract() -> None:
    """摘要太泛（几乎每篇 RAG 论文都写 retrieval/graph），只认标题。"""
    idea = "引用图谱约束"
    pool = [
        _cand(
            "A Generic Survey",
            abstract="retrieval augmented generation citation graph everywhere",
        )
    ]
    assert lit.pool_coverage_gaps(idea, pool) != []


def test_pool_coverage_gaps_empty_pool() -> None:
    assert lit.pool_coverage_gaps("引用图谱约束", []) == []


# --------------------------------------------------------------------- P3 OpenAlex


def test_openalex_abstract_restores_word_order() -> None:
    """倒排索引是「词 → 位置」，必须按位置拼回正常语序。"""
    inverted = {
        "Naive": [0],
        "RAG": [1],
        "focuses": [2],
        "on": [3],
        "documents": [4],
    }
    assert lit._openalex_abstract(inverted) == "Naive RAG focuses on documents"


def test_openalex_abstract_handles_repeated_words() -> None:
    """同一个词出现多次时，位置列表里会有多个下标。"""
    inverted = {"graph": [0, 2], "the": [1]}
    assert lit._openalex_abstract(inverted) == "graph the graph"


def test_openalex_abstract_empty() -> None:
    assert lit._openalex_abstract(None) == ""
    assert lit._openalex_abstract({}) == ""


def test_parse_openalex_strips_doi_prefix() -> None:
    """OpenAlex 的 doi 是完整链接，Candidate.doi 约定是裸 DOI。"""
    payload = {
        "results": [
            {
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1145/3777378",
                "title": "Graph Retrieval-Augmented Generation: A Survey",
                "publication_year": 2025,
                "authorships": [
                    {"author": {"display_name": "Yuntong Hu"}},
                    {"author": {"display_name": "Zhihan Lei"}},
                ],
                "primary_location": {"source": {"display_name": "ACM Computing Surveys"}},
                "abstract_inverted_index": {"Graph": [0], "RAG": [1]},
            }
        ]
    }
    rows = lit._parse_openalex(payload)
    assert len(rows) == 1
    c = rows[0]
    assert c.doi == "10.1145/3777378"
    assert c.source == lit.SOURCE_OPENALEX
    assert c.year == 2025
    assert c.authors == ["Yuntong Hu", "Zhihan Lei"]
    assert c.venue == "ACM Computing Surveys"
    assert c.abstract == "Graph RAG"
    assert c.url == "https://doi.org/10.1145/3777378"


def test_parse_openalex_skips_entries_without_title() -> None:
    rows = lit._parse_openalex({"results": [{"id": "W1", "title": ""}, {"title": "Ok"}]})
    assert [c.title for c in rows] == ["Ok"]


def test_search_openalex_returns_candidates(monkeypatch) -> None:
    payload = {
        "results": [
            {
                "title": "Retrieval-Augmented Generation with Graphs (GraphRAG)",
                "doi": "https://doi.org/10.48550/arxiv.2501.00309",
                "publication_year": 2024,
                "abstract_inverted_index": {"RAG": [0], "graphs": [1]},
            }
        ]
    }
    fake = _FakeClient([_FakeResp(payload=payload)])
    monkeypatch.setattr(lit, "_client", lambda: fake)

    rows = lit.search_openalex("graph rag", limit=3)

    assert [c.title for c in rows] == ["Retrieval-Augmented Generation with Graphs (GraphRAG)"]
    url, params = fake.calls[0]
    assert url == lit.OPENALEX_API
    assert params["search"] == "graph rag"
    assert params["mailto"] == lit.OPENALEX_MAILTO


@pytest.mark.parametrize("status", [429, 500, 503])
def test_search_openalex_degrades_on_error(monkeypatch, status: int) -> None:
    """补漏来源挂了只能返回空列表，不能把整个候选池构建带崩。"""
    fake = _FakeClient([_FakeResp({}, status)])
    monkeypatch.setattr(lit, "_client", lambda: fake)
    assert lit.search_openalex("graph rag", limit=3) == []


def test_search_openalex_caches_result(monkeypatch) -> None:
    """第二次同样查询走缓存，不再打网络。"""
    payload = {"results": [{"title": "Cached Paper", "publication_year": 2024}]}
    fake = _FakeClient([_FakeResp(payload=payload)])
    monkeypatch.setattr(lit, "_client", lambda: fake)

    first = lit.search_openalex("cached query", limit=3)
    second = lit.search_openalex("cached query", limit=3)

    assert [c.title for c in first] == ["Cached Paper"]
    assert [c.title for c in second] == ["Cached Paper"]
    assert len(fake.calls) == 1


def test_openalex_sits_last_in_source_priority() -> None:
    """P3 决策：新源排在本库 > arXiv > Crossref 之后，只做补漏。"""
    rows = [
        _cand("Same Title", lit.SOURCE_OPENALEX),
        _cand("Same Title", lit.SOURCE_CROSSREF),
        _cand("Same Title", lit.SOURCE_ARXIV),
    ]
    out = lit._dedupe(rows)
    assert [c.source for c in out] == [lit.SOURCE_ARXIV]


def test_build_candidates_queries_openalex(monkeypatch) -> None:
    """build_candidates 必须真的去问 OpenAlex，而不是只加了个参数。"""
    monkeypatch.setattr(lit, "search_local", lambda *_a, **_k: [])
    monkeypatch.setattr(lit, "search_arxiv", lambda *_a, **_k: [])
    monkeypatch.setattr(lit, "search_crossref", lambda *_a, **_k: [])
    seen: list[str] = []

    def fake_openalex(query, limit=8):
        seen.append(query)
        return [_cand(f"OA {query}", lit.SOURCE_OPENALEX)]

    monkeypatch.setattr(lit, "search_openalex", fake_openalex)

    pool = lit.build_candidates(["query one", "query two"], total_limit=16)

    assert seen == ["query one", "query two"]
    assert [c.key for c in pool] == ["C1", "C2"]
    assert all(c.source == lit.SOURCE_OPENALEX for c in pool)


def test_candidate_to_dict_includes_openalex_source() -> None:
    """来源字段会被序列化进缓存，再读回来必须还是 openalex。"""
    c = _cand("X", lit.SOURCE_OPENALEX, year=2025)
    restored = Candidate(**json.loads(json.dumps(c.to_dict())))
    assert restored.source == lit.SOURCE_OPENALEX


# --------------------------------------------------------- 相关性门槛（词袋源）

# 以下四条锁住 build_candidates 的两道过滤，理由见 app/literature.py 里
# ``_keep_relevant`` 的 docstring：它们都是真实网络跑出来才暴露的漏网情况。


def test_relevance_terms_splits_camel_case() -> None:
    """``NativeSparseAttention`` 不先按 camelCase 拆，整串在标题里永远匹配不上。"""
    assert lit._relevance_terms("NativeSparseAttention") == [
        "native",
        "sparse",
        "attention",
    ]


def test_relevance_terms_keeps_terms_when_query_is_all_generic() -> None:
    """``long document question answering`` 全是泛词，不能退化成空列表。

    词条为空时 ``_keep_relevant`` 会直接放行，门槛静默失效。
    """
    terms = lit._relevance_terms("long document question answering")
    assert terms, "全泛词查询至少应退回泛词本身"
    assert set(terms) == {"long", "document", "question", "answering"}


def test_keep_relevant_drops_fragment_titles() -> None:
    """Crossref 把正文表号当独立文献收录，标题命中词一个不少，必须单独挡。"""
    rows = [
        _cand("Table 5: Computational overhead comparison."),
        _cand("Figure 2: Sparse matrix multiplication to minimize computational overhead."),
        _cand("Supplemental Information 13: Computational Overhead of Fuzzy Optimization."),
        _cand("Computational overhead in sparse attention"),
    ]
    kept = lit._keep_relevant(rows, ["computational", "overhead"], None)
    assert [c.title for c in kept] == ["Computational overhead in sparse attention"]


def test_keep_relevant_topic_gate_only_for_modifier_phrase() -> None:
    """``computational overhead`` 这一列必须贴主题；普通列不能被主题词卡死。

    实测前者在 OpenAlex 回 MPC 密码学论文，后者
    （``long document question answering``）的标题里根本不会出现 sparse/attention。
    """
    mpc = _cand("Cryptography with constant computational overhead")
    # 不带主题词时，它自己那列词条是命中的，会被放行
    assert len(lit._keep_relevant([mpc], ["computational", "overhead"], None)) == 1
    # 带上「稀疏注意力」主题词后必须挡掉
    assert lit._keep_relevant([mpc], ["computational", "overhead"], ["sparse", "attention"]) == []


def test_build_candidates_skips_bag_sources_for_cjk_query(monkeypatch) -> None:
    """中文查询只走本库与 arXiv：Crossref/OpenAlex 不做中文分词，回的全是噪声。"""
    calls = {"crossref": 0, "openalex": 0, "arxiv": 0}

    def fake_crossref(*_a, **_k):
        calls["crossref"] += 1
        return [_cand("水文计算在小型水库除险加固设计中需注意的问题", lit.SOURCE_CROSSREF)]

    def fake_openalex(*_a, **_k):
        calls["openalex"] += 1
        return [_cand("小学低年级学生注意力问题的成因与干预策略", lit.SOURCE_OPENALEX)]

    def fake_arxiv(*_a, **_k):
        calls["arxiv"] += 1
        return []

    monkeypatch.setattr(lit, "search_local", lambda *_a, **_k: [])
    monkeypatch.setattr(lit, "search_crossref", fake_crossref)
    monkeypatch.setattr(lit, "search_openalex", fake_openalex)
    monkeypatch.setattr(lit, "search_arxiv", fake_arxiv)

    pool = lit.build_candidates(["稀疏注意力降低长文档问答中的计算开销"])

    assert calls == {"crossref": 0, "openalex": 0, "arxiv": 1}
    assert pool == []


def test_build_candidates_gates_modifier_column_on_all_bag_sources(monkeypatch) -> None:
    """修饰短语列在三个词袋源上都要清空，不能漏掉 arXiv。

    实测 ``computational overhead`` 的 arXiv 列回的是
    ``Low overhead quantum computation using lattice surgery`` 这类量子计算论文，
    5 条占了 16 个名额的 5 个。
    """
    def fake_crossref(*_a, **_k):
        return [_cand("Table 5: Computational overhead comparison.", lit.SOURCE_CROSSREF)]

    def fake_openalex(*_a, **_k):
        return [_cand("Cryptography with constant computational overhead", lit.SOURCE_OPENALEX)]

    def fake_arxiv(*_a, **_k):
        return [_cand("Low overhead quantum computation using lattice surgery")]

    monkeypatch.setattr(lit, "search_local", lambda *_a, **_k: [])
    monkeypatch.setattr(lit, "search_crossref", fake_crossref)
    monkeypatch.setattr(lit, "search_openalex", fake_openalex)
    monkeypatch.setattr(lit, "search_arxiv", fake_arxiv)

    pool = lit.build_candidates(["sparse attention", "computational overhead"])

    assert pool == []