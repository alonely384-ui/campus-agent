"""论文分析页两个模块各自选论文：主题分组的数据源与批量勾选的收窄口径。

前端「勾主题 = 批量勾该主题下全部论文」依赖两件事：
1. `/papers/files?meta=true` 每条带 ``theme``，且顶层给出主题清单；
2. 把某个主题下的文件名清单交给 ``survey(filenames=...)``，收窄结果必须
   正好是该主题在引用关系图里的那批节点——不能多、也不能一个都没有。
"""

import warnings
from unittest import mock

warnings.filterwarnings("ignore")

from app import graph  # noqa: E402
from app.main import _file_themes  # noqa: E402  导入 app.main 即触发安全层路由对账


def _mini_view() -> dict:
    """造一张三篇论文、两个主题的小图视图（形状同 graph_view 的输出）。"""
    def node(nid, title, copies, category):
        return {
            "id": nid, "title": title, "copies": copies,
            "cites": 1, "cited_by": 2, "pagerank": 0.3, "betweenness": 0.1,
            "community": 0, "isolated": False, "cornerstone": 1.0,
            "role": "cornerstone", "reader_url": "", "category": category,
            "year": 2020, "abstract": f"{title} 的摘要首句。",
        }

    nodes = [
        node("arxiv:1706.03762", "Attention Is All You Need", ["1706.03762v7.pdf"], "模型架构"),
        node("arxiv:2005.14165", "GPT-3", ["2005.14165v4.pdf"], "基础模型与预训练"),
        node("arxiv:2203.02155", "InstructGPT", ["2203.02155v1.pdf"], "模型架构"),
    ]
    return {
        "nodes": nodes,
        "edges": [{"source": "arxiv:1706.03762", "target": "arxiv:2005.14165"}],
        "reviews": [],
        "stats": {"nodes": 3, "edges": 1, "reviews": 0},
    }


def test_pick_theme_file_list_selects_exactly_that_theme():
    """按主题挑出的文件名交回 survey()，收窄结果正好是该主题那批节点。"""
    view = _mini_view()
    by_theme: dict[str, list[str]] = {}
    for n in view["nodes"]:
        by_theme.setdefault(n["category"], []).extend(n["copies"])

    assert by_theme == {
        "模型架构": ["1706.03762v7.pdf", "2203.02155v1.pdf"],
        "基础模型与预训练": ["2005.14165v4.pdf"],
    }

    data = graph.survey(view, "", by_theme["模型架构"])
    assert data["ready"] is True
    got = {n["id"] for n in data["citable"]}
    assert got == {"arxiv:1706.03762", "arxiv:2203.02155"}

    data = graph.survey(view, "", by_theme["基础模型与预训练"])
    assert {n["id"] for n in data["citable"]} == {"arxiv:2005.14165"}


def test_single_paper_pick_narrows_to_one():
    """细到单篇：只勾一篇时，citable 就只剩那一篇。"""
    data = graph.survey(_mini_view(), "", ["2203.02155v1.pdf"])
    assert [n["id"] for n in data["citable"]] == ["arxiv:2203.02155"]


def test_file_themes_maps_by_filename_and_never_guesses():
    """文件名 → 主题：查不到就是空串，绝不按标题相似度瞎猜。"""
    idx = {
        "by_title": {
            "attention is all you need": {"file": "1706.03762v7.pdf", "category": "模型架构"},
        },
        "by_arxiv": {
            "2005.14165": {"file": "2005.14165v4.pdf", "category": "基础模型与预训练"},
        },
    }
    with mock.patch.object(graph, "_meta_index", lambda: idx):
        got = _file_themes(["1706.03762v7.pdf", "2005.14165v4.pdf", "不存在的.pdf"])
    assert got == {
        "1706.03762v7.pdf": "模型架构",
        "2005.14165v4.pdf": "基础模型与预训练",
        "不存在的.pdf": "",
    }


def test_papers_files_meta_groups_cover_every_file():
    """真实论文库：每个文件都能分到一个组（没有主题的进「未归入主题」）。"""
    names = [f["filename"] for f in _real_files()]
    themes = _file_themes(names)
    assert set(themes) == set(names)
    assert all(isinstance(v, str) for v in themes.values())
    # 允许有空的，但不允许全是空的：那样分组就没意义了
    assert any(themes.values()), "真实库里一篇有主题的都没有，分组会退化成一个大组"


def _real_files():
    from app.papers import PAPERS_DIR

    return [{"filename": p.name} for p in sorted(PAPERS_DIR.glob("*.pdf"))]