"""引用图谱（赛题 1.5）的单元测试。

全部用假数据跑 `build_graph(rows=..., refs_loader=...)`，不碰真实 PDF、不碰索引，
所以跑测试不会影响 RAG 检索，也不依赖本机有没有那 39 篇论文。
"""

from __future__ import annotations

from app import graph


def _row(
    filename: str, title: str, arxiv_id: str = "", abstract: str = "", paper_id: str = ""
) -> dict:
    return {
        "filename": filename,
        "title": title,
        "arxiv_id": arxiv_id,
        "abstract": abstract,
        "paper_id": paper_id,
    }


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


def test_same_filename_twice_is_not_counted_as_two_copies():
    """回归：同一份文件被索引两次（同名两条记录）不得让 copy_count 虚高。

    真实事故：index.json 里 1706.03762v7.pdf 有两条记录 —— 一条 paper_id 是内容哈希
    bdfaa68d8984、一条是文件名式 1706.03762v7，同一份文件被索引了两次。
    旧代码无脑 append，copies 里出现两个 "1706.03762v7.pdf"，copy_count=4。
    """
    rows = [
        _row("1706.03762v7.pdf", "Attention Is All You Need", "1706.03762"),
        _row("1706.03762v7.pdf", "Attention Is All You Need", "1706.03762"),  # 同一文件，第二条记录
        _row("1706.03762v1.pdf", "Attention Is All You Need", "1706.03762"),
    ]
    data = _build(rows, {})

    node = data["nodes"][0]
    assert node["copies"] == ["1706.03762v7.pdf", "1706.03762v1.pdf"]
    assert node["copy_count"] == 2

    s = data["summary"]
    assert s["file_count"] == 3          # 记录数
    assert s["unique_file_count"] == 2   # 唯一文件数
    assert s["node_count"] == 1          # 逻辑论文数
    assert s["duplicate_records"] == 1   # 同名重复记录
    assert s["merged_copies"] == 1       # 真正被并掉的不同文件数
    assert s["merged_away"] == 2         # 记录数 - 节点数


def test_different_titles_under_same_filename_stay_separate():
    """同名但标题不同 = 两份不同文件，不能因为文件名相同就被吃掉。"""
    rows = [
        _row("paper.pdf", "Attention Is All You Need", "1706.03762"),
        _row("paper.pdf", "Layer Normalization", "1607.06450"),
    ]
    data = _build(rows, {})
    assert data["summary"]["node_count"] == 2
    assert sorted(n["copy_count"] for n in data["nodes"]) == [1, 1]


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


def test_arxiv_id_conflict_blocks_fuzzy_match():
    """回归：条目声明了库内没有的 arXiv 号，不许退到标题模糊层连到同家族的别篇论文。

    真实事故：Qwen Technical Report（arXiv 2309.16609，不在库）被标题相似度
    连到了 Qwen2.5 Technical Report（arXiv 2412.15115）。两个编号不同，
    是两篇论文，仅因命名同源才像。
    """
    rows = [
        _row("instr.pdf", "Instruction Pre-Training", "2406.14491"),
        _row("qwen25.pdf", "Qwen2.5 Technical Report", "2412.15115"),
    ]
    refs = {"instr.pdf": [_entry(title="Qwen Technical Report", arxiv_id="2309.16609")]}
    # 阈值故意放得很松：只要还退到标题层，这条边就会生成
    data = _build(rows, refs, fuzzy_accept=0.80, fuzzy_review=0.75)

    assert data["edges"] == []
    # 第三轮 P1：库里没有 Qwen 2309.16609 这篇，且没有同标题候选 → 计入 arxiv_absent
    # （旧实现把它混进 arxiv_conflict，于是"本地未收录"被误读成"ID 明确冲突"）
    assert data["summary"]["refs"]["arxiv_absent"] == 1
    assert data["summary"]["refs"]["arxiv_conflict"] == 0
    assert data["summary"]["refs"]["matched_fuzzy"] == 0
    conflicts = [r for r in data["reviews"] if r["match_method"] == "arxiv_absent"]
    assert len(conflicts) == 1
    assert conflicts[0]["reference_arxiv_id"] == "2309.16609"


def test_arxiv_id_conflict_does_not_block_real_title_matches():
    """冲突拦截只作用于"有 ID 但库里没有"的条目；ID 缺失时标题匹配照常生效。"""
    rows = [
        _row("src.pdf", "Some Paper", "2401.00001"),
        _row("dst.pdf", "Layer Normalization", "1607.06450"),
    ]
    refs = {
        # 没给 ID：允许标题匹配
        "src.pdf": [_entry(title="Layer Normalization")],
    }
    data = _build(rows, refs)
    assert data["summary"]["refs"]["arxiv_conflict"] == 0
    assert len(data["edges"]) == 1
    assert data["edges"][0]["match_method"] == "title_exact"


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
    # 2026-11-12 定位修正：原来的「主题聚类」已改名为「引用关系簇」——
    # 实测簇划分不通过随机基线，不能再叫"主题"。
    assert "领域概览" in headings and "引用关系簇" in headings and "发展脉络" in headings
    assert "主题聚类" not in headings, "用户可见章节名不得再宣称是主题/研究子领域"
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


