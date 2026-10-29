"""引用图谱（赛题 1.5）的单元测试。

全部用假数据跑 `build_graph(rows=..., refs_loader=...)`，不碰真实 PDF、不碰索引，
所以跑测试不会影响 RAG 检索，也不依赖本机有没有那 39 篇论文。
"""

from __future__ import annotations

from app import graph


def _row(filename: str, title: str, arxiv_id: str = "", abstract: str = "") -> dict:
    return {"filename": filename, "title": title, "arxiv_id": arxiv_id, "abstract": abstract}


def _entry(title: str = "", arxiv_id: str = "", raw: str = "", index: int = 1):
    return graph.ReferenceEntry(index=index, raw=raw or f"{title} {arxiv_id}", title=title, arxiv_id=arxiv_id)


def _build(rows, refs, **kwargs):
    return graph.build_graph(rows=rows, refs_loader=lambda name: refs.get(name, []), **kwargs)


# ---------------- 重复论文归并 ----------------
def test_three_attention_copies_merge_into_one_node():
    """要求 5-1：bdfaa68d8984.pdf / 1706.03762v1.pdf / 1706.03762v7.pdf 合成一个逻辑节点。"""
    rows = [
        _row("1706.03762v1.pdf", "Attention Is All You Need", "1706.03762"),
        _row("1706.03762v7.pdf", "Attention Is All You Need", "1706.03762"),
        # 重名导入的那份，文件名不是 arXiv 号，只能靠标题认出来
        _row("bdfaa68d8984.pdf", "Attention is all you need", ""),
    ]
    data = _build(rows, {})

    assert data["summary"]["node_count"] == 1
    node = data["nodes"][0]
    assert node["canonical_id"] == "arxiv:1706.03762"
    assert node["copy_count"] == 3
    assert set(node["copies"]) == {"1706.03762v1.pdf", "1706.03762v7.pdf", "bdfaa68d8984.pdf"}
    # 合并决定要留下依据，答辩时能解释"凭什么认成同一篇"
    bases = {d["filename"]: d["basis"] for d in data["merge_decisions"] if d["action"] == "并入"}
    assert bases["bdfaa68d8984.pdf"] == "标题精确匹配"


