"""局域图谱（赛题 1.5 第 1、2 条）：只在一批论文内部建引用网，并挖出基石与边缘衍生论文。

和 `app/graph.py` 的全库图谱是什么关系：
- 全库图谱：库里的 40 篇一起算，边可以连到库里任何一篇，落盘成 citation-graph.json，离线跑。
- 局域图谱：只认「这一批」里的论文，批次外的论文不参与连边，也不参与排名，当场算。

算法不重写：抽取用 `extract_references`，归并用 `build_nodes`，匹配用 `match_references`，
中心性用 `pagerank` / `betweenness`。本模块只负责三件事：
1. 把批次切成「只含这批节点」的子图；
2. 在子图上重新算中心性（全库的 PageRank 拿到子图上没有任何意义，
   分母里包含了批次外的节点）；
3. 按赛题措辞给角色：基石论文 / 边缘衍生论文。

为什么基石和边缘衍生不能直接用全库那套 5 角色：
赛题问的是「这批 20 篇里，哪一篇是基石，哪几篇是边缘衍生」。
全库的 cornerstone_scores 按全库分位算，一个在子图里被引最多的论文，
放到全库里可能只排中游，用它的分位数会答非所问。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.graph import (
    ReferenceEntry,
    arxiv_id_of,
    betweenness,
    build_nodes,
    extract_references,
    load_structures,
    match_references,
    norm_arxiv,
    norm_title,
    pagerank,
)

logger = logging.getLogger(__name__)

# 一个节点要当「基石论文」，至少得被这批里的 2 篇引用。
# 只被 1 篇引用的论文当基石没有说服力：20 篇的样本里，1 条入边完全可能是巧合。
CORNERSTONE_MIN_CITED = 2

# 「边缘衍生」的判据：自己至少引了这批里的 1 篇（说明它是站在别人肩膀上写的），
# 但自己几乎不被这批里的任何人引用。
DERIVATIVE_MAX_CITED = 1


@dataclass
class LocalPaper:
    """局域图谱里的一个输入单元：一篇 PDF 在磁盘上的位置 + 它的元信息。"""

    filename: str
    path: Path
    title: str = ""
    arxiv_id: str = ""
    paper_id: str = ""
    abstract: str = ""


def _rows_of(papers: list[LocalPaper]) -> list[dict[str, Any]]:
    """把输入整理成 build_nodes 要的 rows 形状。

    标题和 arXiv 号缺失时按文件名兜底 —— 这是 build_graph 对库内论文的做法，
    局域图谱保持一致，否则同一篇论文在两种模式下的 canonical_id 会不一样。
    """
    rows: list[dict[str, Any]] = []
    for p in papers:
        title = p.title or ""
        aid = p.arxiv_id or arxiv_id_of(p.filename, title)
        rows.append(
            {
                "filename": p.filename,
                "paper_id": p.paper_id,
                "title": title,
                "arxiv_id": aid,
                "abstract": p.abstract,
            }
        )
    return rows


def _pick_cornerstone(
    ids: list[str],
    in_deg: dict[str, int],
    pr: dict[str, float],
    title_of: dict[str, str],
) -> str:
    """基石论文 = 内部被引最多；并列时比 PageRank（被有影响力的论文引用更算数）。"""
    cands = [i for i in ids if in_deg.get(i, 0) >= CORNERSTONE_MIN_CITED]
    if not cands:
        # 这批论文互相之间一条边都没有，或者只有零星一条：不硬凑，
        # 由调用方在 summary 里说明「样本内没有达到门槛的基石论文」。
        return ""
    return max(
        cands,
        key=lambda i: (in_deg.get(i, 0), pr.get(i, 0.0), title_of.get(i, "")),
    )


def build_local_graph(
    papers: list[LocalPaper],
    *,
    fuzzy_accept: float | None = None,
    fuzzy_review: float | None = None,
) -> dict[str, Any]:
    """只在这一批论文内部建图，返回前端可直接渲染的结构。

    返回字段：
        nodes / edges      子图（边只保留两端都在这批里的）
        summary            节点数、边数、抽取统计、基石论文
        cornerstone        基石论文（一篇，或空 dict）
        derivatives        边缘衍生论文（可多篇）
        rankings           内部被引榜 / 内部 PageRank 榜 / 内部中介性榜
        papers             每篇论文的角色标注，前端按它上色

    输入论文少于 2 篇时直接返回空图：一篇论文内部谈不上引用关系。
    """
    if not papers:
        raise ValueError("没有选中任何论文")
    if len(papers) < 2:
        raise ValueError("局域图谱至少需要 2 篇论文，1 篇谈不上引用关系")

    rows = _rows_of(papers)
    nodes, merge_decisions = build_nodes(rows)

    # 标题：build_nodes 在缺标题时用文件名 stem 兜底，这里把原标题补回去，
    # 免得前端看到一串 arxiv 编号。
    title_of = {p.filename: p.title for p in papers}
    for n in nodes:
        if n.copies:
            real = title_of.get(n.copies[0]) or ""
            if real:
                n.title = real

    # ---- 抽参考文献 ----
    by_filename = {p.filename: p.path for p in papers}
    refs_by_node: dict[str, list[ReferenceEntry]] = {}
    parse_errors: list[dict[str, str]] = []
    for n in nodes:
        merged: dict[tuple[str, str], ReferenceEntry] = {}
        for copy in n.copies:
            path = by_filename.get(copy)
            if path is None or not path.exists():
                parse_errors.append({"filename": copy, "error": "文件不存在"})
                continue
            try:
                entries = extract_references(path)
            except Exception as exc:  # 解析失败不能拖垮整批，逐篇记下来
                logger.warning("局域图谱抽取参考文献失败：%s", copy, exc_info=True)
                parse_errors.append({"filename": copy, "error": f"{type(exc).__name__}: {exc}"})
                continue
            for e in entries:
                # 去重键：规范化标题 + arXiv 号。同一节点的多个副本常常引用同一篇文献，
                # 不去重会让「引用了同一篇」被算成两条边。
                merged.setdefault((norm_title(e.title or e.raw), norm_arxiv(e.arxiv_id)), e)
        refs_by_node[n.canonical_id] = list(merged.values())

    kwargs: dict[str, float] = {}
    if fuzzy_accept is not None:
        kwargs["fuzzy_accept"] = fuzzy_accept
    if fuzzy_review is not None:
        kwargs["fuzzy_review"] = fuzzy_review
    edges_all, reviews, stats = match_references(nodes, refs_by_node, **kwargs)

    # ---- 切子图：两端都在这批里才算 ----
    # 赛题原文是「用这 20 篇 PDF 的内部引用网络」，所以连到批次外的边必须丢掉。
    #
    # 注意：`match_references` 只会产出「两端都是本批节点」的边 —— 批次外的论文
    # 根本不在 nodes 里，引用它的条目会落进 arxiv_absent / unmatched 桶。
    # 所以这里过滤后其实是空转，`len(edges_all) - len(edges)` 恒为 0，
    # 拿它当「被丢掉的边数」对外展示会永远显示 0，等于撒谎。
    # 真正能说明「这批之外还有多少引用」的口径是 `edges_outside_batch` 下面的
    # `refs_outside_batch`：本批引用条目里、没能连到本批任何一篇的条数。
    ids = [n.canonical_id for n in nodes]
    id_set = set(ids)
    edges = [e for e in edges_all if e.source in id_set and e.target in id_set]

    in_deg: dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)
    for e in edges:
        in_deg[e.target] += 1
        out_deg[e.source] += 1

    edge_payload = [
        {
            "source": e.source,
            "target": e.target,
            "match_method": e.match_method,
            "match_score": round(float(e.match_score), 4),
            "reference_title": e.reference_title,
            "reference_arxiv_id": e.reference_arxiv_id,
        }
        for e in edges
    ]

    # ---- 子图上的中心性：必须重算 ----
    # 直接拿全库 PageRank 会在分母里带上批次外的节点，20 篇的局域图里那是错的。
    pr = pagerank(edges, ids)
    bc = betweenness([{"canonical_id": i} for i in ids], edge_payload)

    base_nodes = [
        {
            "canonical_id": n.canonical_id,
            "title": n.title or n.canonical_id,
            "arxiv_id": n.arxiv_id,
            "copies": list(n.copies),
        }
        for n in nodes
    ]

    corner_id = _pick_cornerstone(ids, in_deg, pr, {n.canonical_id: n.title for n in nodes})

    # ---- 引用深度：以基石论文为根，在无向引用图上做 BFS ----
    # 为什么用无向图：引用方向是「谁引了谁」，但赛题问的是「哪篇离基石远」。
    # 一篇引用了基石的论文（入边方向）和一篇被基石引用的论文（出边方向），
    # 在「学术谱系上离基石多远」这件事上是同一层 —— 都只隔了一步。
    # 只看有向图会把 Layer Normalization（Attention 引了它、没人引它）
    # 判成不可达，而它显然是 Attention 的直接前驱，深度就是 1。
    depth: dict[str, int] = {}
    if corner_id:
        adj: dict[str, set[str]] = {i: set() for i in ids}
        for e in edges:
            adj[e.source].add(e.target)
            adj[e.target].add(e.source)
        depth[corner_id] = 0
        queue = [corner_id]
        while queue:
            cur = queue.pop(0)
            for nb in adj[cur]:
                if nb not in depth:
                    depth[nb] = depth[cur] + 1
                    queue.append(nb)

    # ---- 角色判定（赛题措辞）----
    # 「边缘衍生论文」的判据是**引用深度**，不是简单的出度/入度大小。
    # 迭代记录（两次都判错过，留作证据）：
    #   第一版 `cited <= 1 and cites >= 1` → GPT-4 Technical Report 被判成边缘衍生。
    #       它出度 13（这批里引用别人最多的之一），只因为它是技术报告、PDF 里没有
    #       numbered references 才入度 1。一篇「谁都引了、谁都不引它」的论文
    #       显然不是"衍生"出来的，它是这批里的参照系。
    #   第二版加了 `cites < hub_cites_min` → 20 篇里只剩 Mistral 7B 一篇被判衍生，
    #       18 篇全塌进 hub，分类失去意义。
    #   第三版（当前）：按 BFS 层级。离基石 0 层=基石，1 层=直接衍生，
    #       2 层及以上=更远的衍生。这才是「衍生」这个词在图上的本义。
    max_depth = max(depth.values()) if depth else 0
    # 「边缘」= 深度处于最深那一档。层数少时把最深两档合并，否则 20 篇里几乎人人
    # 都只有 2 层，一刀切会把一半论文都判成边缘。
    #
    # 但有个坑：整批只有 1 层（所有论文都直接连着基石）时，上面这条链式写法会得到
    # edge_depth_min = 1，于是「深度 ≥ 1 且被引 ≤ 1」把所有非基石节点全判成衍生 ——
    # 高扇出的 GPT-4 Technical Report 就这样被误判过。所以这里**兜底为 2**：
    # 衍生论文至少要「和基石之间隔着一篇」，直接相邻的不算边缘。
    edge_depth_min = max(2, max_depth - 1) if max_depth >= 2 else 2

    roles: dict[str, str] = {}
    for i in ids:
        cited = in_deg.get(i, 0)
        cites = out_deg.get(i, 0)
        d = depth.get(i)
        if i == corner_id:
            roles[i] = "cornerstone"          # 基石论文
        elif d is None:
            roles[i] = "isolated"             # 和基石之间没有任何引用路径
        elif d >= edge_depth_min and cited <= DERIVATIVE_MAX_CITED:
            roles[i] = "derivative"           # 边缘衍生：离基石远、又被引得少
        elif cited >= CORNERSTONE_MIN_CITED:
            roles[i] = "hub"                  # 直接被引多但不是基石
        else:
            # 剩下的都是「离基石不远、被引也不多」的中间层。
            # 这里**不能**再写一次 `or cited <= DERIVATIVE_MAX_CITED`：
            # 那个条件会把前面 edge_depth_min 的深度闸门整个作废，
            # 深度 1、只被引 1 次的高扇出节点（GPT-4 Technical Report 那类
            # 参照系，出度十几、入度 1）会再次被判成边缘衍生 —— 这是回归。
            roles[i] = "middle"

    node_payload = []
    for item in base_nodes:
        i = item["canonical_id"]
        cites = out_deg.get(i, 0)
        cited = in_deg.get(i, 0)
        node_payload.append(
            {
                "id": i,
                "title": item["title"],
                "arxiv_id": item["arxiv_id"],
                "copies": item["copies"],
                "cites": cites,
                "cited_by": cited,
                "pagerank": round(pr.get(i, 0.0), 6),
                "betweenness": round(bc.get(i, 0.0), 6),
                "role": roles[i],
                "is_cornerstone": i == corner_id,
                "depth": depth.get(i),
                "reader_url": (
                    f"reader.html?paper={item['copies'][0]}" if item["copies"] else ""
                ),
            }
        )
    node_payload.sort(key=lambda x: (-x["cited_by"], -x["pagerank"]))

    cornerstone = next((n for n in node_payload if n["is_cornerstone"]), None)
    derivative_payload = sorted(
        [n for n in node_payload if n["role"] == "derivative"],
        key=lambda x: (x["cited_by"], -x["cites"], x["title"]),
    )

    def _rank(key: str, limit: int = 10) -> list[dict[str, Any]]:
        return sorted(node_payload, key=lambda x: (-x[key], x["id"]))[:limit]

    summary = {
        "papers_total": len(papers),
        "nodes": len(node_payload),
        "merged": len(papers) - len(node_payload),  # 被归并成同一节点的副本数
        "edges": len(edge_payload),
        # 批次外被丢掉的边：这是「局域」两个字的具体代价，必须让用户看见。
        # 口径说明见上面切子图处的注释：match_references 产不出跨批次的边，
        # 所以这里用「本批参考文献条目里、没连到本批任何一篇的条数」来说话。
        "edges_outside_batch": int(stats.get("entries_total") or 0)
        - int(stats.get("matched_arxiv") or 0)
        - int(stats.get("matched_exact") or 0)
        - int(stats.get("matched_fuzzy") or 0)
        - int(stats.get("self_reference") or 0),
        "refs_entries": int(stats.get("entries_total") or 0),
        "refs_matched": int(stats.get("matched_arxiv") or 0)
        + int(stats.get("matched_exact") or 0)
        + int(stats.get("matched_fuzzy") or 0),
        "parse_errors": parse_errors,
        "review_total": len(reviews),
        "cornerstone_rule": (
            f"内部被引 ≥ {CORNERSTONE_MIN_CITED} 篇里取被引最多，并列比 PageRank"
        ),
        "derivative_rule": (
            f"以基石论文为根做 BFS：深度 ≥ {edge_depth_min} 层且内部被引 ≤ "
            f"{DERIVATIVE_MAX_CITED} 篇；或被引 ≤ {DERIVATIVE_MAX_CITED} 篇且不与基石连通"
        ),
        "depth_note": (
            f"这批论文共 {max_depth + 1} 个引用层级（0=基石，最深 {max_depth} 层）"
            if depth
            else "未判出基石论文，无法计算引用层级"
        ),
        "max_depth": max_depth,
        "isolation_note": (
            "" if corner_id else f"这批论文内部没有任何一篇被引达到 {CORNERSTONE_MIN_CITED} 篇，未判出基石论文"
        ),
    }

    return {
        "nodes": node_payload,
        "edges": edge_payload,
        "summary": summary,
        "cornerstone": cornerstone or {},
        "derivatives": derivative_payload,
        "rankings": {
            "cited_by": _rank("cited_by"),
            "pagerank": _rank("pagerank"),
            "betweenness": _rank("betweenness"),
        },
        "merge_decisions": merge_decisions,
    }


def papers_from_library(filenames: list[str], papers_dir: Path) -> list[LocalPaper]:
    """把「库里勾选的这批文件名」变成 LocalPaper 列表。

    标题优先取 structures.json（和全库图谱同源），取不到退回文件名。
    找不到的论文直接跳过并在日志里留痕，由调用方通过 returned 数量比对发现。
    """
    structures = load_structures()
    out: list[LocalPaper] = []
    seen: set[str] = set()
    for name in filenames:
        name = str(name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        path = papers_dir / name
        if not path.exists():
            logger.warning("局域图谱：库里找不到 %s", name)
            continue
        meta = structures.get(name) or {}
        out.append(
            LocalPaper(
                filename=name,
                path=path,
                title=str(meta.get("title") or ""),
                arxiv_id=arxiv_id_of(name, str(meta.get("title") or "")),
                abstract=str(meta.get("abstract") or ""),
            )
        )
    return out


def papers_from_uploads(items: list[tuple[str, Path]]) -> list[LocalPaper]:
    """把「用户刚上传的一批 PDF」变成 LocalPaper 列表。

    items: [(原始文件名, 临时落盘路径)]
    标题尝试从 structures.json 命中（这批 PDF 若也进过库就有），否则留空，
    由 build_nodes 用文件名兜底 —— 局域图谱不为了拿标题去额外解析一遍 PDF，
    那会让 20 篇的耗时代价翻倍。
    """
    structures = load_structures()
    out: list[LocalPaper] = []
    for name, path in items:
        meta = structures.get(name) or {}
        out.append(
            LocalPaper(
                filename=name,
                path=path,
                title=str(meta.get("title") or ""),
                arxiv_id=arxiv_id_of(name, str(meta.get("title") or "")),
                abstract=str(meta.get("abstract") or ""),
            )
        )
    return out


__all__ = [
    "CORNERSTONE_MIN_CITED",
    "DERIVATIVE_MAX_CITED",
    "LocalPaper",
    "build_local_graph",
    "papers_from_library",
    "papers_from_uploads",
]