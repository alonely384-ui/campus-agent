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

import re
from pathlib import Path

from app.layout import layout_summary
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
        # 扫描件抽不出标题时，章节树是借用同一篇论文的文本版（页码按文本版算）；
        # 前端据此在侧栏注明来源，别让人以为扫描件自己解析出了目录。
        "sections_borrowed_from": str(meta.get("sections_borrowed_from") or ""),
        # 赛题 1.3.1「公式和表格的排版」：表格给"表 N + 标题 + 页码"清单，
        # 公式给页码 + 片段（公式的 text 是碎片，只取前 40 字当提示）。
        "tables": _table_list(meta),
        "formula_pages": _formula_pages(meta),
        "formula_samples": _formula_samples(meta),
        # 1.1.2「图表 chart」：图给"图 N + 题注 + 页码"，缩略图按需裁（/paper/{f}/figure/{i}）
        "figures": _figure_list(meta),
        # 版式摘要（单栏/双栏 + 版式切换页），明细走 /paper/{f}/layout
        "layout": layout_summary(path),
    }


def _table_list(meta: dict, limit: int = 40) -> list[dict]:
    """表格清单。只留真有表格行（rows 非空）的条目：
    `_captions()` 会把正文里以 "Table 1 …" 开头的句子也当题注，那些没有表格行。
    """
    items: list[dict] = []
    for tab in (meta.get("tables") or []):
        if not tab.get("rows"):
            continue
        caption = str(tab.get("caption") or "").strip()
        if not caption:
            continue
        items.append(
            {
                "number": str(tab.get("number") or ""),
                "caption": caption[:80],
                # json 里 0 基，和章节/参考文献一个口径
                "page": int(tab.get("page") or 0) + 1,
            }
        )
        if len(items) >= limit:
            break
    return items


def _formula_pages(meta: dict, limit: int = 40) -> list[dict]:
    """公式按页汇总：只报"第几页有几个公式"，点进去还要靠 PDF 自己渲染。"""
    counts: dict[int, int] = {}
    for item in (meta.get("formulas") or []):
        page = int(item.get("page") or 0) + 1
        counts[page] = counts.get(page, 0) + 1
    rows = [{"page": p, "count": c} for p, c in sorted(counts.items()) if p > 0]
    return rows[:limit]


def _formula_samples(meta: dict, limit: int = 30) -> list[dict]:
    """公式片段：每页挑一条最像公式的当代表，前端在页码后面灰字显示。

    为什么不把它们全列出来：公式被 pdf 拆成好几行，全列是一堆 "= f(x)" 碎片。
    为什么不随便挑最长的一条：最长的那条往往是"含公式的正文句"（
    "layers, produce outputs of dimension d_model…"），看着像正文却挂在公式清单里。
    所以按"带运算符 > 英文单词少 > 短"打分。
    """
    operators = set("=∑∫√±×·≈≤≥∂")
    best: dict[int, tuple[tuple, str]] = {}
    for item in (meta.get("formulas") or []):
        page = int(item.get("page") or 0) + 1
        text = " ".join(str(item.get("text") or "").split())
        if page <= 0 or not text:
            continue
        words = len(re.findall(r"[A-Za-z]{2,}", text))
        has_op = any(ch in text for ch in operators)
        score = (has_op, -min(words, 9), -len(text))
        if page not in best or score > best[page][0]:
            best[page] = (score, text)
    rows = [
        {"page": p, "snippet": text[:40] + ("…" if len(text) > 40 else "")}
        for p, (_score, text) in sorted(best.items())
    ]
    return rows[:limit]


def _figure_list(meta: dict, limit: int = 60) -> list[dict]:
    """图表清单：只留题注非空的；每项带 index，前端用它去裁缩略图。"""
    items: list[dict] = []
    for idx, fig in enumerate(meta.get("figures") or []):
        caption = str(fig.get("caption") or "").strip()
        if not caption:
            continue
        items.append(
            {
                "index": idx,
                "number": str(fig.get("number") or ""),
                "caption": caption[:80],
                # json 里 0 基（和章节/表格同口径），这里 +1 给人类看
                "page": int(fig.get("page") or 0) + 1,
            }
        )
        if len(items) >= limit:
            break
    return items


def _section_page_of(sections: list[dict], title: str) -> int | None:
    """在章节表里找标题等于 title 的那一节，返回它的页码（1 基）。"""
    for sec in sections:
        if str(sec.get("title") or "").strip().lower().rstrip(":.") == title:
            return int(sec.get("page") or 0) or None
    return None