def test_different_versions_of_same_arxiv_not_duplicated():
    """要求 5-2：同一 arXiv 号的不同版本不会各建一个节点。"""
    rows = [
        _row("2005.14165v3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
        _row("2005.14165v4.pdf", "Language Models are Few-Shot Learners: Some Subtitle", "2005.14165"),
    ]
    data = _build(rows, {})
    assert data["summary"]["node_count"] == 1
    assert data["nodes"][0]["copy_count"] == 2


def test_arxiv_id_normalisation_handles_prefix_space_and_version():
    """要求 3-1：arXiv: 前缀 / 空格 / 版本号，规范化后是同一个基础号。"""
    assert graph.norm_arxiv("arXiv:1706.03762v7") == "1706.03762"
    assert graph.norm_arxiv("1706.03762 v1") == "1706.03762"
    assert graph.norm_arxiv("arXiv：2201.11903") == "2201.11903"  # 全角冒号
    assert graph.norm_arxiv("no id here") == ""


# ---------------- 标题匹配 ----------------
def test_title_case_and_punctuation_do_not_cause_miss():
    """要求 5-3：大小写、标点、连字差异不应造成漏匹配。"""
    rows = [
        _row("a.pdf", "LLaMA: Open and Efficient Foundation Language Models", "2302.13971"),
        _row("b.pdf", "Attention Is All You Need", "1706.03762"),
    ]
    refs = {
        "a.pdf": [
            # 全小写 + 去掉冒号
            _entry(title="llama open and efficient foundation language models"),
            # 原文里的 "LAN-\xadGUAGE" 那种软连字符写法
            _entry(title="Attention Is All You Nee\u00add"),
        ]
    }
    data = _build(rows, refs)
    methods = {(e["source"], e["target"]): e["match_method"] for e in data["edges"]}
    assert methods[("arxiv:2302.13971", "arxiv:1706.03762")] == "title_exact"
    assert len(data["edges"]) == 1


def test_same_first_60_chars_not_merged():
    """要求 5-4 / 3-2：不能只因为标题前 60 个字符相同就当成同一篇。"""
    shared = "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness"
    rows = [
        _row("fa1.pdf", shared, "2205.14135"),
        _row("fa2.pdf", shared + " for Long Sequences", "2401.99999"),
    ]
    data = _build(rows, {})
    assert data["summary"]["node_count"] == 2
    # 反向验证：前 60 个字符确实一样，节点仍然是两个
    n1, n2 = data["nodes"]
    assert graph.norm_title(n1["title"])[:60] == graph.norm_title(n2["title"])[:60]


def test_unconfirmed_fuzzy_title_does_not_create_edge():
    """要求 5-5 / 3-3：无法确认的近似标题不直接出边，只记待人工核验。

    `Survey of LLMs` vs `Survey of Large Language Models` 主题相近但不是同一篇（相似度 0.9688），
    把接受线抬到 0.98 时必须拒绝自动连边，只留待人工核验。
    """
    rows = [
        _row("src.pdf", "Survey of LLMs", "2401.00001"),
        _row("dst.pdf", "Survey of Large Language Models", "2303.18223"),
    ]
    refs = {"src.pdf": [_entry(title="A Survey of Large Language Models")]}
    data = _build(rows, refs, fuzzy_accept=0.98, fuzzy_review=0.6)

    assert data["edges"] == []                      # 没到接受阈值 → 不出边
    assert len(data["reviews"]) == 1                # 但要进待人工核验
    review = data["reviews"][0]
    assert review["source"] == "arxiv:2401.00001"
    assert review["candidate"] == "arxiv:2303.18223"
    assert 0.6 <= review["match_score"] < 0.98
    assert review["match_method"] == "title_fuzzy"
    assert review["reference_title"] == "A Survey of Large Language Models"


def test_fuzzy_match_above_threshold_creates_edge_with_score():
    """阈值可配：同一对候选，接受阈值放到 0.90 就出边，并记录 match_score / match_method。"""
    rows = [
        _row("src.pdf", "Survey of LLMs", "2401.00001"),
        _row("dst.pdf", "Survey of Large Language Models", "2303.18223"),
    ]
    refs = {"src.pdf": [_entry(title="A Survey of Large Language Models")]}
    data = _build(rows, refs, fuzzy_accept=0.90, fuzzy_review=0.6)
    assert len(data["edges"]) == 1
    edge = data["edges"][0]
    assert edge["match_method"] == "title_fuzzy"
    assert edge["match_score"] >= 0.90
    assert edge["target"] == "arxiv:2303.18223"


def test_self_reference_does_not_fall_through_to_fuzzy():
    """自引陷阱：条目精确命中"源论文自己"时，不许掉进模糊匹配去撞标题相近的别家论文。"""
    rows = [
        _row("src.pdf", "A Survey of Large Language Models", "2303.18223"),
        _row("dst.pdf", "Survey of Large Language Models", "2401.00001"),
    ]
    # 源论文的参考文献里引的是它自己（综述/勘误常见）
    refs = {"src.pdf": [_entry(title="A Survey of Large Language Models")]}
    data = _build(rows, refs, fuzzy_accept=0.95, fuzzy_review=0.75)
    assert data["edges"] == []
    assert data["reviews"] == []
    assert data["summary"]["refs"]["self_reference"] == 1


# ---------------- 计数 ----------------
def test_same_relation_not_double_counted_across_copies():
    """要求 5-6：同一引用关系不因为副本重复而重复计数。"""
    rows = [
        _row("1706.03762v1.pdf", "Attention Is All You Need", "1706.03762"),
        _row("1706.03762v7.pdf", "Attention Is All You Need", "1706.03762"),
        _row("bdfaa68d8984.pdf", "Attention is all you need", ""),
        _row("gpt3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
    ]
    # 三份 Attention 副本各自都引用了 GPT-3 → 只该算一条边
    refs = {
        "1706.03762v1.pdf": [_entry(title="Language Models are Few-Shot Learners", arxiv_id="2005.14165")],
        "1706.03762v7.pdf": [_entry(title="Language models are few-shot learners", arxiv_id="2005.14165")],
        "bdfaa68d8984.pdf": [_entry(title="Language models are few shot learners", arxiv_id="2005.14165")],
    }
    data = _build(rows, refs)
    assert data["summary"]["edge_count"] == 1
    assert data["summary"]["cited_paper_count"] == 1
    assert data["summary"]["citing_paper_count"] == 1
    gpt3 = [n for n in data["nodes"] if n["canonical_id"] == "arxiv:2005.14165"][0]
    assert gpt3["cited_by_count"] == 1


def test_three_metrics_are_distinct():
    """要求 4：引用边数 / 被引用论文数 / 不同引用论文数 必须分开统计。"""
    rows = [
        _row("p1.pdf", "Paper One", "2401.00001"),
        _row("p2.pdf", "Paper Two", "2401.00002"),
        _row("p3.pdf", "Paper Three", "2401.00003"),
    ]
    # p1 引两篇，p2 引同一篇 p3：边 3 条，被引论文 2 篇，引用论文 2 篇
    refs = {
        "p1.pdf": [
            _entry(title="Paper Two", arxiv_id="2401.00002"),
            _entry(title="Paper Three", arxiv_id="2401.00003"),
        ],
        "p2.pdf": [_entry(title="Paper Three", arxiv_id="2401.00003")],
    }
    s = _build(rows, refs)["summary"]
    assert s["edge_count"] == 3
    assert s["cited_paper_count"] == 2
    assert s["citing_paper_count"] == 2


def test_isolated_node_is_still_listed():
    """要求 5-7：孤立节点仍然正常展示（不被过滤掉）。"""
    rows = [
        _row("p1.pdf", "Paper One", "2401.00001"),
        _row("p2.pdf", "Paper Two", "2401.00002"),
        _row("lonely.pdf", "A Paper Nobody Cites", "2401.00003"),
    ]
    refs = {"p1.pdf": [_entry(title="Paper Two", arxiv_id="2401.00002")]}
    data = _build(rows, refs)
    assert data["summary"]["isolated_count"] == 1
    lonely = [n for n in data["nodes"] if n["canonical_id"] == "arxiv:2401.00003"][0]
    assert lonely["isolated"] is True
    assert lonely["cited_by_count"] == 0
    assert lonely["citation_edge_count"] == 0
    assert "arxiv:2401.00003" in data["isolated"]
    # 孤立节点照样参与 PageRank 排名，分数 > 0
    assert lonely["pagerank"] > 0


def test_edge_fields_complete():
    """要求 3：每条边至少记录 source / target / match_method / match_score / reference_title / reference_arxiv_id。"""
    rows = [
        _row("a.pdf", "Source Paper", "2401.00001"),
        _row("b.pdf", "Target Paper", "2401.00002"),
    ]
    refs = {"a.pdf": [_entry(title="Target Paper", arxiv_id="2401.00002")]}
    edge = _build(rows, refs)["edges"][0]
    for key in ("source", "target", "match_method", "match_score", "reference_title", "reference_arxiv_id"):
        assert key in edge
    assert edge["source"] == "arxiv:2401.00001"
    assert edge["target"] == "arxiv:2401.00002"
    assert edge["reference_arxiv_id"] == "2401.00002"


def test_edges_reference_logical_node_ids_not_filenames():
    """要求 2：边引用逻辑节点 ID，而不是 PDF 文件名。"""
    rows = [
        _row("1706.03762v1.pdf", "Attention Is All You Need", "1706.03762"),
        _row("1706.03762v7.pdf", "Attention Is All You Need", "1706.03762"),
        _row("bdfaa68d8984.pdf", "Attention is all you need", ""),
        _row("gpt3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
    ]
    refs = {"gpt3.pdf": [_entry(title="Attention is all you need")]}
    data = _build(rows, refs)
    ids = {n["canonical_id"] for n in data["nodes"]}
    for edge in data["edges"]:
        assert edge["source"] in ids and edge["target"] in ids
        assert not edge["source"].endswith(".pdf") and not edge["target"].endswith(".pdf")


def test_pagerank_based_on_dedup_nodes():
    """要求 4：PageRank 基于去重后的节点算，副本多不会把分数摊薄。"""
    base_rows = [
        _row("src.pdf", "Source Paper", "2401.00001"),
        _row("t.pdf", "Target Paper", "2401.00002"),
    ]
    dup_rows = base_rows + [_row("t2.pdf", "Target Paper", "2401.00002")]
    refs = {"src.pdf": [_entry(title="Target Paper", arxiv_id="2401.00002")]}

    one = _build(base_rows, refs)
    dup = _build(dup_rows, refs)
    rank_one = [n for n in one["nodes"] if n["canonical_id"] == "arxiv:2401.00002"][0]["pagerank"]
    rank_dup = [n for n in dup["nodes"] if n["canonical_id"] == "arxiv:2401.00002"][0]["pagerank"]
    assert rank_one == rank_dup


def test_unknown_duplicate_kept_separate():
    """要求 2：认不出的不强行合并（标题不同、编号不同的两份必须分开）。"""
    rows = [
        _row("x.pdf", "Alpha Beta Gamma", ""),
        _row("y.pdf", "Delta Epsilon Zeta", ""),
    ]
    data = _build(rows, {})
    assert data["summary"]["node_count"] == 2


def test_reference_title_extraction_keeps_title_intact():
    """别再出现"标题被截成作者名碎片"：作者段里的缩写点不该把标题切坏。"""
    split = graph._split_ref_title
    assert split(
        "Colin Raffel, Noam Shazeer, and Peter J. Liu. Exploring the limits of transfer learning with a unified text-to-text transformer."
    ) == "Exploring the limits of transfer learning with a unified text-to-text transformer"
    assert split("Jared Kaplan, Sam McCandlish. 2020. Scaling laws for neural language models.") == (
        "Scaling laws for neural language models"
    )
    assert split("Ashish Vaswani, Noam Shazeer. Attention is all you need. In NIPS, 2017.") == (
        "Attention is all you need"
    )


def test_reference_title_not_truncated_by_venue():
    """标题内部/尾部带句点（出处、版本号）时，不能把标题切掉一半。

    实测来源：Attention 第 1 条被整条丢掉、GPT-3 的 "Sentiwordnet 3.0: an enhanced lexical ..."
    被截成 "Sentiwordnet 3.0"，都是这个原因——旧逻辑一遇到标题段就停。
    """
    split = graph._split_ref_title
    # 标题后面跟 "arXiv preprint arXiv:1607.06450, 2016."，旧逻辑只有 "Layer normalization"
    assert split(
        "Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. "
        "Layer normalization. arXiv preprint arXiv:1607.06450, 2016."
    ).startswith("Layer normalization")
    # 标题段里有冒号 + 大写开头的后半句，旧逻辑会停在 "Sentiwordnet 3.0"
    senti = split(
        "Stefano Baccianella, Andrea Esuli, and Fabrizio Sebastiani. "
        "Sentiwordnet 3.0: an enhanced lexical resource for sentiment analysis and opinion mining. LREC, 2010."
    )
    assert "sentiment analysis" in senti


def test_continuation_marks_unfinished_entries():
    """续行判定：断在虚词 / 出处词 / 小写词上的行必须算"没写完"。

    背景（实测）：Attention 的 "[1] ... Layer normalization. arXiv preprint" 下一行
    "arXiv:1607.06450, 2016." 是续行，旧判定把它当独立条目，续行被丢掉。
    """
    cont = graph._is_continuation
    assert cont("Jimmy Lei Ba, and Geoffrey E Hinton. Layer normalization. arXiv preprint")
    assert cont("Dzmitry Bahdanau, and Yoshua Bengio. Neural machine translation by jointly")
    assert cont("Kaiming He, and Jian Sun. Deep residual learning for im-")
    assert cont("Jason Wei, Xuezhi Wang, and Rewon Child,")
    # 完整条目不能被误判成断行
    assert not cont("Ke Yang, Quoc V Le, et al. Large scale distributed deep networks. In NIPS, 2012.")
    assert not cont("Ilya Sutskever, Oriol Vinyals, and Quoc V Le.")


def test_column_split_tail_reattached_by_layout():
    """分栏把标题切断时，右栏底部的尾巴要靠版面坐标接回去，而不是另起一条。"""
    tail = {"page": 0, "x": 120.0, "y": 700.0, "text": "Kaiming He. Deep residual learning for im-"}
    head = {"page": 0, "x": 32.0, "y": 60.0, "text": "age recognition. In CVPR, 2016."}
    # 同栏（左栏）里的下一行：不是"右栏顶行"，不触发接续
    same_col = {"page": 0, "x": 110.0, "y": 712.0, "text": "Sepp Hochreiter. Long short-term memory."}
    # 别的页：不接
    other_page = {"page": 1, "x": 32.0, "y": 60.0, "text": "age recognition. In CVPR, 2016."}
    cols = {0: 32.0}
    assert graph._locale_text(tail, head, cols) is True
    assert graph._locale_text(tail, same_col, cols) is False
    assert graph._locale_text(tail, other_page, cols) is False


# ---------------- 基石节点打分（1.5「基石节点挖掘」） ----------------
def _node(nid: str, cited_by: int, pagerank: float) -> dict:
    """构造图算法要的最小节点形状：只填打分会用到的字段。"""
    return {
        "canonical_id": nid,
        "title": nid,
        "cited_by_count": cited_by,
        "citation_edge_count": 0,
        "pagerank": pagerank,
        "isolated": False,
    }


def _edge(s: str, t: str) -> dict:
    return {"source": s, "target": t}


def test_rank_pct_handles_ties_and_extremes():
    """分位换算是打分的地基，同分必须同分位，否则同质论文会排出假名次。"""
    pct = graph._rank_pct({"a": 10, "b": 10, "c": 0})
    assert pct["a"] == pct["b"]          # 同分同名次
    assert pct["a"] > pct["c"]
    assert pct["c"] == 0.0               # 最低的那个固定为 0
    assert pct["a"] <= 1.0
    # 单元素不能 ZeroDivision
    assert graph._rank_pct({"only": 5}) == {"only": 1.0}


def test_cornerstone_ranks_highly_cited_and_well_connected_paper_first():
    """基石节点：入度和桥接度都高的论文应当排第一。"""
    nodes = [
        _node("base", cited_by=10, pagerank=0.30),   # 被引最多、又是桥
        _node("mid1", cited_by=2, pagerank=0.10),
        _node("mid2", cited_by=2, pagerank=0.10),
        _node("leaf", cited_by=1, pagerank=0.05),
    ]
    # base 被 mid1/mid2 引用，mid1/mid2 又都引用 leaf —— base 同时具备高被引和桥接
    edges = [_edge("mid1", "base"), _edge("mid2", "base"), _edge("mid1", "leaf"), _edge("mid2", "leaf")]
    scores = graph.cornerstone_scores(nodes, edges)

    ranked = sorted(scores, key=lambda i: -scores[i]["score"])
    assert ranked[0] == "base"
    assert scores["base"]["rank"] == 1
    assert scores["base"]["score"] > scores["leaf"]["score"]


def test_cornerstone_pagerank_beats_raw_citation_count():
    """PageRank 相对被引数的增量价值：被引少但都被"重要论文"引的，分要高于靠边论文堆被引的。

    这是基石的判据核心 —— 只比被引数就退化成 in-degree 排行，PageRank 这一项就白算了。
    """
    nodes = [
        _node("vip", cited_by=3, pagerank=0.40),     # 被引少，但被中枢引用
        _node("bulk", cited_by=3, pagerank=0.02),    # 被引数一样，但都是边缘论文
        _node("hub", cited_by=0, pagerank=0.20),
    ]
    edges = [_edge("hub", "vip"), _edge("hub", "vip"), _edge("hub", "bulk")]
    scores = graph.cornerstone_scores(nodes, edges)
    assert scores["vip"]["pagerank_pct"] > scores["bulk"]["pagerank_pct"]
    assert scores["vip"]["score"] > scores["bulk"]["score"]


def test_isolated_node_gets_zero_score_and_isolated_role():
    """孤立论文不该参与排名，更不该因为"别人分低"而显得像基石。"""
    nodes = [_node("solo", cited_by=0, pagerank=0.0), _node("a", cited_by=3, pagerank=0.2)]
    scores = graph.cornerstone_scores(nodes, [_edge("a", "a")])
    assert scores["solo"]["score"] == 0.0
    assert scores["solo"]["role"] == "isolated"
    assert scores["solo"]["rank"] == 0


def test_high_pagerank_low_indegree_is_foundation_not_fringe():
    """回归：Layer Normalization 这类"通用基础件"曾被误判成 fringe。

    它的 PageRank 全库第一（被 GPT/BERT/LLaMA 反复引用），但直接引用篇数不高、
    也不是桥。判成"边缘"会和前端"金边=PageRank 前 3"自相矛盾，所以单列 foundation。
    """
    nodes = [_node("ln", cited_by=2, pagerank=0.90)]
    for i in range(8):
        nodes.append(_node(f"p{i}", cited_by=10 - i, pagerank=0.01))
    # 关键：连边必须来自一条"链"而不是星形。若 8 个节点都只指向 ln，
    # ln 就成了唯一的桥（betweenness=1），会被判 bridge —— 那是正确行为，
    # 测不到 foundation 分支。这里让高被引的 p0 当唯一中枢，
    # p1..p7 沿链连过去，ln 只挂在链边上而不承担桥接。
    edges = [_edge("p1", "p0"), _edge("p2", "p1"), _edge("p3", "p2"), _edge("p4", "p3"),
             _edge("p5", "ln"), _edge("p6", "p5"), _edge("p7", "p6")]
    scores = graph.cornerstone_scores(nodes, edges)
    assert scores["ln"]["pagerank_pct"] == 1.0
    # ln 入度 1（低于 75% 分位）、桥度也非顶级，唯一突出的是 PageRank
    assert scores["ln"]["cited_pct"] < 0.75
    assert scores["ln"]["bridge_pct"] < 0.75
    assert scores["ln"]["role"] == "foundation"


def test_graph_view_carries_cornerstone_fields_and_board():
    """接口形状：节点要带基石分/角色，顶层要有排好序的 cornerstone 榜。"""
    rows = [
        _row("a.pdf", "Paper A", "2401.00001"),
        _row("b.pdf", "Paper B", "2401.00002"),
    ]
    refs = {
        "b.pdf": [_entry(title="Paper A", arxiv_id="2401.00001")],
        "a.pdf": [],
    }
    view = graph.graph_view(_build(rows, refs))

    node = next(n for n in view["nodes"] if n["title"] == "Paper A")
    assert node["cornerstone"] > 0
    assert node["cornerstone_rank"] == 1
    assert node["role"] in {"cornerstone", "hub", "bridge", "foundation", "fringe"}

    board = view["cornerstones"]
    assert board[0]["title"] == "Paper A"
    assert board[0]["rank"] == 1
    # 榜单顺序必须和 rank 一致，否则前端展示会和节点详情对不上
    assert [b["rank"] for b in board] == sorted(b["rank"] for b in board)


# ---------------- 自动综述（1.5.3） ----------------
def _fake_meta(monkeypatch, catalog, structures=None):
    """把元数据来源换成内存里的假数据，测试不依赖磁盘上的 catalog.json。"""
    monkeypatch.setattr(graph, "_meta_index", lambda: _meta_from(catalog, structures or {}))


def _meta_from(catalog, structures):
    idx: dict[str, dict] = {}
    by_arxiv: dict[str, dict] = {}
    for item in catalog:
        st = structures.get(item["file"]) or {}
        rec = {
            "file": item["file"],
            "title": item.get("title") or st.get("title") or "",
            "year": item.get("year"),
            "category": item.get("category") or "",
            "abstract": (st.get("abstract") or "")[:1200],
        }
        key = graph.norm_title(rec["title"])
        if key:
            idx.setdefault(key, rec)
        aid = graph.norm_arxiv(item.get("arxiv_id") or "")
        if aid:
            by_arxiv.setdefault(aid, rec)
    return {"by_title": idx, "by_arxiv": by_arxiv}


def _sample_view():
    """两篇论文：B 引用了 A。年份和分类都齐全。"""
    rows = [
        _row("a.pdf", "Paper A", "2401.00001"),
        _row("b.pdf", "Paper B", "2401.00002"),
    ]
    refs = {"b.pdf": [_entry(title="Paper A", arxiv_id="2401.00001")], "a.pdf": []}
    return graph.graph_view(_build(rows, refs))


def test_survey_generates_sections_with_real_numbers(monkeypatch):
    """综述必须含概览/聚类/脉络/代表工作四块，且数字来自图本身。"""
    _fake_meta(
        monkeypatch,
        [
            {"file": "a.pdf", "title": "Paper A", "arxiv_id": "2401.00001", "year": "2023", "category": "模型架构"},
            {"file": "b.pdf", "title": "Paper B", "arxiv_id": "2401.00002", "year": "2024", "category": "模型架构"},
        ],
    )
    s = graph.survey(_sample_view())

    assert s["ready"] is True
    assert s["stats"]["papers"] == 2
    headings = [x["heading"] for x in s["sections"]]
    assert "领域概览" in headings and "主题聚类" in headings and "发展脉络" in headings
    # 概览里的被引数字必须和图里的 cited_by 对上，不能是编的
    assert "被库内 1 篇直接引用" in s["overview"]
    # 时间轴按年份升序
    assert [y["year"] for y in s["timeline"]] == [2023, 2024]
    assert s["representatives"][0]["title"] == "Paper A"


def test_survey_reports_missing_years_instead_of_silently_dropping(monkeypatch):
    """缺年份的论文必须被明确说出来：库里 BERT/GPT-1/LLaMA 恰好都缺。

    如果悄悄跳过，读者会以为这些论文不在库里——而它们其实是被引最多的。
    """
    _fake_meta(
        monkeypatch,
        [
            {"file": "a.pdf", "title": "Paper A", "arxiv_id": "2401.00001", "year": None, "category": "模型架构"},
            {"file": "b.pdf", "title": "Paper B", "arxiv_id": "2401.00002", "year": "2024", "category": "模型架构"},
        ],
    )
    s = graph.survey(_sample_view())
    assert "1 篇在库内元数据中缺少发表年份" in s["overview"]
    # 时间轴只统计有年份的，但总数仍是 2
    assert s["stats"]["papers"] == 2
    assert sum(y["count"] for y in s["timeline"]) == 1


def test_survey_focus_filters_by_category(monkeypatch):
    """focus 传主题名时只综述该主题。"""
    _fake_meta(
        monkeypatch,
        [
            {"file": "a.pdf", "title": "Paper A", "arxiv_id": "2401.00001", "year": "2023", "category": "模型架构"},
            {"file": "b.pdf", "title": "Paper B", "arxiv_id": "2401.00002", "year": "2024", "category": "对齐与推理"},
        ],
    )
    s = graph.survey(_sample_view(), focus="对齐与推理")
    assert s["stats"]["papers"] == 1
    assert "模型架构" in s["topics"]  # topics 列的是全库主题，供前端下拉用


def test_survey_empty_graph_is_not_ready():
    assert graph.survey({"nodes": [], "edges": []})["ready"] is False


def test_first_sentence_strips_abstract_label():
    """摘要开头常带 "Abstract" 标签词，抽取首句时要剥掉，否则综述里会出现 "Abstract We introduce…"。"""
    assert graph._first_sentence("Abstract We introduce BERT. It is good.") == "We introduce BERT."
    assert graph._first_sentence("") == ""


# ---------------- 前沿探索（1.5.4） ----------------
def test_frontier_three_kinds_and_every_item_has_basis(monkeypatch):
    """三条线索都要能产出，且每条都必须带可核验的依据，不能只有标题。"""
    _fake_meta(
        monkeypatch,
        [
            {"file": "a.pdf", "title": "Paper A", "arxiv_id": "2401.00001", "year": "2024", "category": "x"},
            {"file": "b.pdf", "title": "Paper B", "arxiv_id": "2401.00002", "year": "2025", "category": "x"},
        ],
    )
    f = graph.frontier(_sample_view())
    assert f["ready"] is True
    kinds = {it["kind"] for it in f["items"]}
    assert "recent_cited" in kinds
    for it in f["items"]:
        assert it["basis"], f"条目 {it['title']} 没有依据"
        assert it["metric"], f"条目 {it['title']} 没有量化指标"


def test_frontier_excludes_cornerstones_from_bridges(monkeypatch):
    """回归：BERT/Attention 这类被引最多的点中介性也高，但它们不是"桥"。

    上一版把 betweenness > 0 全算成桥，这张 36 点的图里出了 35 条，
    真正在缝社区的 Llama 3 / GPT-4 反而被埋掉。现在要排除基石与枢纽角色。
    """
    _fake_meta(monkeypatch, [])
    view = _sample_view()
    # 人为把唯一的被引节点标成 cornerstone，再确认它不会出现在 bridge 里
    for n in view["nodes"]:
        if n["title"] == "Paper A":
            n["cornerstone_rank"] = 1
            n["role"] = "cornerstone"
    f = graph.frontier(view)
    bridge_titles = {it["title"] for it in f["items"] if it["kind"] == "bridge"}
    assert "Paper A" not in bridge_titles


def test_frontier_island_detects_zero_degree_node(monkeypatch):
    """没有任何引用往来的论文要归到"未被接入引用网"，并说明它有两个可能原因。"""
    _fake_meta(monkeypatch, [])
    rows = [_row("solo.pdf", "Solo Paper", "2401.00009")]
    view = graph.graph_view(_build(rows, {}))
    f = graph.frontier(view)
    islands = [it for it in f["items"] if it["kind"] == "island"]
    assert [it["title"] for it in islands] == ["Solo Paper"]
    legend = {item["kind"]: item["desc"] for item in f["legend"]}
    assert "样本覆盖不足" in legend["island"]


def test_frontier_empty_graph_is_not_ready():
    assert graph.frontier({"nodes": []})["ready"] is False


# ---------------- Future Work 原文（1.5.4 明确要求） ----------------
def _no_fw(monkeypatch):
    """把 Future Work 抽取打成永远抽不到，隔离掉磁盘 PDF 的干扰。"""
    monkeypatch.setattr(graph, "future_work_of", lambda cid: {"found": False, "sentences": []})


def test_frontier_has_future_work_legend(monkeypatch):
    """图例里必须有 Future Work 这一条，否则前端不知道这一组是什么。"""
    _fake_meta(monkeypatch, [])
    _no_fw(monkeypatch)
    f = graph.frontier(_sample_view())
    legend = {item["kind"]: item["desc"] for item in f["legend"]}
    assert "future_work" in legend
    assert "Future Work" in legend["future_work"]


def test_frontier_includes_future_work_items_with_quotes(monkeypatch):
    """抽到原文时要作为独立一类出现，且带上句子和来源依据。"""
    _fake_meta(
        monkeypatch,
        [{"file": "a.pdf", "title": "Paper A", "arxiv_id": "2401.00001", "year": "2024", "category": "x"}],
    )
    monkeypatch.setattr(
        graph,
        "future_work_of",
        lambda cid: (
            {
                "found": True,
                "sentences": ["We plan to extend this to long inputs."],
                "source": "a.pdf",
                "page": 3,
                "basis": "来自 a.pdf 第 3 页起的正文原文",
                "reader_url": "reader.html?paper=a.pdf&page=3",
            }
            if cid == "arxiv:2401.00001"
            else {"found": False, "sentences": []}
        ),
    )
    f = graph.frontier(_sample_view())
    fw = [it for it in f["items"] if it["kind"] == "future_work"]
    assert len(fw) == 1
    assert fw[0]["title"] == "Paper A"
    assert fw[0]["sentences"] == ["We plan to extend this to long inputs."]
    assert "a.pdf" in fw[0]["basis"]
    assert f["stats"]["future_work"] == 1


def test_frontier_skips_nodes_without_future_work(monkeypatch):
    """抽不到就跳过，绝不拿摘要冒充原文。"""
    _fake_meta(monkeypatch, [])
    _no_fw(monkeypatch)
    f = graph.frontier(_sample_view())
    assert [it for it in f["items"] if it["kind"] == "future_work"] == []
    assert f["stats"]["future_work"] == 0


def test_future_work_sentence_regex_hits_real_phrasings():
    """这几种是库里真实出现过的说法，正则必须命中，否则一条都抽不出来。"""
    for s in [
        "We plan to extend the Transformer to problems involving other modalities.",
        "We think further research is needed to make layer normalization work well.",
        "We hope that this work motivates future research on the characterization.",
        "This remains an open problem for future study.",
    ]:
        assert graph._FW_SENTENCE_RE.search(s), s


def test_future_work_sentence_regex_ignores_plain_conclusion():
    """普通结论句不该被当成未来工作，否则整节 Conclusion 都会被塞进来。"""
    s = "In this paper we proposed a new architecture and achieved state of the art results."
    assert not graph._FW_SENTENCE_RE.search(s)


def test_fw_paragraphs_dedupes_across_pages(monkeypatch):
    """跨页拼接时同一句会被抽两次（实测 InstructGPT 出现过），必须去重。"""
    same = "We plan to extend the Transformer to other modalities and to investigate local attention."
    monkeypatch.setattr(
        "app.rag.pipeline.page_text",
        lambda source, page: same + "\n" + same,
    )
    sents, basis = graph._fw_paragraphs("a.pdf", [1])
    assert len(sents) == 1
    assert "a.pdf" in basis


def test_fw_paragraphs_returns_empty_when_page_missing(monkeypatch):
    monkeypatch.setattr("app.rag.pipeline.page_text", lambda source, page: "")
    assert graph._fw_paragraphs("a.pdf", [1]) == ([], "")


def test_heading_line_is_split_out_of_sentences():
    """小节标题行必须独立成段，否则会得到「标题 + 正文首句」粘在一起的假句子。

    实测 DeepSeek-R1 抽出来的是：
      "Conclusion, Limitation, and Future Work We present DeepSeek-R1-Zero and ..."
    这是章节标题和正文首句被拼成一句——不是未来工作内容。
    """
    raw = (
        "5. Ethics and Safety Statement\n"
        "We deeply recognize the potential ethical risks of this model.\n"
        "6. Conclusion, Limitation, and Future Work\n"
        "We aim to address this limitation in future updates."
    )
    norm = graph._norm_space(raw)
    assert graph._HEADING_LINE_RE.match("6. Conclusion, Limitation, and Future Work")
    assert "\n\n6. Conclusion, Limitation, and Future Work\n\n" in "\n" + norm + "\n"
    assert "Work We aim" not in norm


def test_fw_title_regex_rejects_scope_phrase():
    """GPT-4 有一节叫 "Scope and Limitations of this Technical Report"，
    那是报告范围声明而非局限章节，早期用 _FW_TITLE_RE.search 会误命中并抽出无关句子。
    """
    assert graph._FW_TITLE_RE.search("Limitations")
    assert graph._FW_TITLE_RE.search("7 Future Work")
    assert graph._FW_TITLE_RE.search("Conclusion, Limitation, and Future Work")
    assert graph._FW_TITLE_RE.search("Limitations and Future Work")
    assert not graph._FW_TITLE_RE.search("Scope and Limitations of this Technical Report")
    assert not graph._FW_TITLE_RE.search("Introduction")


def test_fw_paragraphs_skips_heading_sentences(monkeypatch):
    """标题行即使让进候选，也不能被当成未来工作句子输出。"""
    raw = (
        "Conclusion, Limitation, and Future Work\n"
        "We aim to address this limitation in future updates and will report results later."
    )
    monkeypatch.setattr("app.rag.pipeline.page_text", lambda source, page: raw)
    sents, _ = graph._fw_paragraphs("a.pdf", [1])
    assert sents
    assert not any(graph._HEADING_LINE_RE.match(s) for s in sents)
    assert not any("Work We aim" in s for s in sents)


def test_future_work_regex_hits_technical_report_phrasings():
    """DeepSeek-R1 / Qwen 这类技术报告不写 "future work"，写的是下面这些。"""
    for s in [
        "We aim to address this limitation in future updates.",
        "Future versions will address this by implementing rejection sampling.",
        "In the future, a robust reward model can be obtained to address such issues.",
    ]:
        assert graph._FW_SENTENCE_RE.search(s), s


def test_substance_filter_drops_pure_announcements():
    """这些句子语法上在说 future work，但没告诉读者到底要做什么——实测从库里抽出来的原文。"""
    for s in [
        "In the future, we will focus on advancing robust foundational models.",          # Qwen2.5
        "There are many directions for future works.",                                    # LoRA
        "There are several promising directions for future work on the RWKV architecture.",  # RWKV
        "Our results raise several important questions for future work.",                 # DPO
        "From our experimental results, analyses, and ablations, we have noted several "
        "limitations and areas for future work.",                                          # Whisper
    ]:
        assert not graph._fw_has_substance(s), s


def test_substance_filter_keeps_concrete_plans():
    """有具体对象或动作的必须保留，否则过滤就变成了误杀。"""
    for s in [
        "We plan to extend the Transformer to problems involving other modalities.",
        "We leave further evaluation of analyzing biases in Guanaco and similar chatbots to future work.",
        "In this context, further investigation into model parallelism is warranted.",
        "We aim to address this limitation in future updates.",
        "Further research into improving upon the computational and data efficiency of CLIP will be necessary.",
    ]:
        assert graph._fw_has_substance(s), s