# ---------------- 社区划分的边权与稳定性（负责人专项改造 P0-2） ----------------
def _triangle_graph():
    """两个三角 + 一条弱连接：无权重时是两个清晰社区。"""
    rows = [
        _row("a.pdf", "Paper A", "2401.00001"),
        _row("b.pdf", "Paper B", "2401.00002"),
        _row("c.pdf", "Paper C", "2401.00003"),
        _row("d.pdf", "Paper D", "2401.00004"),
        _row("e.pdf", "Paper E", "2401.00005"),
        _row("f.pdf", "Paper F", "2401.00006"),
    ]
    refs = {
        "a.pdf": [_entry(title="Paper B", arxiv_id="2401.00002")],
        "b.pdf": [_entry(title="Paper C", arxiv_id="2401.00003")],
        "c.pdf": [_entry(title="Paper A", arxiv_id="2401.00001")],
        "d.pdf": [_entry(title="Paper E", arxiv_id="2401.00005")],
        "e.pdf": [_entry(title="Paper F", arxiv_id="2401.00006")],
        "f.pdf": [_entry(title="Paper D", arxiv_id="2401.00004")],
    }
    return _build(rows, refs)


def _two_cliques_bridge():
    """两个四点团 + 一条桥边（X→Y）。桥的权重决定两团是一个社区还是两个。"""
    rows = [
        _row("a.pdf", "Paper A", "2401.00001"),
        _row("b.pdf", "Paper B", "2401.00002"),
        _row("c.pdf", "Paper C", "2401.00003"),
        _row("x.pdf", "Paper X", "2401.00004"),
        _row("d.pdf", "Paper D", "2401.00005"),
        _row("e.pdf", "Paper E", "2401.00006"),
        _row("f.pdf", "Paper F", "2401.00007"),
        _row("y.pdf", "Paper Y", "2401.00008"),
    ]
    refs = {
        "a.pdf": [_entry(title="Paper B", arxiv_id="2401.00002")],
        "b.pdf": [_entry(title="Paper C", arxiv_id="2401.00003")],
        "c.pdf": [_entry(title="Paper X", arxiv_id="2401.00004")],
        "x.pdf": [
            _entry(title="Paper A", arxiv_id="2401.00001"),
            # 桥：X 引用 Y。它连通两个团，权重就是这条边的属性
            _entry(title="Paper Y", arxiv_id="2401.00008"),
        ],
        "d.pdf": [_entry(title="Paper E", arxiv_id="2401.00006")],
        "e.pdf": [_entry(title="Paper F", arxiv_id="2401.00007")],
        "f.pdf": [_entry(title="Paper D", arxiv_id="2401.00005")],
        "y.pdf": [_entry(title="Paper D", arxiv_id="2401.00005")],
    }
    return _build(rows, refs)


def test_communities_default_weights_are_all_one():
    """旧调用方不传 weight_of：等价于所有边权重 1，结果与历史一致。"""
    data = _two_cliques_bridge()
    a = graph.communities(data["nodes"], data["edges"])
    b = graph.communities(data["nodes"], data["edges"], weight_of=lambda _e: 1.0)
    assert a == b


def test_edge_weight_really_changes_the_partition():
    """边权必须真的进算法：桥边权重拉大能合并社区，压小能拆分社区。

    这是回归测试——上一版 communities() 把权重硬编码成 1.0，
    前端有的是 match_score、算法内部却按无权图算。
    """
    data = _two_cliques_bridge()
    nodes, edges = data["nodes"], data["edges"]

    def by_bridge(scale: float):
        return lambda e: scale if {e["source"], e["target"]} == {"arxiv:2401.00004", "arxiv:2401.00008"} else 1.0

    heavy = graph.communities(nodes, edges, weight_of=by_bridge(50.0))
    light = graph.communities(nodes, edges, weight_of=by_bridge(0.01))
    # 只断言「两种权重给出的分区不同」——这足以证明权重进了增益公式。
    # 不断言「桥权重大就合并成更少的社区」：Louvain 是启发式，两阶段的
    # 局部最优方向不由单条边的权重单调决定，实测本图桥权重 50 反而分出 3 个社区。
    assert heavy != light, "桥边权重从 50 改成 0.01 分区毫无变化，说明边权没进算法"


def test_edge_weight_by_match_grades_by_evidence_strength():
    """三级权重必须按证据强度递减，且未知匹配方式不得拿到最高权重。"""
    assert graph.edge_weight_by_match({"match_method": "arxiv"}) == 1.0
    assert graph.edge_weight_by_match({"match_method": "title_exact"}) == 0.8
    assert graph.edge_weight_by_match({"match_method": "title_fuzzy"}) == 0.5
    assert graph.edge_weight_by_match({}) == 0.5
    assert (
        graph.edge_weight_by_match({"match_method": "arxiv"})
        > graph.edge_weight_by_match({"match_method": "title_exact"})
        > graph.edge_weight_by_match({"match_method": "title_fuzzy"})
    )


