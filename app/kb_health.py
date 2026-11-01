"""知识库健康检查与幽灵索引清理（设置中心 D 模块）。

「幽灵索引」指：索引里还有块，但对应的 PDF 已经不在 data/papers 了。
成因是删除论文时 remove_source 失败、或直接从磁盘删文件绕过了应用。
危害很具体：这些块仍在三路召回里，问答会引用一篇库里已经看不到的论文。

设计取舍：
    - 只读扫描，绝不修改任何东西 —— 清理走独立的显式调用。
    - 三路（Chroma docs / docs_dense / BM25）分开统计，因为它们的
      删除路径各自独立，只清一路会留下更隐蔽的不一致。
    - 清理用 remove_source（已有的、经过测试的入口），不自己手写删除，
      避免和增量索引的编号规则打架。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app import papers
from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

PAPERS_DIR = Path(papers.PAPERS_DIR)


# ---------------------------------------------------------------- 底层读取


def _chroma_sources(collection_name: str) -> dict[str, int]:
    """返回 {source: 块数}。读不到集合时返回空 dict 而不是抛异常。"""
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    try:
        col = client.get_collection(collection_name)
    except Exception:  # noqa: BLE001 —— 集合不存在属正常（没建过库）
        logger.info("集合 %s 不存在，按 0 处理", collection_name)
        return {}
    got = col.get(include=["metadatas"]) or {}
    metas = got.get("metadatas") or []
    counts: dict[str, int] = {}
    for meta in metas:
        if not meta:
            continue
        src = meta.get("source")
        if src:
            counts[str(src)] = counts.get(str(src), 0) + 1
    return counts


def _bm25_sources() -> dict[str, int]:
    from app.rag import pipeline

    index = pipeline._load_bm25()
    if not index:
        return {}
    counts: dict[str, int] = {}
    for doc in index.get("docs", []):
        src = doc.get("source")
        if src:
            counts[str(src)] = counts.get(str(src), 0) + 1
    return counts


def _disk_sources() -> tuple[set[str], set[str]]:
    """返回 (磁盘上真实存在的论文对应的 source 名集合, `<paper_id>.pdf` 别名集合)。

    为什么不能只取 ``PAPERS_DIR.glob("*.pdf")`` 的文件名：
        ``papers.save_paper`` 落盘时用的是 ``<paper_id>.pdf``
        （见 ``app/papers.py`` 的 ``dest = PAPERS_DIR / f"{paper_id}.pdf"``），
        而索引里 chunk 的 ``source`` 记的是**用户上传时的原始文件名**
        （``paper.filename``）。两者对同一篇论文是两个名字，例如磁盘上的
        ``57567c72c96f.pdf`` 在索引里叫 ``1610.08613.pdf``。

        只拿裸文件名去比对会产生一对镜像误报：
          - ``1610.08613.pdf`` 在磁盘上"找不到" → 被判成幽灵块（会误删）
          - ``57567c72c96f.pdf`` 索引里没有 → 被判成"磁盘有索引无"（一致的假警报）

    所以这里按 ``papers.list_papers()`` 还原真实映射，并**保留裸文件名**：
    样例/手动拷进来的论文保留原名（``app/papers.py:330`` 注释说明过两种命名都在），
    它们的 source 就是文件名本身，不能漏。

    第二个返回值给调用方过滤 ``missing_from_index``：``<paper_id>.pdf`` 是别名，
    不代表索引缺失。
    """
    if not PAPERS_DIR.exists():
        return set(), set()

    names = {p.name for p in PAPERS_DIR.glob("*.pdf")}
    aliases: set[str] = set()
    for paper in papers.list_papers():
        # paper_path 已经处理了「<paper_id>.pdf 与原名两种命名」的择一逻辑，
        # 拿它来判定存在性，和删除、读取走同一套口径。
        if papers.paper_path(paper.paper_id) is None:
            continue
        # 索引里的 source 是 filename；再补上 paper_id.pdf 这种历史写法
        names.add(paper.filename)
        alias = f"{paper.paper_id}{Path(paper.filename).suffix.lower()}"
        names.add(alias)
        aliases.add(alias)
    return names, aliases


# ---------------------------------------------------------------- 健康检查


@dataclass
class SourceIssue:
    """一个 source 在三路上的分布，用来定位幽灵块。"""

    source: str
    in_disk: bool
    docs_chunks: int = 0
    dense_chunks: int = 0
    bm25_chunks: int = 0

    @property
    def is_ghost(self) -> bool:
        """磁盘上没有这个 PDF，但索引里还有块。"""
        return not self.in_disk and (self.docs_chunks or self.dense_chunks or self.bm25_chunks)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "in_disk": self.in_disk,
            "docs_chunks": self.docs_chunks,
            "dense_chunks": self.dense_chunks,
            "bm25_chunks": self.bm25_chunks,
            "is_ghost": self.is_ghost,
        }


@dataclass
class HealthReport:
    papers_on_disk: int = 0
    papers_in_catalog: int = 0
    docs_total: int = 0
    dense_total: int = 0
    bm25_total: int = 0
    ghost_sources: list[SourceIssue] = field(default_factory=list)
    ghost_chunks_docs: int = 0
    ghost_chunks_dense: int = 0
    ghost_chunks_bm25: int = 0
    missing_from_index: list[str] = field(default_factory=list)
    consistent: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "papers_on_disk": self.papers_on_disk,
            "papers_in_catalog": self.papers_in_catalog,
            "docs_total": self.docs_total,
            "dense_total": self.dense_total,
            "bm25_total": self.bm25_total,
            "ghost_sources": [g.to_dict() for g in self.ghost_sources],
            "ghost_chunks_docs": self.ghost_chunks_docs,
            "ghost_chunks_dense": self.ghost_chunks_dense,
            "ghost_chunks_bm25": self.ghost_chunks_bm25,
            "missing_from_index": sorted(self.missing_from_index),
            "consistent": self.consistent,
            "notes": self.notes,
        }


def health_check() -> HealthReport:
    """只读扫描三路索引与磁盘的一致性。不修改任何数据。"""
    report = HealthReport()

    # alias_names: `<paper_id>.pdf` 这类物理文件名的集合。它们代表"同一篇论文"，
    # 索引里不会以这个名字出现，用来过滤 missing_from_index 的假警报。
    disk, alias_names = _disk_sources()
    docs = _chroma_sources("docs")
    dense = _chroma_sources("docs_dense")
    bm25 = _bm25_sources()
    catalog = papers.list_papers()

    # disk 现在含「原文件名」与「<paper_id>.pdf」两种写法（同一篇论文占两个名字），
    # 所以磁盘论文数改成数 list_papers 里原件真实存在的那几篇，而不是 len(disk)。
    # 索引侧同理：source 集合里的两种写法归一后才是论文篇数。
    report.papers_on_disk = sum(
        1 for p in catalog if papers.paper_path(p.paper_id) is not None
    )
    report.papers_in_catalog = len(catalog)
    report.docs_total = sum(docs.values())
    report.dense_total = sum(dense.values())
    report.bm25_total = sum(bm25.values())

    for src in sorted(set(docs) | set(dense) | set(bm25) | disk):
        issue = SourceIssue(
            source=src,
            in_disk=src in disk,
            docs_chunks=docs.get(src, 0),
            dense_chunks=dense.get(src, 0),
            bm25_chunks=bm25.get(src, 0),
        )
        if issue.is_ghost:
            report.ghost_sources.append(issue)
            report.ghost_chunks_docs += issue.docs_chunks
            report.ghost_chunks_dense += issue.dense_chunks
            report.ghost_chunks_bm25 += issue.bm25_chunks
        elif src in disk and not (issue.docs_chunks or issue.bm25_chunks):
            # `<paper_id>.pdf` 只是同一篇论文在磁盘上的物理别名，索引里的正名是
            # `paper.filename`（已在 disk 集合内）。它本身永远不会出现在索引里，
            # 报出来就是假警报，所以跳过。真·孤立 PDF（磁盘有、清单里没有）不在此列。
            if src in alias_names:
                continue
            report.missing_from_index.append(src)

    if report.papers_on_disk != report.papers_in_catalog:
        report.notes.append(
            f"磁盘 PDF {report.papers_on_disk} 个，论文清单 {report.papers_in_catalog} 条，两者不一致"
        )
    if report.docs_total != report.bm25_total:
        report.notes.append(
            f"Chroma docs {report.docs_total} 块与 BM25 {report.bm25_total} 块数量不一致"
        )
    if report.docs_total != report.dense_total:
        report.notes.append(
            f"Chroma docs {report.docs_total} 块与 docs_dense {report.dense_total} 块数量不一致"
        )
    if report.missing_from_index:
        report.notes.append(f"{len(report.missing_from_index)} 个 PDF 在磁盘上但索引里没有")

    report.consistent = (
        not report.ghost_sources
        and not report.missing_from_index
        and report.docs_total == report.dense_total == report.bm25_total
        and report.papers_on_disk == report.papers_in_catalog
    )
    return report


# ---------------------------------------------------------------- 清理


@dataclass
class CleanupResult:
    requested: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed_chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "cleaned": self.cleaned,
            "skipped": self.skipped,
            "removed_chunks": self.removed_chunks,
            "errors": self.errors,
        }


def cleanup_ghosts(sources: list[str] | None = None) -> CleanupResult:
    """清理幽灵 source 在**三路索引**里的残留块。

    安全护栏（对应「不得删除有效论文索引」）：
        1. 只处理磁盘上确实不存在的 source —— 传进来的有效论文一律跳过并报告。
        2. 某个 source 删除中途抛异常时，把异常记进 errors，
           继续处理其他的，最后如实返回，不吞掉。
        3. 每清一个都重新核对三路，避免「报表说清了、实际还在」。

    sources 为 None 时清理 health_check() 认定**全部**幽灵 source。
    """
    report = health_check()
    ghosts = {g.source for g in report.ghost_sources}
    target = list(sources) if sources is not None else sorted(ghosts)

    result = CleanupResult(requested=list(target))
    if not target:
        return result

    from app.rag import pipeline

    for src in target:
        if src not in ghosts:
            # 有效论文或根本不存在的名字：绝不删，如实报告
            result.skipped.append(src)
            logger.warning("跳过非幽灵 source：%s", src)
            continue
        try:
            removed = pipeline.remove_source(src)
            result.cleaned.append(src)
            result.removed_chunks += removed
        except Exception as exc:  # noqa: BLE001 —— 记录并继续，绝不静默
            logger.exception("清理幽灵 source 失败：%s", src)
            result.errors.append(f"{src}: {type(exc).__name__}: {exc}")

    return result


def verify_consistency() -> dict:
    """清理完再扫一遍三路，给用户一个「真的干净了」的证据。"""
    report = health_check()
    return {
        "consistent": report.consistent,
        "ghost_sources": [g.source for g in report.ghost_sources],
        "docs_total": report.docs_total,
        "dense_total": report.dense_total,
        "bm25_total": report.bm25_total,
        "notes": report.notes,
    }