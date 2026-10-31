"""同一来源的块不能霸占全部证据位。

背景（实测）：提问「Transformer 的注意力机制怎么算」时，4 个证据位被同一篇论文
的高分块占满，其他论文一条都进不来，「覆盖论文数」这个展示指标失去意义。

注意这里限的是 **source（文件名）**。论文库里 1706.03762 的 v7/v1/v1_img 是
三个不同文件名、内容近似，pipeline 层拿不到 catalog 的 duplicate_of 关系，
所以按文件名去重拦不住它们——那需要在调用层做，另行处理。
"""

from app.rag.pipeline import _MAX_PER_SOURCE, Chunk, _diversify_by_source


def _item(source: str, cid: int, score: float) -> tuple[Chunk, float, list[str]]:
    return (Chunk(text=f"t{cid}", source=source, page=1, chunk_id=cid), score, ["hash"])


def test_same_source_does_not_monopolize_evidence():
    """同一文件的多块不能占满 4 个证据位，别的文件必须能进来。"""
    items = [
        _item("hot.pdf", 1, 0.9),
        _item("hot.pdf", 2, 0.89),
        _item("hot.pdf", 3, 0.88),
        _item("hot.pdf", 4, 0.87),
        _item("other-a.pdf", 5, 0.5),
        _item("other-b.pdf", 6, 0.4),
    ]
    out = _diversify_by_source(items, top_k=4)
    sources = [c.source for c, _s, _ch in out]

    assert len(out) == 4, f"证据条数不能被多样性拖少，实际 {len(out)}"
    assert sources.count("hot.pdf") <= _MAX_PER_SOURCE, f"hot.pdf 超出上限：{sources}"
    assert "other-a.pdf" in sources and "other-b.pdf" in sources


def test_single_paper_qa_skips_diversification():
    """单篇问答传了 source，所有块来源相同，限流会把证据砍光，必须直接返回。"""
    items = [_item("1706.03762v7.pdf", i, 1.0 - i / 100) for i in range(1, 6)]
    out = _diversify_by_source(items, top_k=4, source="1706.03762v7.pdf")

    assert len(out) == 5, "单篇问答不做来源限流"
    assert all(c.source == "1706.03762v7.pdf" for c, _s, _ch in out)


def test_backfills_when_sources_run_out():
    """只有一篇论文有候选时，回补其余块，不能只剩 2 条。"""
    items = [_item("only.pdf", i, 1.0 - i / 100) for i in range(1, 7)]
    out = _diversify_by_source(items, top_k=4)

    assert len(out) == 4, f"来源不够时必须回补到 top_k，实际 {len(out)}"
    assert all(c.source == "only.pdf" for c, _s, _ch in out)


def test_short_candidate_list_passes_through():
    """候选比 top_k 还少时不需要处理，原样返回。"""
    items = [_item("a.pdf", 1, 0.9), _item("b.pdf", 2, 0.8)]
    assert _diversify_by_source(items, top_k=4) == items


# ---------------------------------------------------------------- 跨文件副本折叠
# 论文库里 1706.03762 存了 4 个文件（v7 / v1 / v1_img / 重传），内容几乎一样、
# 分数几乎一样，纯按分数截断会让它们占满证据位。折叠要在 agent 层做，
# 因为只有那一层拿得到 index.json 的 duplicate_of 关系。


def test_dup_owner_map_covers_arxiv_versions(monkeypatch):
    """v1 / v1_img / 重传副本都要映射到 v7。"""
    from app import agent
    from app.papers import Paper

    papers_ = [
        Paper(paper_id="1706.03762v7", filename="1706.03762v7.pdf", size=1, pages=15, uploaded_at=""),
        Paper(
            paper_id="1706.03762v1",
            filename="1706.03762v1.pdf",
            size=1,
            pages=15,
            uploaded_at="",
            duplicate_of="1706.03762v7",
        ),
        Paper(
            paper_id="1706.03762v1_img",
            filename="1706.03762v1_img.pdf",
            size=1,
            pages=15,
            uploaded_at="",
            duplicate_of="1706.03762v7",
        ),
        Paper(
            paper_id="bdfaa68d8984",
            filename="1706.03762v7.pdf",
            size=1,
            pages=15,
            uploaded_at="",
            duplicate_of="1706.03762v7",
        ),
        Paper(paper_id="other", filename="other.pdf", size=1, pages=5, uploaded_at=""),
    ]
    from app import papers as papers_mod

    monkeypatch.setattr(papers_mod, "list_papers", lambda: papers_)
    owner = agent._dup_owner_map()

    assert owner.get("1706.03762v1.pdf") == "1706.03762v7.pdf"
    assert owner.get("1706.03762v1_img.pdf") == "1706.03762v7.pdf"
    # 历史遗留：索引里这条是 <paper_id>.pdf，也要能折叠
    assert owner.get("bdfaa68d8984.pdf") == "1706.03762v7.pdf"
    assert "1706.03762v7.pdf" not in owner, "主版本不该映射到任何人"
    assert "other.pdf" not in owner


def test_collapse_demotes_copies_below_distinct_papers(monkeypatch):
    """折叠后前 N 条应该是互不相同的论文，副本被排到后面。"""
    from app import agent
    from app.papers import Paper

    papers_ = [
        Paper(paper_id="1706.03762v7", filename="1706.03762v7.pdf", size=1, pages=15, uploaded_at=""),
        Paper(
            paper_id="1706.03762v1",
            filename="1706.03762v1.pdf",
            size=1,
            pages=15,
            uploaded_at="",
            duplicate_of="1706.03762v7",
        ),
        Paper(
            paper_id="1706.03762v1_img",
            filename="1706.03762v1_img.pdf",
            size=1,
            pages=15,
            uploaded_at="",
            duplicate_of="1706.03762v7",
        ),
    ]
    from app import papers as papers_mod

    monkeypatch.setattr(papers_mod, "list_papers", lambda: papers_)

    items = [
        _item("1706.03762v1.pdf", 1, 0.9),
        _item("1706.03762v7.pdf", 2, 0.89),
        _item("1706.03762v1_img.pdf", 3, 0.88),
        _item("other-a.pdf", 4, 0.5),
        _item("other-b.pdf", 5, 0.4),
    ]
    out = agent._collapse_duplicates(items)
    head = [c.source for c, _s, _ch in out][:4]

    assert "other-a.pdf" in head, f"别的论文必须进前排，实际 {head}"
    assert len(out) == 5, "折叠只调顺序，不丢块"
    # 副本仍在，只是排到了后面
    assert out[0][0].source == "1706.03762v1.pdf"
    assert [c.source for c, _s, _ch in out].index("other-b.pdf") < len(out) - 1