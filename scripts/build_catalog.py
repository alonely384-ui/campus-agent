"""生成论文库清单 `data/catalog.json`。

为什么要这份清单：向量检索每次只回 top-k 个片段，模型看不到"全库"，
问"一共有多少篇/都有哪些论文"时它只能数片段里出现过几篇。
清单是"全库视角"的数据源，由 `app/agent.py` 在全库类问题上注入上下文。

用法：
    python scripts/build_catalog.py              # 扫描 data/papers/*.pdf
    python scripts/build_catalog.py --no-title   # 不解析标题（快，仅统计）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.structure import parse_structure  # noqa: E402

PAPERS_DIR = BASE / "data" / "papers"
OUT_PATH = BASE / "data" / "catalog.json"
BM25_PATH = BASE / "data" / "chroma" / "bm25.json"
ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?", re.I)


def indexed_sources() -> set[str]:
    """读 BM25 倒排表里的来源文件名，判断哪些论文真的进了索引。"""
    try:
        data = json.loads(BM25_PATH.read_text(encoding="utf-8"))
        return {d.get("source") for d in data.get("docs", []) if d.get("source")}
    except (OSError, json.JSONDecodeError):
        return set()


def arxiv_id(name: str) -> str:
    """从文件名里取 arXiv 编号（如 1706.03762v7.pdf → 1706.03762）。"""
    m = ARXIV_RE.match(name)
    return m.group(1) if m else ""


def year_from_arxiv(arxiv: str) -> int | None:
    """arXiv 编号前两位是年份：1706 → 2017，2205 → 2022。"""
    if len(arxiv) < 4 or not arxiv[:2].isdigit():
        return None
    yy = int(arxiv[:2])
    return 2000 + yy if yy < 90 else 1900 + yy


def page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path), strict=False).pages)
    except Exception:  # noqa: BLE001
        return None


def previous_items() -> dict[str, dict]:
    """重建清单时保留已有的人工/规则字段（category、topic、arxiv_category），别覆盖。"""
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):
        return {d.get("file"): d for d in data if isinstance(d, dict) and d.get("file")}
    return {}


def main() -> None:
    no_title = "--no-title" in sys.argv
    items: list[dict] = []
    old = previous_items()
    pdfs = sorted(PAPERS_DIR.glob("*.pdf"))
    indexed = indexed_sources()
    for i, pdf in enumerate(pdfs, 1):
        arxiv = arxiv_id(pdf.name)
        prev = old.get(pdf.name, {})
        title = ""
        authors = 0
        if not no_title:
            try:
                st = parse_structure(str(pdf), with_tables=False)
                title = (st.get("title") or "").strip()
                authors = len(st.get("authors") or [])
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] 标题解析失败 {pdf.name}: {type(e).__name__}")
        items.append(
            {
                "file": pdf.name,
                "title": title or prev.get("title") or pdf.stem,
                "authors_count": authors or prev.get("authors_count", 0),
                "pages": page_count(pdf),
                "arxiv_id": arxiv,
                "year": year_from_arxiv(arxiv),
                "category": prev.get("category") or "未分类",
                "topic": prev.get("topic"),
                "arxiv_category": prev.get("arxiv_category"),
                "indexed": pdf.name in indexed,
            }
        )
        print(f"[{i}/{len(pdfs)}] {pdf.name} | 标题: {title[:50] or '(未抽到)'} | arXiv: {arxiv or '-'}")

    OUT_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    got = sum(1 for i in items if i["arxiv_id"])
    print(f"\n共 {len(items)} 篇，写入 {OUT_PATH}")
    print(f"其中能解析出 arXiv 编号的：{got} 篇（下一步 B1 用它们查 primary_category）")


if __name__ == "__main__":
    main()