def test_communities_isolated_node_does_not_crash():
    """孤立节点（没有任何边）不能把划分搞崩，也不能被算进别人的社区。"""
    rows = [_row("solo.pdf", "Solo Paper", "2401.00009"), _row("a.pdf", "Paper A", "2401.00001")]
    data = _build(rows, {})
    comm = graph.communities(data["nodes"], data["edges"])
    assert set(comm) == {n["canonical_id"] for n in data["nodes"]}


def test_communities_are_deterministic():
    """同样的数据跑 5 次，社区归属完全一致（不随机打乱节点顺序）。"""
    data = _triangle_graph()
    runs = [graph.communities(data["nodes"], data["edges"]) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_community_sizes_match_node_communities():
    """graph_view 报的社区规模，必须与节点上的 community 字段逐一对得上。"""
    data = _triangle_graph()
    view = graph.graph_view(data)
    sizes: dict[int, int] = {}
    for n in view["nodes"]:
        sizes[n["community"]] = sizes.get(n["community"], 0) + 1
    reported = {c["id"]: c["size"] for c in view["summary"]["communities"]}
    assert reported == sizes
    assert view["summary"]["community_count"] == len(sizes)


# ---------------- 第三轮 P1/P2：arXiv 冲突判据与 exact-title 豁免 ----------------
def test_exempt_allowed_when_candidate_has_no_arxiv_id(tmp_path, monkeypatch):
    """场景 1：库外 ID + 精确标题 + 候选无 ID → 允许严格豁免（保持原有行为）。"""
    rows = [
        _row("bert.pdf", "BERT: Pre-training of Deep Bidirectional Transformers", ""),
    ]
    refs = {"bert.pdf": []}
    rows.append(_row("src.pdf", "Source Paper", "2401.00001"))
    refs["src.pdf"] = [
        _entry(
            title="BERT: Pre-training of Deep Bidirectional Transformers",
            arxiv_id="1810.04805",  # 库内节点自己没录编号，这个号"库里没有"只是没入库
        )
    ]
    data = _build(rows, refs)

    assert len(data["edges"]) == 1
    assert data["edges"][0]["match_method"] == "title_exact"
    assert data["summary"]["refs"]["arxiv_conflict_exempt"] == 1
    assert data["summary"]["refs"]["arxiv_conflict"] == 0
    assert data["summary"]["refs"]["arxiv_absent"] == 0


def test_exempt_blocked_when_candidate_has_another_arxiv_id():
    """场景 2：库外 ID + 精确标题 + 候选有另一个明确 ID → 禁止连边。

    这是第二轮终审点出的风险：旧 `_title_exact_target()` 不看候选的 arxiv_id，
    真实数据里 19 次豁免恰好全落在无编号节点上（运气），一旦补录编号就会复活错边。
    """
    rows = [
        _row("qwen25.pdf", "Qwen2.5 Technical Report", "2412.15115"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    refs = {
        "src.pdf": [
            _entry(title="Qwen2.5 Technical Report", arxiv_id="2309.16609"),
        ]
    }
    data = _build(rows, refs)

    assert data["edges"] == []
    refs_stats = data["summary"]["refs"]
    assert refs_stats["arxiv_conflict_exempt"] == 0
    # 有同标题候选，但候选是另一个明确 ID → 真冲突
    assert refs_stats["arxiv_conflict"] == 1
    assert refs_stats["arxiv_absent"] == 0
    row = [r for r in data["reviews"] if r["match_method"] == "arxiv_conflict"]
    assert len(row) == 1
    assert "身份冲突" in row[0]["reason"]


def test_exempt_blocked_when_title_only_partially_similar():
    """场景 3：库外 ID + 部分相似标题 → 禁止自动连边。"""
    rows = [
        _row("qwen25.pdf", "Qwen2.5 Technical Report", "2412.15115"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    refs = {"src.pdf": [_entry(title="Qwen Technical Report", arxiv_id="2309.16609")]}
    data = _build(rows, refs)

    assert data["edges"] == []
    assert data["summary"]["refs"]["arxiv_absent"] == 1


def test_ambiguous_same_titles_do_not_silently_pick_last(monkeypatch):
    """场景 4：引用标题键上出现多个候选 → 禁止静默取字典最后一项。

    旧实现 `_title_exact_target` 里 `cand = by_title.get(key)` 一次取定，
    同键多候选时结果取决于遍历顺序，连到谁全看运气。
    `build_nodes()` 会按标题把同名论文并成一个节点，所以真实数据里这个键
    基本只有一项；这里直接注入两个同键节点，专门守 `ambiguous` 分支——
    否则那段代码永远跑不到，也就无从回归。
    """
    shared = "Shared Title Paper"
    target = graph.LogicalNode(
        canonical_id="arxiv:2401.00011", title=shared, arxiv_id="2401.00011", copies=["a.pdf"]
    )
    other = graph.LogicalNode(
        canonical_id="arxiv:2401.00012", title=shared, arxiv_id="2401.00012", copies=["b.pdf"]
    )
    src = graph.LogicalNode(
        canonical_id="arxiv:2401.00001", title="Source Paper", arxiv_id="2401.00001", copies=["src.pdf"]
    )

    monkeypatch.setattr(graph, "build_nodes", lambda rows: ([src, target, other], []))
    rows = [
        _row("a.pdf", shared, "2401.00011"),
        _row("b.pdf", shared, "2401.00012"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    refs = {"src.pdf": [_entry(title=shared, arxiv_id="2401.99999")]}
    data = _build(rows, refs)

    assert data["edges"] == []
    refs_stats = data["summary"]["refs"]
    assert refs_stats["arxiv_conflict_exempt"] == 0
    assert refs_stats["arxiv_absent"] == 1
    assert refs_stats["conflict_reasons"]["ambiguous"] == 1


def test_exact_arxiv_id_still_matches_normally():
    """场景 5：引用 ID 与库内一致 → 正常走 arXiv 精确匹配，不受本轮改动影响。"""
    rows = [
        _row("gpt3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    refs = {
        "src.pdf": [
            _entry(title="Language Models are Few-Shot Learners", arxiv_id="2005.14165")
        ]
    }
    data = _build(rows, refs)

    assert len(data["edges"]) == 1
    assert data["edges"][0]["match_method"] == "arxiv"
    assert data["edges"][0]["match_score"] == 1.0
    assert data["summary"]["refs"]["matched_arxiv"] == 1


def test_arxiv_absent_never_becomes_conflict():
    """场景 6：库里完全没有这篇（标题也对不上）→ 只能记 arxiv_absent。"""
    rows = [
        _row("bert.pdf", "BERT", "1810.04805"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    refs = {"src.pdf": [_entry(title="Some Totally Different Work", arxiv_id="1409.0473")]}
    data = _build(rows, refs)

    assert data["edges"] == []
    assert data["summary"]["refs"]["arxiv_absent"] == 1
    assert data["summary"]["refs"]["arxiv_conflict"] == 0
    assert data["summary"]["refs"]["conflict_reasons"]["no_candidate"] == 1


def test_reference_buckets_conserve_total():
    """第三轮 P3：互斥桶之和必须等于 entries_total。

    第二轮终审发现：`stats["review"] = len(reviews)` 与 arxiv_conflict 跨字段重复计入，
    各类之和比 entries_total 多出整整 1242；同时 title_fuzzy 队列条目两边都不记，
    又少了 24。这里用一批混合条目把守恒钉住。
    """
    rows = [
        _row("a.pdf", "Alpha Paper", "2401.00001"),
        _row("b.pdf", "Beta Paper", "2401.00002"),
        _row("c.pdf", "Gamma Paper No Id", ""),
        _row("src.pdf", "Source Paper", "2401.00009"),
    ]
    refs = {
        "src.pdf": [
            _entry(title="Alpha Paper", arxiv_id="2401.00001"),          # matched_arxiv
            _entry(title="Beta Paper"),                                   # matched_exact
            _entry(title="Gamma Paper No Id", arxiv_id="2401.88888"),     # 豁免 → matched_exact
            _entry(title="Nothing Like It At All", arxiv_id="1409.0473"), # arxiv_absent
            _entry(title="Source Paper"),                                 # self_reference
            _entry(title=""),                                             # 空标题 → unmatched
        ]
    }
    data = _build(rows, refs)
    stats = data["summary"]["refs"]

    buckets = (
        "matched_arxiv", "matched_exact", "matched_fuzzy",
        "unmatched", "arxiv_conflict", "arxiv_absent", "self_reference",
    )
    assert sum(stats[b] for b in buckets) == stats["entries_total"] == 6
    assert stats["self_reference"] == 1
    # review 只是队列视图，等于两个子项之和，且不参与上面的守恒
    assert stats["review"] == stats["review_arxiv_conflict"] + stats["review_title_fuzzy"]
    assert stats["review"] == len(data["reviews"])


def test_review_queue_item_still_counted_in_a_bucket():
    """第三轮 P3 回归：进待核验队列的条目也必须落进一个互斥桶。

    旧代码在 title_fuzzy 队列分支直接 continue，既不算 matched 也不算 unmatched，
    于是守恒差正好是队列条数。
    """
    rows = [
        _row("gpt3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
        _row("src.pdf", "Source Paper", "2401.00001"),
    ]
    # 相似度 0.8916：落在 0.75~0.92 之间，进队列但不连边
    refs = {"src.pdf": [_entry(title="Language Models are Few-Shot Learners Preprint")]}
    data = _build(rows, refs)

    stats = data["summary"]["refs"]
    assert stats["review_title_fuzzy"] >= 1
    assert stats["matched_fuzzy"] == 0
    assert stats["unmatched"] >= 1
    buckets = sum(
        stats[b]
        for b in (
            "matched_arxiv", "matched_exact", "matched_fuzzy",
            "unmatched", "arxiv_conflict", "arxiv_absent", "self_reference",
        )
    )
    assert buckets == stats["entries_total"]


# ---------------- 第三轮 P0：副本文件定位 ----------------
def test_missing_copy_file_is_counted_not_silently_dropped(tmp_path, monkeypatch):
    """P0 回归：文件定位不到时必须记账 + 告警，不能静默当成"零参考文献"。"""
    monkeypatch.setattr(graph, "PAPERS_DIR", tmp_path)

    rows = [
        _row("ghost.pdf", "Ghost Paper", "2401.00001", paper_id="deadbeefcafe"),
        _row("present.pdf", "Present Paper", "2401.00002", paper_id="present"),
    ]
    (tmp_path / "present.pdf").write_bytes(b"%PDF-1.4 fake")

    data = graph.build_graph(rows=rows)  # 不注入 refs_loader，走真实的定位路径
    summary = data["summary"]

    assert summary["missing_copy_count"] == 1
    assert summary["missing_copy_files"][0]["filename"] == "ghost.pdf"
    assert summary["missing_copy_files"][0]["paper_id"] == "deadbeefcafe"
    assert summary["missing_copy_files"][0]["node"] == "arxiv:2401.00001"


def test_hashed_filename_is_resolved_by_paper_id_hash():
    """P0 核心：索引记原名、磁盘只有 `<paper_id>.pdf` 时，靠内容哈希认出来。"""
    rows = [_row("1610.08613.pdf", "Can Active Memory Replace Attention?", "1610.08613",
                 paper_id="57567c72c96f")]
    resolved = graph._resolve_copy_path("1610.08613.pdf", "57567c72c96f")
    assert resolved is not None
    assert resolved.name == "57567c72c96f.pdf"
    # 哈希必须真的与 paper_id 对得上，而不是"名字碰巧存在"
    assert graph._sha12_of(resolved) == "57567c72c96f"
    assert rows  # 保留断言上下文


def test_paper_id_fallback_rejects_hash_mismatch(tmp_path, monkeypatch):
    """P0 安全边界：回退候选存在但内容哈希对不上 → 拒绝使用，记缺失。"""
    monkeypatch.setattr(graph, "PAPERS_DIR", tmp_path)
    (tmp_path / "deadbeefcafe.pdf").write_bytes(b"not the same content")

    assert graph._resolve_copy_path("ghost.pdf", "deadbeefcafe") is None


def test_paper_id_fallback_needs_no_similar_filename(tmp_path, monkeypatch):
    """P0 边界：没有 paper_id 时不做任何猜测，直接判缺失。"""
    monkeypatch.setattr(graph, "PAPERS_DIR", tmp_path)
    (tmp_path / "looks_similar.pdf").write_bytes(b"%PDF-1.4")

    assert graph._resolve_copy_path("missing.pdf", "") is None


def test_multiple_copies_do_not_multiply_edges():
    """P0 要求 7：一个节点有多个副本时，参考文献合并去重，边不能翻倍。"""
    rows = [
        _row("v1.pdf", "Attention Is All You Need", "1706.03762", paper_id="v1"),
        _row("v7.pdf", "Attention Is All You Need", "1706.03762", paper_id="v7"),
        _row("gpt3.pdf", "Language Models are Few-Shot Learners", "2005.14165"),
    ]
    same = [_entry(title="Language Models are Few-Shot Learners", arxiv_id="2005.14165")]
    refs = {"v1.pdf": same, "v7.pdf": list(same)}
    data = _build(rows, refs)

    assert data["summary"]["edge_count"] == 1
    assert data["summary"]["cited_paper_count"] == 1


def test_build_graph_keeps_paper_id_on_rows():
    """P0：rows 带上 paper_id 后，节点内部记录必须与 copies 一一对应。"""
    rows = [
        _row("x.pdf", "X Paper", "2401.00001", paper_id="aaa111"),
        _row("x_v2.pdf", "X Paper", "2401.00001", paper_id="bbb222"),
    ]
    nodes, _ = graph.build_nodes(rows)
    assert len(nodes) == 1
    assert nodes[0].copies == ["x.pdf", "x_v2.pdf"] or nodes[0].copies == ["x_v2.pdf", "x.pdf"]
    assert len(nodes[0]._copy_paper_ids) == len(nodes[0].copies) == 2


# ---------------- 副本内参考文献去重（方案 A） ----------------
# 背景：build_graph 原来在每个副本开头用已有 merged 重建 seen，但 append 之后从不
# 更新它，等于副本内部完全不去重（单副本时 seen 永远是空集）。更要紧的是空标题
# 条目会退化成 ("", "") 这个假键——Mamba 的 35 条空标题其实是附录正文残片，按假键
# 合并会丢掉 34 段互不相同的内容。下面几条用例都断言**最终合并结果**，而不是
# 去看有没有调用 seen.add()。


def _merged_refs(copies, refs) -> list:
    """绕开完整建图，直接验合并结果：copies 里每个文件名去 refs 取条目再合并。"""
    return graph._merge_node_references(copies, lambda name: refs.get(name, []))


def test_same_title_same_arxiv_inside_one_copy_is_deduped():
    """1. 单副本内相同有效标题 + 相同 arXiv 编号 → 只留一条。"""
    refs = [
        _entry(title="Layer Normalization", arxiv_id="1607.06450"),
        _entry(title="Layer Normalization", arxiv_id="1607.06450"),
        _entry(title="Gaussian Error Linear Units", arxiv_id="1606.08415"),
    ]
    out = _merged_refs(["a.pdf"], {"a.pdf": refs})

    assert [e.title for e in out] == ["Layer Normalization", "Gaussian Error Linear Units"]


def test_same_title_different_arxiv_inside_one_copy_is_kept():
    """2. 单副本内相同标题、不同 arXiv 编号 → 两条都留，不同论文不能被合并。"""
    refs = [
        _entry(title="Scaling Laws", arxiv_id="2001.08361"),
        _entry(title="Scaling Laws", arxiv_id="2203.15556"),
    ]
    out = _merged_refs(["a.pdf"], {"a.pdf": refs})

    assert len(out) == 2
    assert [e.arxiv_id for e in out] == ["2001.08361", "2203.15556"]


def test_empty_titles_inside_one_copy_are_all_kept():
    """3. 单副本内多条空标题 → 必须全部保留（它们是不同正文，不是同一篇的重复）。"""
    refs = [
        _entry(title="", arxiv_id="", raw="Example: GLU Activation. To illustrate..."),
        _entry(title="", arxiv_id="", raw="Mechanics of Selective SSMs Proof of Theorem 1"),
        _entry(title="", arxiv_id="", raw="Table 14: YouTubeMix length scaling"),
    ]
    out = _merged_refs(["mamba.pdf"], {"mamba.pdf": refs})

    assert len(out) == 3
    assert [e.raw for e in out] == [r.raw for r in refs]


def test_duplicate_across_two_copies_is_deduped():
    """4. 两个副本之间相同有效条目 → 只留一条（原有跨副本去重能力不能丢）。"""
    same = _entry(title="Layer Normalization", arxiv_id="1607.06450")
    out = _merged_refs(["v1.pdf", "v7.pdf"], {"v1.pdf": [same], "v7.pdf": [_entry(title="Layer Normalization", arxiv_id="1607.06450")]})

    assert len(out) == 1
    assert out[0].title == "Layer Normalization"


def test_multiple_copies_with_inner_and_cross_duplicates():
    """5. 多副本各自存在内部重复 + 跨副本重复 → 去重后条数符合预期。"""
    a = [
        _entry(title="Layer Normalization", arxiv_id="1607.06450", raw="a-1"),
        _entry(title="Layer Normalization", arxiv_id="1607.06450", raw="a-2"),
        _entry(title="Attention Is All You Need", arxiv_id="1706.03762", raw="a-3"),
    ]
    b = [
        _entry(title="Attention Is All You Need", arxiv_id="1706.03762", raw="b-1"),
        _entry(title="Gaussian Error Linear Units", arxiv_id="1606.08415", raw="b-2"),
        _entry(title="Gaussian Error Linear Units", arxiv_id="1606.08415", raw="b-3"),
    ]
    out = _merged_refs(["a.pdf", "b.pdf"], {"a.pdf": a, "b.pdf": b})

    assert len(out) == 3
    assert [e.raw for e in out] == ["a-1", "a-3", "b-2"]


def test_publisher_fragment_with_empty_title_survives_next_to_real_entries():
    """6. Active Memory 的空标题出版信息残片不能被误删。

    实测该篇 30 条里恰好 1 条空标题：'In CVPR, 2016.'（真文献本身也是 30 条去重后 30 条）。
    """
    refs = [
        _entry(title="ImageNet Classification with Deep Convolutional Neural Networks", arxiv_id=""),
        _entry(title="", arxiv_id="", raw="In CVPR, 2016."),
        _entry(title="Long Short-Term Memory", arxiv_id=""),
    ]
    out = _merged_refs(["active-memory.pdf"], {"active-memory.pdf": refs})

    assert len(out) == 3
    assert any(e.raw == "In CVPR, 2016." for e in out)


def test_real_duplicate_publisher_line_is_deduped():
    """Mamba 里唯一一组真重复：同一条 NeurIPS 出版信息被抽到两次（#36 与 #68）。"""
    line = "In: Advances in Neural Information Processing Systems (NeurIPS)"
    refs = [
        _entry(title=line, arxiv_id="", raw="#36"),
        _entry(title=line, arxiv_id="", raw="#68"),
    ]
    out = _merged_refs(["mamba.pdf"], {"mamba.pdf": refs})

    assert len(out) == 1
    assert out[0].raw == "#36"


def test_ref_dedup_key_returns_none_for_blank_title():
    """去重键本身：空标题不产生键，调用方据此跳过去重。"""
    assert graph._ref_dedup_key(_entry(title="", arxiv_id="")) is None
    assert graph._ref_dedup_key(_entry(title="   ", arxiv_id="1706.03762")) is None
    assert graph._ref_dedup_key(_entry(title="Layer Normalization", arxiv_id="")) == (
        "Layer Normalization",
        "",
    )


def test_inner_copy_duplicates_do_not_fake_a_second_edge():
    """副本内重复条目去重后，不能因为多抽了一次就多出一条边。"""
    rows = [
        _row("src.pdf", "Source Paper", "2401.00001"),
        _row("dst.pdf", "Layer Normalization", "1607.06450"),
    ]
    refs = {
        "src.pdf": [
            _entry(title="Layer Normalization", arxiv_id="1607.06450"),
            _entry(title="Layer Normalization", arxiv_id="1607.06450"),
        ]
    }
    data = _build(rows, refs)

    assert data["summary"]["edge_count"] == 1
    assert data["summary"]["refs"]["entries_total"] == 1


# ---------------- 参考文献区结束行识别（附录正文不得算成文献条目） ----------------
# 为什么需要这一组：原来的边界识别只认 "appendix" / 致谢三种写法，而大量论文的附录标题是
# "A" + "Discussion: Selection Mechanism"（Mamba）或 "A. Experimental Settings"，
# 结果参考文献区一路读进附录正文 —— Mamba 实测多抽 57 条正文残片。


def _line(text, size=10.0, bold=False, page=1, x=72.0, y=100.0):
    """造一条版面行，字段与 app.structure._lines 一致。"""
    return {"text": text, "size": size, "bold": bold, "page": page, "x": x, "y": y}


def _lines_with_references(body_after):
    """构造 "正文若干行 + References 标题 + 若干行"，返回 (lines, references 行号)。"""
    head = [_line("This is the body text of the paper.", size=10.0) for _ in range(5)]
    idx = len(head)
    head.append(_line("References", size=10.0))
    return head + body_after, idx


def test_heading_detector_accepts_chapter_titles():
    """编号 + 标题的各种正常写法都要认成标题。"""
    for text in (
        "A. Experimental Settings",
        "A. Linear-probe evaluation",
        "F. Hyperparameters",
        "APPENDIX",
        "Appendix",
        "Supplementary Material",
        "A LARGE LANGUAGE MODELS STILL NEED PARAMETER UPDATES",
    ):
        assert graph._looks_like_heading(text) is True, text


def test_heading_detector_rejects_author_lines():
    """以作者缩写开头的参考文献条目不能被当成标题（曾把 DeepSeek 的文献行当附录起点）。"""
    for text in (
        "P. Minervini. Are we done with mmlu? CoRR, abs/2110.08173, 2021",
        "H. Face. Open r1: A fully open reproduction of deepseek-r1",
        "Z. Gou, Z. Shao, Y. Gong, Y. Yang, N. Duan, and W. Chen.",
        "Hendrycks, D. and Gimpel, K. Gaussian error linear units",
        "[57.5] ERNIE-ViLG: Unified generative pretraining for text-to-image",
    ):
        assert graph._looks_like_heading(text) is False, text


def test_stop_index_finds_split_title_heading():
    """Mamba 式排法：编号独占一行，标题在下一行。"""
    after = [
        _line("[1] Author. Title. 2020."),
        _line("A", size=14.3, bold=True, page=3),
        _line("Discussion: Selection Mechanism", size=14.3, bold=True, page=3),
        _line("We explain why the selection mechanism works.", size=10.0, page=3),
    ]
    lines, ref_idx = _lines_with_references(after)
    stop = graph._reference_stop_index(lines, ref_idx)
    assert stop is not None
    assert graph._norm(lines[stop]["text"]) == "A"


def test_stop_index_finds_inline_title_heading():
    """编号与标题同行："A. Experimental Settings"。"""
    after = [
        _line("[1] Author. Title. 2020."),
        _line("A. Experimental Settings", size=12.0, bold=True, page=2),
        _line("Details of the experiments.", size=10.0, page=2),
    ]
    lines, ref_idx = _lines_with_references(after)
    stop = graph._reference_stop_index(lines, ref_idx)
    assert stop is not None
    assert graph._norm(lines[stop]["text"]) == "A. Experimental Settings"


def test_header_of_bold_small_text_is_not_a_stop():
    """页眉（加粗但比正文小）不能当附录起点 —— CLIP / Whisper / Codex 都这么排。"""
    after = [
        _line("Learning Transferable Visual Models", size=9.0, bold=True, page=2),
        _line("[1] Author. Title. 2020."),
        _line("Learning Transferable Visual Models", size=9.0, bold=True, page=3),
        _line("[2] Another. Title. 2021."),
    ]
    lines, ref_idx = _lines_with_references(after)
    assert graph._reference_stop_index(lines, ref_idx) is None


def test_no_stop_when_references_run_to_the_end():
    """没有附录的论文（Switch Transformer / Mistral 7B）不能凭空截断。"""
    after = [_line(f"[{i}] Author. Some Title. 20{i:02d}.") for i in range(1, 8)]
    lines, ref_idx = _lines_with_references(after)
    assert graph._reference_stop_index(lines, ref_idx) is None


def test_acknowledgments_still_end_the_section():
    """原有的致谢判据必须保留（字号不一定更大）。"""
    after = [
        _line("[1] Author. Title. 2020."),
        _line("Acknowledgments", size=10.0),
        _line("We thank the reviewers.", size=10.0),
    ]
    lines, ref_idx = _lines_with_references(after)
    stop = graph._reference_stop_index(lines, ref_idx)
    assert stop is not None
    assert graph._norm(lines[stop]["text"]) == "Acknowledgments"


def test_merge_column_splits_respects_stop():
    """stop 之后的行一律不进入参考文献处理。"""
    after = [
        _line("[1] Author. Title. 2020.", page=2),
        _line("A", size=14.3, bold=True, page=3),
        _line("Discussion", size=14.3, bold=True, page=3),
        _line("This paragraph is appendix body text.", size=10.0, page=3),
    ]
    lines, ref_idx = _lines_with_references(after)
    stop = graph._reference_stop_index(lines, ref_idx)
    texts = graph._merge_column_splits(lines, ref_idx, stop)
    joined = " ".join(texts)
    assert "appendix body text" not in joined
    assert "Author. Title" in joined


# ---------- 模块度与随机基线（P0-1：顶部结论必须能被代码复算）----------


def _two_blobs() -> tuple[list[dict], list[dict]]:
    """两个互不相连的团：模块度应该接近 0.5，远高于同规模随机图。

    节点必须用 `canonical_id`（`communities()` 与 `graph_view()` 都用这个键），
    用 `id` 会直接 KeyError。
    """
    nodes = [{"canonical_id": f"n{i}", "id": f"n{i}", "title": f"n{i}"} for i in range(6)]
    edges = [
        {"source": "n0", "target": "n1"},
        {"source": "n1", "target": "n2"},
        {"source": "n2", "target": "n0"},
        {"source": "n3", "target": "n4"},
        {"source": "n4", "target": "n5"},
        {"source": "n5", "target": "n3"},
    ]
    return nodes, edges


def test_modularity_of_two_disjoint_triangles():
    """两个不相连的三角形，Q 应该明显大于 0（分层清楚）。"""
    nodes, edges = _two_blobs()
    comm = {"n0": 0, "n1": 0, "n2": 0, "n3": 1, "n4": 1, "n5": 1}
    q = graph.modularity(nodes, edges, comm)
    assert q > 0.4, q


def test_random_baseline_direction_and_fields():
    """实际 Q 明显好于随机时，beaten 必须接近满分、p_value 必须很小。

    这个用例专门守住一个真 bug：`beaten` 曾用 `q >= real` 计数、p 又写成
    `beaten / rounds`，方向全反——实际 Q 压过 198/200 会被报成 p≈0.99「比随机还差」。
    """
    nodes, edges = _two_blobs()
    comm = {"n0": 0, "n1": 0, "n2": 0, "n3": 1, "n4": 1, "n5": 1}
    real = graph.modularity(nodes, edges, comm)
    res = graph.random_q_baseline(nodes, edges, comm, rounds=50, seed=20261112)

    assert res["real"] == real
    assert res["node_count"] == 6
    # 方向：实际明显好于随机 -> 压过绝大多数随机图，p 很小
    assert res["beaten"] >= 45, res
    assert res["p_value"] <= 0.1, res
    assert res["significant"] is True, res
    # p 值与 beaten 必须自洽：p = (rounds - beaten) / rounds
    assert abs(res["p_value"] - (res["rounds"] - res["beaten"]) / res["rounds"]) < 1e-9


def test_random_baseline_is_reproducible_with_seed():
    """同一个 seed 必须给出完全相同的基线，否则页面上的结论没法复现。"""
    nodes, edges = _two_blobs()
    comm = {"n0": 0, "n1": 0, "n2": 0, "n3": 1, "n4": 1, "n5": 1}
    a = graph.random_q_baseline(nodes, edges, comm, rounds=30, seed=7)
    b = graph.random_q_baseline(nodes, edges, comm, rounds=30, seed=7)
    assert a == b
    assert a["seed"] == 7 and a["rounds"] == 30


