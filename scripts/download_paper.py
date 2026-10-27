"""命令行：按论文名/arXiv 号下载论文入库；可选顺着参考文献批量下载（赛题 1.4.1）。

用法：
    python scripts/download_paper.py "Layer normalization"
    python scripts/download_paper.py 1607.06450 --refs --limit 3 --min-cites 2

为什么还留一个命令行入口：
    定时任务（Windows 计划任务 / cron）跑的是脚本，不是网页；
    演示时也能在终端直接给评委看"下载 → 入库 → 索引"的完整流水。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.logging_config import setup_logging  # noqa: E402
from app.online import (  # noqa: E402
    OnlineError,
    arxiv_id_of,
    citation_counts,
    download_pdf,
    paper_meta,
    references,
    search_papers,
)
from app.papers import (  # noqa: E402
    PAPERS_DIR,
    get_structure,
    merge_structure,
    paper_path,
    save_paper,
)
from app.rag.pipeline import index_paper  # noqa: E402


def _pick(query: str, results: list[dict]) -> dict | None:
    import difflib

    if not results:
        return None

    def ratio(candidate: dict) -> float:
        return difflib.SequenceMatcher(
            None, query.lower().strip(), str(candidate.get("title") or "").lower().strip()
        ).ratio()

    best = max(results, key=ratio)
    return best if ratio(best) >= 0.5 else None


def download_one(arxiv_id: str, title: str = "") -> dict:
    """下一个 arXiv 号并走上传同一条链路（去重/解析/索引/合并结构）。"""
    tmp = PAPERS_DIR / f".cli_{arxiv_id}.pdf"
    try:
        download_pdf(arxiv_id, tmp)
        data = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    paper, duplicate, score = save_paper(f"{arxiv_id}.pdf", data)
    structure = get_structure(paper.paper_id)
    if structure:
        merge_structure(paper.filename, structure)
    indexed = 0
    path = paper_path(paper.paper_id)
    if path is not None:
        try:
            indexed = index_paper(str(path), paper.filename)
        except Exception as exc:  # noqa: BLE001
            print(f"  入库失败（文件已保存）：{exc}")
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "filename": paper.filename,
        "pages": paper.pages,
        "indexed": indexed,
        "duplicate_of": duplicate.filename if duplicate else "",
        "duplicate_score": score,
    }


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="按论文名下载 arXiv 论文并入库")
    parser.add_argument("query", help="论文标题，或 arXiv 号（如 1607.06450）")
    parser.add_argument("--refs", action="store_true", help="同时下载这篇的参考文献")
    parser.add_argument("--limit", type=int, default=3, help="参考文献最多下几篇（默认 3）")
    parser.add_argument("--min-cites", type=int, default=2, help="正文引用次数阈值（默认 2）")
    args = parser.parse_args()

    query = args.query.strip()
    arxiv_id = arxiv_id_of(query) if any(ch.isdigit() for ch in query) else ""
    title = ""
    if arxiv_id and "/" not in query:
        title = paper_meta(arxiv_id).get("title", "")
        print(f"目标：{arxiv_id} {title}")
    else:
        print(f"搜索：{query}")
        try:
            results = search_papers(query, 6)
        except OnlineError as exc:
            print(f"搜索失败：{exc}")
            return 2
        best = _pick(query, results)
        if best is None:
            print("没搜到足够接近的论文")
            return 2
        arxiv_id = best["arxiv_id"]
        title = best["title"]
        print(f"命中：{arxiv_id} {title}")

    try:
        info = download_one(arxiv_id, title)
    except (OnlineError, ValueError) as exc:
        print(f"下载失败：{exc}")
        return 3
    print(
        f"已入库：{info['filename']}（{info['pages']} 页，索引 {info['indexed']} 块）"
        + (f"，与 {info['duplicate_of']} 重复（相似度 {info['duplicate_score']:.2f}）" if info["duplicate_of"] else "")
    )

    if not args.refs:
        return 0

    try:
        rows = references(arxiv_id, 40)
    except OnlineError as exc:
        print(f"取参考文献失败：{exc}")
        return 4
    path = paper_path(info["filename"].removesuffix(".pdf"))
    text = ""
    if path is not None:
        import fitz

        with fitz.open(str(path)) as doc:
            text = "\n".join(page.get_text() for page in doc)
    counts = citation_counts(text)
    ranked = []
    for index, row in enumerate(rows):
        if not row.get("arxiv_id"):
            continue
        cites = counts.get(index + 1, 0)
        if cites >= args.min_cites:
            ranked.append((cites, row))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    print(f"参考文献：候选 {len(ranked)} 篇（正文引用 ≥{args.min_cites} 次），下载前 {args.limit} 篇")
    for cites, row in ranked[: args.limit]:
        try:
            done = download_one(row["arxiv_id"], row["title"])
            print(f"  ✓ [{cites} 次] {done['arxiv_id']} {done['filename']}（索引 {done['indexed']} 块）")
        except (OnlineError, ValueError) as exc:
            print(f"  ✗ [{cites} 次] {row['arxiv_id']} {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
