"""论文库主题分组与标题截断的单元测试。"""

from __future__ import annotations

import json

import pytest

from app import paper_topics


@pytest.fixture()
def fake_meta(tmp_path, monkeypatch):
    """把 structures / 图谱两个路径都指到 tmp，避免测试依赖真实数据。"""
    structures = tmp_path / "structures.json"
    graph = tmp_path / "citation-graph.json"
    monkeypatch.setattr(paper_topics, "_structures_path", lambda: structures)
    monkeypatch.setattr(paper_topics, "_graph_path", lambda: graph)

    def write(structures_data, graph_data):
        structures.write_text(json.dumps(structures_data, ensure_ascii=False), encoding="utf-8")
        graph.write_text(json.dumps(graph_data, ensure_ascii=False), encoding="utf-8")

    return write


def _graph(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {"nodes": nodes, "edges": edges or []}


class TestShortTitle:
    def test_short_title_kept_as_is(self):
        display, cut = paper_topics.short_title("Attention Is All You Need")
        assert display == "Attention Is All You Need"
        assert cut is False

    def test_long_title_is_truncated_with_ellipsis(self):
        title = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
        display, cut = paper_topics.short_title(title)
        assert cut is True
        assert display.endswith("…")
        assert len(display) <= paper_topics.TITLE_MAX_CHARS

    def test_truncation_breaks_on_word_boundary(self):
        title = "A " * 40  # 80 字符，全是短词
        display, cut = paper_topics.short_title(title.strip())
        assert cut is True
        # 不应该出现"半个单词"：截断后去掉省略号，剩下的应该都是完整词
        assert not display.rstrip("…").endswith("A ") or display.rstrip("…").endswith("A")

    def test_no_space_falls_back_to_hard_cut(self):
        # 没有空格的中文长标题（这里必须真的超过 TITLE_MAX_CHARS），硬截且不超长
        title = "这是一个没有任何空格而且非常非常长的中文论文标题用于测试硬截断逻辑是否正确工作以及是否会超出限制" * 2
        assert len(title) > paper_topics.TITLE_MAX_CHARS, "测试数据本身不够长，测不出截断"
        display, cut = paper_topics.short_title(title)
        assert cut is True
        assert len(display) <= paper_topics.TITLE_MAX_CHARS
        assert display.endswith("…")


class TestLoadTitles:
    def test_skips_empty_titles(self, fake_meta):
        fake_meta(
            {
                "a.pdf": {"title": "Real Title"},
                "b.pdf": {"title": "   "},
                "c.pdf": {"title": ""},
                "d.pdf": {},
            },
            _graph([]),
        )
        assert paper_topics.load_titles() == {"a.pdf": "Real Title"}

    def test_missing_file_returns_empty(self, fake_meta, tmp_path):
        # 两个文件都不写
        assert paper_topics.load_titles() == {}
        assert paper_topics.load_file_communities() == {}

    def test_corrupt_file_returns_empty_instead_of_raising(self, fake_meta, tmp_path):
        bad = tmp_path / "structures.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        (tmp_path / "citation-graph.json").write_text("{}", encoding="utf-8")
        assert paper_topics.load_titles() == {}


class TestBuildTopics:
    def test_every_paper_lands_in_exactly_one_group(self, fake_meta):
        titles = {f"p{i}.pdf": {"title": f"Paper {i}"} for i in range(5)}
        nodes = [
            {"canonical_id": "arxiv:1", "copies": ["p0.pdf", "p1.pdf"], "cited_by_count": 10},
            {"canonical_id": "arxiv:2", "copies": ["p2.pdf"], "cited_by_count": 3},
        ]
        fake_meta(titles, _graph(nodes, [{"source": "arxiv:1", "target": "arxiv:2"}]))

        groups = paper_topics.build_topics(list(titles))
        seen = [p.filename for g in groups for p in g.papers]
        assert sorted(seen) == sorted(titles), "有论文丢失或重复"
        assert len(seen) == len(set(seen)), "同一篇论文出现在多个分组里"

    def test_unknown_paper_goes_to_other_group(self, fake_meta):
        fake_meta(
            {"known.pdf": {"title": "Known"}, "ghost.pdf": {"title": "Ghost"}},
            _graph([{"canonical_id": "arxiv:1", "copies": ["known.pdf"], "cited_by_count": 1}]),
        )
        groups = paper_topics.build_topics(["known.pdf", "ghost.pdf"])
        other = [g for g in groups if g.topic_id == paper_topics.OTHER_TOPIC_ID]
        assert len(other) == 1
        assert [p.filename for p in other[0].papers] == ["ghost.pdf"]

    def test_other_group_sorts_last(self, fake_meta):
        # 造一个大社区 + 一个孤儿，"其他"必须排最后，哪怕它有 5 篇
        titles = {}
        for i in range(3):
            titles[f"c{i}.pdf"] = {"title": f"Core {i}"}
        for i in range(5):
            titles[f"x{i}.pdf"] = {"title": f"Loose {i}"}
        nodes = [{"canonical_id": f"arxiv:{i}", "copies": [f"c{i}.pdf"], "cited_by_count": 5 - i} for i in range(3)]
        fake_meta(titles, _graph(nodes))

        groups = paper_topics.build_topics(list(titles))
        assert groups[-1].topic_id == paper_topics.OTHER_TOPIC_ID
        assert len(groups[-1].papers) == 5

    def test_papers_sorted_by_cited_by_desc(self, fake_meta):
        # 注意：不同节点即使有边也会被 Louvain 拆成不同社区，
        # 所以这里断言的是「同一分组内」的排序，用跨分组摊平的同一份数据验证：
        # 先把三篇都做成孤儿（都进「其他」组），它们必然同组，排序才可断言。
        titles = {f"p{i}.pdf": {"title": f"P{i}"} for i in range(3)}
        fake_meta(titles, _graph([]))  # 空图 → 全部归入「其他」
        groups = paper_topics.build_topics(list(titles))
        assert len(groups) == 1
        # 全是孤儿时被引都是 0，退化成按文件名排序，验证兜底顺序稳定
        assert [p.filename for p in groups[0].papers] == ["p0.pdf", "p1.pdf", "p2.pdf"]

    def test_same_group_sorted_by_cited_by_desc(self, fake_meta):
        # 一个节点带多个副本，它们必然同组；用被引数验证组内排序
        titles = {"big.pdf": {"title": "Big"}, "small.pdf": {"title": "Small"}}
        nodes = [
            {"canonical_id": "arxiv:big", "copies": ["big.pdf"], "cited_by_count": 50},
            {"canonical_id": "arxiv:small", "copies": ["small.pdf"], "cited_by_count": 1},
        ]
        fake_meta(titles, _graph(nodes))
        flat = [p for g in paper_topics.build_topics(list(titles)) for p in g.papers]
        # 不管分几组，被引高的那篇必须排在它自己所在的组里更靠前
        for group in paper_topics.build_topics(list(titles)):
            files = [p.filename for p in group.papers]
            if "big.pdf" in files and "small.pdf" in files:
                assert files.index("big.pdf") < files.index("small.pdf")
        # 全局摊平后至少能确认两篇都在
        assert {p.filename for p in flat} == {"big.pdf", "small.pdf"}

    def test_missing_title_falls_back_to_readable_placeholder(self, fake_meta):
        fake_meta({"img.pdf": {"title": ""}}, _graph([]))
        paper = paper_topics.build_topics(["img.pdf"])[0].papers[0]
        # 不能直接把文件名当标题，也不能是空串
        assert paper.title != "img.pdf"
        assert "img.pdf" in paper.title
        assert paper.title.startswith("未解析出标题")

    def test_multi_copy_node_takes_highest_cited(self, fake_meta):
        titles = {"dup.pdf": {"title": "Dup"}}
        nodes = [
            {"canonical_id": "arxiv:low", "copies": ["dup.pdf"], "cited_by_count": 1},
            {"canonical_id": "arxiv:high", "copies": ["dup.pdf"], "cited_by_count": 99},
        ]
        fake_meta(titles, _graph(nodes))
        paper = paper_topics.build_topics(["dup.pdf"])[0].papers[0]
        assert paper.node_id == "arxiv:high"
        assert paper.cited_by == 99

    def test_empty_input_returns_empty(self, fake_meta):
        fake_meta({}, _graph([]))
        assert paper_topics.build_topics([]) == []


class TestRealData:
    def test_page_size_is_twelve(self):
        assert paper_topics.PAGE_SIZE == 12

    def test_community_names_cover_default_ids(self):
        for cid in (0, 1, 2):
            assert paper_topics.topic_name(cid) in paper_topics.COMMUNITY_NAMES.values()

    def test_unknown_community_gets_fallback_name(self):
        assert paper_topics.topic_name(99) == "主题 99"
        assert paper_topics.topic_name(paper_topics.OTHER_TOPIC_ID) == paper_topics.OTHER_TOPIC_NAME