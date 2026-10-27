"""阅读页后端：论文原文件定位 + 章节树元数据。

为什么要单独一个模块（而不是塞进 main.py）：
    "文件名 → 磁盘路径"这一步是安全边界（路径穿越），单独放这里才能写单测；
    路由层只负责把结果变成 HTTP 响应。

页码口径（很重要，前端最容易错）：
    data/papers/structures.json 里的 page 是 **0 基**（第 1 页 = 0）；
    聊天页证据卡片的 page 是 **1 基**（显示"第 8 页"）；
    本模块返回值统一用 1 基（给人看的页码），0 基只存在于 json 里。
"""

from __future__ import annotations

from pathlib import Path

from app.papers import PAPERS_DIR, load_structures

# 摘要只返回前 1200 字：阅读页侧栏只当"这篇讲什么"的提示，不承担全文展示
ABSTRACT_MAX_CHARS = 1200
# 作者列表太长会把侧栏撑爆，取前 12 位（超出显示"等"）
AUTHORS_MAX = 12


def safe_paper_path(filename: str) -> Path | None:
    """把 URL 里的文件名变成本地 PDF 路径；不合法一律返回 None（路由层转 404）。

    挡掉三类输入：
        1. 带路径的：`../app/main.py`、`C:\\Windows\\x.pdf`、`sub/dir.pdf`
        2. 非 PDF：`structures.json`
        3. 解析后不在 data/papers 里的（软链接指向别处也算）
    """
    if not filename or filename != Path(filename).name:
        return None
    if Path(filename).suffix.lower() != ".pdf":
        return None
    root = PAPERS_DIR.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def list_reader_papers() -> list[dict]:
    """阅读页左上角"换一篇"的清单：磁盘上真实存在的 39 篇（带标题/页数）。"""
    structures = load_structures()
    items: list[dict] = []
    for path in sorted(PAPERS_DIR.glob("*.pdf")):
        meta = structures.get(path.name) or {}
        items.append(
            {
                "filename": path.name,
                "title": (meta.get("title") or "").strip() or path.stem,
                "pages": int(meta.get("pages") or 0),
                "has_structure": bool(meta.get("sections")),
            }
        )
    return items


def reader_meta(filename: str) -> dict | None:
    """阅读页需要的元数据：标题/作者/摘要/章节树/参考文献起始页。

    返回 None 表示"这篇不在库里"，路由层转 404。
    """
    path = safe_paper_path(filename)
    if path is None:
        return None

    meta = load_structures().get(filename) or {}
    sections = [
        {
            "number": str(sec.get("number") or ""),
            "title": str(sec.get("title") or ""),
            "level": int(sec.get("level") or 1),
            # json 里是 0 基，这里 +1 换成人类页码；缺失就置 0 让前端跳过跳转
            "page": int(sec.get("page") or 0) + 1,
        }
        for sec in (meta.get("sections") or [])
        if str(sec.get("title") or "").strip()
    ]

    refs = meta.get("references") or {}
    ref_page = refs.get("page")
    return {
        "filename": filename,
        "size": path.stat().st_size,
        "title": (meta.get("title") or "").strip() or path.stem,
        "authors": [str(a) for a in (meta.get("authors") or [])[:AUTHORS_MAX]],
        "abstract": str(meta.get("abstract") or "")[:ABSTRACT_MAX_CHARS],
        "pages": int(meta.get("pages") or 0),
        "sections": sections,
        "references": {
            # json 里 0 基，这里 +1；references.page 缺失时退化为标题含 References 的章节
            "page": (int(ref_page) + 1 if ref_page is not None
                     else _section_page_of(sections, "references")),
            "count": int(refs.get("count") or 0),
        },
        "appendix": [
            {"number": str(a.get("number") or ""), "title": str(a.get("title") or ""),
             "page": int(a.get("page") or 0) + 1}
            for a in (meta.get("appendix") or [])
        ],
    }


def _section_page_of(sections: list[dict], title: str) -> int | None:
    """在章节表里找标题等于 title 的那一节，返回它的页码（1 基）。"""
    for sec in sections:
        if str(sec.get("title") or "").strip().lower().rstrip(":.") == title:
            return int(sec.get("page") or 0) or None
    return None
