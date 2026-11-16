"""局域图谱（赛题 1.5.1 / 1.5.2）的单元测试。

`build_local_graph` 本身要从 PDF 里抽参考文献，这里用 monkeypatch 把
`local_graph.extract_references` 换掉，喂进假的参考文献列表，
所以在没有 PDF、没建索引的机器上也能跑。

注意两件事（都是踩过的坑）：
1. `build_local_graph` 会用 `path.exists()` 过滤，所以 LocalPaper 的 path
   必须是**真实存在的文件**，否则整批都被当作解析失败跳过，最后拿到一张空图。
   这里统一用 tmp_path 建空文件。
2. 节点的 canonical_id 优先取 arXiv 编号（`arxiv:1000.0001`），没有编号才落到
   `hash:<内容哈希>`。假数据里如果引用条目带编号、节点自己却没编号，
   就会走标题层，结果是两条不同的 id 对不上——所以造数据时编号要对齐。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import local_graph
from app.graph import ReferenceEntry
from app.local_graph import (
    CORNERSTONE_MIN_CITED,
    DERIVATIVE_MAX_CITED,
    LocalPaper,
    build_local_graph,
    papers_from_library,
    papers_from_uploads,
)


@pytest.fixture
def fake_pdfs(tmp_path, monkeypatch):
    """建真实（空的）PDF 文件，并把参考文献抽取换成查表。"""

    def _install(specs: list[tuple[str, str, str, list[tuple[str, str]]]], paths: dict | None = None):
        """specs: [(文件名, 标题, arXiv 编号, [(被引标题, 被引编号)])]"""
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
        return papers

    return _install


# ---------------- 基石论文（1.5.2 核心节点挖掘）----------------
def test_cornerstone_is_the_most_cited_within_batch(fake_pdfs):
    """被这批里最多篇论文引用的那一篇，就是基石论文。"""
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Base Paper", "1000.0001")]),
            ("b.pdf", "Paper B", "", [("Base Paper", "1000.0001")]),
            ("c.pdf", "Paper C", "", [("Base Paper", "1000.0001")]),
            ("d.pdf", "Base Paper", "1000.0001", []),
        ]
    )
    data = build_local_graph(papers)
    assert data["cornerstone"]["title"] == "Base Paper"
    assert data["cornerstone"]["cited_by"] == 3
    assert data["cornerstone"]["is_cornerstone"] is True
    assert data["summary"]["edges"] == 3
    assert [n["role"].count("cornerstone") for n in data["nodes"]].count(1) == 1


def test_cornerstone_result_is_input_order_independent(fake_pdfs):
    """两篇完全对称时，结果不能随输入顺序变（否则同一批论文两次跑出不同结论）。"""
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Base Paper", "1000.0001")]),
            ("b.pdf", "Paper B", "", [("Base Paper", "1000.0001")]),
            ("d.pdf", "Base Paper", "1000.0001", []),
            ("e.pdf", "Paper E", "", [("Base Paper", "1000.0001")]),
        ]
    )
    d1 = build_local_graph(list(papers))
    d2 = build_local_graph(list(reversed(papers)))
    assert d1["cornerstone"]["id"] == d2["cornerstone"]["id"]


def test_no_cornerstone_when_batch_has_no_internal_citation(fake_pdfs):
    """这批论文互不引用时不硬凑基石，并给出可读的说明。"""
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Someone Else", "9999.9999")]),
            ("b.pdf", "Paper B", "", []),
        ]
    )
    data = build_local_graph(papers)
    assert data["cornerstone"] == {}
    assert data["summary"]["edges"] == 0
    # 批次外被丢掉的引用必须如实报数（口径：没连到本批任何一篇的条目数）
    assert data["summary"]["edges_outside_batch"] == 1
    assert CORNERSTONE_MIN_CITED == 2
    assert "未判出基石论文" in data["summary"]["isolation_note"]


# ---------------- 边缘衍生论文 ----------------
def test_high_fanout_reference_frame_is_not_derivative(fake_pdfs):
    """回归：高扇出的「参照系」不能被判成边缘衍生。

    GPT-4 Technical Report 那类的特征：引用别人一大堆（出度高），
    自己几乎没人引（入度 0~1，因为技术报告没有 numbered references）。
    第一版只看 `cited <= 1` 就把它判成衍生 —— 这是错的，它是这批的参照系，不是衍生物。
    """
    papers = fake_pdfs(
        [
            ("center.pdf", "Center Paper", "1000.0001", [("GPT4 Report", "2303.08774")]),
            ("a.pdf", "Paper A", "1111.0001", [("Center Paper", "1000.0001")]),
            ("b.pdf", "Paper B", "1111.0002", [("Center Paper", "1000.0001")]),
            (
                "gpt4.pdf",
                "GPT4 Report",
                "2303.08774",
                [
                    ("Center Paper", "1000.0001"),
                    ("Paper A", "1111.0001"),
                    ("Paper B", "1111.0002"),
                    ("Paper C", ""),
                    ("Paper D", ""),
                ],
            ),
        ]
    )
    data = build_local_graph(papers)
    by_title = {n["title"]: n for n in data["nodes"]}
    gpt4 = by_title["GPT4 Report"]
    # cites = 它引了本批里几篇（出度），cited_by = 本批里有几篇引了它（入度）
    assert gpt4["cites"] == 3
    assert gpt4["cited_by"] == 1  # 只有 center 引了它
    # 它直接和基石相邻（深度 1），不是「离基石很远」
    assert gpt4["depth"] == 1
    # 达到边缘衍生门槛（被引 1 <= 1）但深度不够，所以不能被判成衍生
    assert gpt4["cited_by"] <= DERIVATIVE_MAX_CITED
    assert gpt4["role"] != "derivative", "高扇出参照系被判成边缘衍生，这是回归"
    assert gpt4["role"] == "middle"


def test_derivative_needs_far_depth_and_low_citation(fake_pdfs):
    """边缘衍生 = 离基石远（深度够）**且** 内部被引少。"""
    papers = fake_pdfs(
        [
            ("corner.pdf", "Corner Paper", "1000.0001", []),
            ("mid.pdf", "Mid Paper", "1000.0002", [("Corner Paper", "1000.0001")]),
            # far 只引 mid，不直接引基石 —— 这样它离基石才真的是 2 层。
            # 如果顺手也引了基石，BFS 深度会直接短路成 1，测不出「远」这件事。
            ("far.pdf", "Far Paper", "1000.0003", [("Mid Paper", "1000.0002")]),
            ("x.pdf", "Paper X", "1111.0001", [("Corner Paper", "1000.0001")]),
            ("y.pdf", "Paper Y", "1111.0002", [("Corner Paper", "1000.0001")]),
        ]
    )
    data = build_local_graph(papers)
    roles = {n["title"]: n["role"] for n in data["nodes"]}
    depths = {n["title"]: n["depth"] for n in data["nodes"]}
    assert roles["Corner Paper"] == "cornerstone"
    assert depths["Corner Paper"] == 0
    # far 和基石之间隔着 mid，深度必然 >= 2
    assert depths["Far Paper"] >= 2
    assert DERIVATIVE_MAX_CITED == 1
    derivative_titles = [d["title"] for d in data["derivatives"]]
    assert "Far Paper" in derivative_titles
    # 直接被基石引用的那两篇（被引 1，深度 1）不会被判成边缘衍生
    assert "Paper X" not in derivative_titles


def test_roles_cover_every_node(fake_pdfs):
    """每个节点都必须有角色，不能让前端拿到 None 上不了色。"""
    papers = fake_pdfs(
        [
            ("base.pdf", "Base Paper", "1000.0001", []),
            ("a.pdf", "Paper A", "", [("Base Paper", "1000.0001")]),
            ("b.pdf", "Paper B", "", [("Base Paper", "1000.0001")]),
            ("lonely.pdf", "Lonely Paper", "", []),
        ]
    )
    data = build_local_graph(papers)
    valid = {"cornerstone", "hub", "middle", "derivative", "isolated"}
    for n in data["nodes"]:
        assert n["role"] in valid, n["title"]
    lonely = next(n for n in data["nodes"] if n["title"] == "Lonely Paper")
    assert lonely["role"] == "isolated"
    assert lonely["depth"] is None


# ---------------- 局域性 ----------------
def test_edges_outside_batch_are_dropped(fake_pdfs):
    """赛题原文要求「用这 20 篇的内部引用网络」，批次外的边必须不在图里。"""
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Outside Paper", "9999.0001"), ("Inside Paper", "1000.0001")]),
            ("b.pdf", "Inside Paper", "1000.0001", []),
        ]
    )
    data = build_local_graph(papers)
    assert data["summary"]["edges"] == 1
    assert data["summary"]["edges_outside_batch"] == 1
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["source"] in ids
        assert e["target"] in ids


def test_less_than_two_papers_raises():
    """1 篇谈不上引用关系，0 篇更谈不上。"""
    with pytest.raises(ValueError):
        build_local_graph([])
    with pytest.raises(ValueError):
        build_local_graph([LocalPaper(filename="a.pdf", path=Path("a.pdf"))])


def test_centrality_is_recomputed_on_subgraph(fake_pdfs):
    """PageRank 必须在子图上重算 —— 归一化后所有节点分数之和应为 1。

    若沿用全库 PageRank（分母含批次外节点），这个和不会等于 1。
    """
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Paper B", "1000.0002")]),
            ("b.pdf", "Paper B", "1000.0002", [("Paper C", "1000.0003")]),
            ("c.pdf", "Paper C", "1000.0003", []),
        ]
    )
    data = build_local_graph(papers)
    total = sum(n["pagerank"] for n in data["nodes"])
    assert 0.98 <= total <= 1.02


def test_missing_pdf_is_reported_not_silently_dropped(fake_pdfs, tmp_path):
    """文件不存在时必须留下 parse_errors，不能悄无声息地给一张空图。"""
    papers = fake_pdfs(
        [
            ("a.pdf", "Paper A", "", [("Base Paper", "1000.0001")]),
            ("d.pdf", "Base Paper", "1000.0001", []),
        ]
    )
    papers[0].path.unlink()  # 模拟文件被删
    data = build_local_graph(papers)
    errors = [e["filename"] for e in data["summary"]["parse_errors"]]
    assert "a.pdf" in errors


# ---------------- 入口函数 ----------------
def test_papers_from_library_skips_unknown_names(tmp_path):
    """库里找不到的文件不能被当成论文塞进图。"""
    (tmp_path / "known.pdf").write_bytes(b"%PDF-1.4\n")
    out = papers_from_library(["known.pdf", "nope.pdf", ""], tmp_path)
    assert [p.filename for p in out] == ["known.pdf"]
    assert out[0].path.exists()


def test_papers_from_uploads_keeps_given_paths(tmp_path):
    """上传模式用的是临时目录里的真路径，不能换成别的目录。"""
    path = tmp_path / "uploaded.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    out = papers_from_uploads([("uploaded.pdf", path)])
    assert len(out) == 1
    assert out[0].path == path