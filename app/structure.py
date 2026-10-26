"""PDF 结构化解析：把「标题 / 作者 / 单位 / 摘要 / 章节树 / 表格 / 图表 / 公式 / 参考文献 / 附录」抽出来。

赛题 1.1.2 要求"解析的粒度越细越好"，字段清单就是评分表：
    标题、作者（顺序 + 单位/邮箱）、摘要、一/二/三级标题、表格、图表、公式、参考文献、附录。

核心做法（不猜内容，只看版式信号）：
    1. 用 PyMuPDF 读每个文本行的字号、坐标、是否加粗；
    2. 全文最常见字号 = 正文字号；
    3. 标题 = 第一页里字号 ≥ 正文 1.3 倍的那几行；
    4. 作者 = 标题下方到 Abstract 之间、"人名样式"且不带邮箱/机构词的行；
    5. 章节 = 形如 "3.2 Model Architecture" 或"编号一行、标题一行"的版式；
    6. 表格/图表 = "Table 2.1:" / "Figure 3:" 这类题注行；
    7. 公式 = 含数学符号且没有成句英文的短行；
    8. 参考文献 = References 之后的 [n] 条目；
    9. 附录 = 标题里带 Appendix 或 A/B 编号的章节。

失败降级：任何一步抽不到就返回空值，绝不抛异常、绝不影响原文检索。
"""

from __future__ import annotations

import re
from collections import Counter

# "3.2 Model Architecture" / "1 Introduction" 这类章节标题
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+([A-Z][A-Za-z0-9 ,:\-]{2,70})$")
# 机构关键词：带这些词的行走人不算作者行
_AFFIL_WORDS = (
    "university", "institute", "department", "google", "brain", "research",
    "inc.", "labs", "laboratory", "college", "school", "center", "centre",
    "abstract", "academy", "corporation", "microsoft", "meta", "openai",
)
# 人名：2~4 个首字母大写的单词，允许中间带 . 或 -
_NAME_RE = re.compile(r"^[A-Z][a-zA-Z.\-']+(?: [A-Z][a-zA-Z.\-']+){1,3}$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 题注：Table 2.1 / Figure 3 / Fig. 3
_TABLE_CAPTION_RE = re.compile(r"^\s*Table\s+(\d+(?:\.\d+)?)\s*[.:]?\s+(.{4,300})", re.IGNORECASE)
_FIGURE_CAPTION_RE = re.compile(r"^\s*(?:Figure|Fig\.)\s+(\d+(?:\.\d+)?)\s*[.:]?\s+(.{4,300})", re.IGNORECASE)
# 参考文献条目：[11] Jimmy Lei Ba, ...
_REF_ENTRY_RE = re.compile(r"^\s*\[(\d{1,3})\]\s+(.{5,200})")
_REF_ENTRY_PATTERNS = [
    re.compile(r"^\s*\[\d{1,3}\]\s+"),                    # [1] Author...
    re.compile(r"^\s*\[[A-Za-z][A-Za-z+\-]+\d{2,4}\]\s*"),  # [Bel+15]（ACM 模板）
    re.compile(r"^\s*[A-Z]\.\s*(?:[A-Z]\.\s*)*[A-Z][a-z]"),  # J. Austin, ...
    re.compile(r"^\s*[A-Z][A-Za-z0-9@&\.\-]{1,40}\.\s+[A-Z]"),  # AI@Meta. / OpenAI.
    re.compile(r"^\s*[A-Z][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’\-]+)*,\s"),  # Amro Abbas,
    re.compile(r"^\s*\d{1,3}\.\s+[A-Z]"),                 # 1. Author...
]


def _is_ref_entry(text: str) -> bool:
    return any(p.match(text) for p in _REF_ENTRY_PATTERNS)
# 公式：含数学符号、长度短、不像成句英文
_MATH_RE = re.compile(r"[=≠≈≤≥±∑∫√∞∂∇×÷]")
_APPENDIX_RE = re.compile(r"appendix|附录", re.IGNORECASE)
# 排版里常带弯引号/零宽字符："‘References’"、"“Abstract”"，比较前先去掉
_QUOTES = str.maketrans({"‘": "", "’": "", "“": "", "”": "", "\u200b": ""})


def _norm(text: str) -> str:
    return text.translate(_QUOTES).strip()


def _lines(page, page_no: int) -> list[dict]:
    """把一页拆成"行"：文本 + 字号 + 纵坐标 + 横坐标 + 是否粗体。"""
    out: list[dict] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:      # 0 = 文本块，1 = 图片
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            out.append(
                {
                    "text": text,
                    "size": max(s.get("size", 0.0) for s in spans),
                    "x": min(s.get("bbox", (0, 0, 0, 0))[0] for s in spans),
                    "y": min(s.get("bbox", (0, 0, 0, 0))[1] for s in spans),
                    "bold": any("bold" in s.get("font", "").lower() or s.get("flags", 0) & 16 for s in spans),
                    "page": page_no,
                }
            )
    return out


def _body_size(lines: list[dict]) -> float:
    """正文字号 = 全文出现次数最多的字号（标题、页脚都是少数派）。"""
    sizes = Counter(round(ln["size"], 1) for ln in lines)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _title(first_page: list[dict], body: float) -> str:
    """标题 = 第一页字号 ≥ 正文 1.3 倍、位置靠上的那些行里最长的一行。"""
    cand = [ln for ln in first_page if ln["size"] >= body * 1.3 and len(ln["text"]) >= 8]
    if not cand:
        return ""
    top = min(ln["y"] for ln in cand)
    band = [ln for ln in cand if ln["y"] - top <= ln["size"] * 2.2]
    band.sort(key=lambda ln: ln["y"])
    return " ".join(ln["text"] for ln in band)[:200]


def _authors(first_page: list[dict], title_y: float, body: float) -> list[str]:
    """作者 = 标题下方、Abstract 之前，长得像人名的行（按 y 分行、按 x 排序）。"""
    names: list[str] = []
    for ln in first_page:
        t = ln["text"].strip()
        if ln["y"] <= title_y or ln["y"] - title_y > 260:
            continue
        if len(t) > 80 or "@" in t:
            continue
        low = t.lower()
        if any(w in low for w in _AFFIL_WORDS):
            continue
        # 一行多个作者（"Ashish Vaswani, Noam Shazeer"）按逗号拆
        parts = [p.strip(" ∗*†‡,") for p in re.split(r",\s*", t)]
        for p in parts:
            if _NAME_RE.match(p):
                names.append(p)
    # 去重但保持原顺序
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _affiliations(first_page: list[dict], title_y: float) -> list[str]:
    """单位/机构：标题下方带机构关键词的行（去重、保序）。"""
    out: list[str] = []
    for ln in first_page:
        if ln["y"] <= title_y or ln["y"] - title_y > 300:
            continue
        low = ln["text"].lower()
        if any(w in low for w in _AFFIL_WORDS) and len(ln["text"]) <= 120:
            out.append(ln["text"])
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))][:12]


def _emails(first_page: list[dict]) -> list[str]:
    """邮箱：整页扫一遍（作者行里常带 ∗ 标注，单独抽更稳）。"""
    found = _EMAIL_RE.findall(" ".join(ln["text"] for ln in first_page))
    seen: set[str] = set()
    return [x for x in found if not (x in seen or seen.add(x))][:12]


def _abstract(lines: list[dict]) -> str:
    """摘要 = "Abstract" 行之后、下一个章节标题之前的段落（截 800 字）。"""
    started = False
    buf: list[str] = []
    for ln in lines:
        t = ln["text"].strip()
        if not started:
            if re.fullmatch(r"(abstract|ABSTRACT)[:：]?", _norm(t)) or _norm(t).lower().startswith("abstract"):
                started = True
                rest = re.sub(r"^(abstract|ABSTRACT)[:：]?\s*", "", _norm(t))
                if rest:
                    buf.append(rest)
            continue
        if _SECTION_RE.match(t) or t in ("Introduction", "INTRODUCTION", "1 Introduction"):
            break
        if t in ("Keywords", "Key words", "KEYWORDS") or t.lower().startswith("keywords"):
            break
        buf.append(t)
        if sum(len(x) for x in buf) > 800:
            break
    return " ".join(buf)[:800].strip()


def _abstract_fallback(first_page: list[dict], body: float) -> str:
    """没有 "Abstract" 标题时（如 LLaMA 3）：首页正文段落直接从标题/作者下方开始。"""
    buf: list[str] = []
    for ln in first_page:
        t = ln["text"].strip()
        if len(t) < 60:                     # 标题、作者、单位等短行跳过
            continue
        if _SECTION_RE.match(t) or t in ("Introduction", "INTRODUCTION", "1 Introduction"):
            break
        if t.lower().startswith(("keywords", "key words")):
            break
        if not (body * 0.85 <= ln["size"] <= body * 1.2):
            continue
        buf.append(t)
        if sum(len(x) for x in buf) > 800:
            break
    return " ".join(buf)[:800].strip()


def _keywords(first_page: list[dict]) -> list[str]:
    """关键词行：Keywords: a; b; c。"""
    for ln in first_page:
        t = ln["text"]
        m = re.match(r"(?i)^key\s*words?\s*[:：]\s*(.+)$", t)
        if m:
            return [k.strip(" .;，、") for k in re.split(r"[;,；，、]", m.group(1)) if k.strip()][:12]
    return []


# 章节编号单独一行的版面（NeurIPS 模板就是这样）："1" 一行、"Introduction" 另一行
_NUMBER_ONLY_RE = re.compile(r"^(\d+(?:\.\d+)*)$")
# 无编号的特殊章节
_SPECIAL_SECTIONS = ("Abstract", "Introduction", "Conclusion", "References", "Appendix", "Acknowledgments", "Acknowledgements")


def _sections(lines: list[dict], body: float) -> list[dict]:
    """章节树：支持两种版式——
    1. 编号与标题同一行："3.2 Model Architecture"；
    2. 编号单独一行："3.2" 换行 "Model Architecture"（本批 PDF 常见）。
    """
    out: list[dict] = []
    prev_number = ""
    for ln in lines:
        t = ln["text"].strip()
        if ln["size"] < body * 0.95:      # 页脚、脚注直接排除
            prev_number = ""
            continue

        m = _SECTION_RE.match(t)
        if m:
            out.append({"number": m.group(1), "title": m.group(2).strip(),
                        "level": m.group(1).count(".") + 1, "page": ln["page"]})
            prev_number = ""
            continue

        n = _NUMBER_ONLY_RE.match(t)
        if n and ln["size"] >= body * 1.05:
            prev_number = n.group(1)
            continue

        # 上一行是纯编号，这一行就是标题
        if prev_number and t[:1].isupper() and 3 <= len(t) <= 80:
            out.append({"number": prev_number, "title": t,
                        "level": prev_number.count(".") + 1, "page": ln["page"]})
            prev_number = ""
            continue

        if t in _SPECIAL_SECTIONS:
            out.append({"number": "", "title": t, "level": 1, "page": ln["page"]})
        prev_number = ""
    return out


def _captions(lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """表格/图表题注：返回 (tables, figures)，每项 {number, caption, page}。"""
    tables: list[dict] = []
    figures: list[dict] = []
    for ln in lines:
        t = ln["text"]
        m = _TABLE_CAPTION_RE.match(t)
        if m:
            tables.append({"number": m.group(1), "caption": m.group(2).strip(), "page": ln["page"]})
            continue
        m = _FIGURE_CAPTION_RE.match(t)
        if m:
            figures.append({"number": m.group(1), "caption": m.group(2).strip(), "page": ln["page"]})
    return tables[:80], figures[:80]


def _formulas(lines: list[dict]) -> list[dict]:
    """公式行：含数学符号、长度 ≤ 120、且不像成句英文（英文单词少于 8 个）。"""
    out: list[dict] = []
    for ln in lines:
        t = ln["text"].strip()
        if not (2 <= len(t) <= 120) or not _MATH_RE.search(t):
            continue
        words = re.findall(r"[A-Za-z]{2,}", t)
        if len(words) >= 8:
            continue
        out.append({"page": ln["page"], "text": t})
    return out[:120]


def _references(lines: list[dict]) -> dict:
    """参考文献：从 References 标题后开始数条目。

    两种常见格式都支持：
    1. 编号式："[1] Jimmy Lei Ba, Jamie Ryan Kiros..."
    2. 作者-年份式（LLaMA 3、Llama 2 等）："Amro Abbas, Kushal Tirumala..."
    """
    start = None
    for i, ln in enumerate(lines):
        if _norm(ln["text"]).lower() == "references":
            start = i
            break
    if start is None:
        return {"page": None, "count": 0, "first": "", "last": ""}
    entries: list[str] = []
    for ln in lines[start + 1:]:
        t = ln["text"].strip()
        if _norm(t).lower() in ("appendix", "appendices", "a appendix"):
            break
        if _is_ref_entry(t):
            entries.append(re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", t))
    page = lines[start]["page"] if start < len(lines) else None
    return {
        "page": page,
        "count": len(entries),
        "first": entries[0] if entries else "",
        "last": entries[-1] if entries else "",
    }


def _appendix(sections: list[dict]) -> list[dict]:
    """附录：标题带 Appendix/附录，或编号形如 A、A.1 的章节。"""
    out = []
    for s in sections:
        num = str(s.get("number") or "")
        if _APPENDIX_RE.search(str(s.get("title") or "")) or re.fullmatch(r"[A-Z](?:\.\d+)*", num):
            out.append(s)
    return out


def _find_references(doc, head: list[dict], pages: int, first_pass: int = 25, limit: int = 90) -> dict:
    """参考文献常在 25 页之后才出现，所以先扫已读行，没有就逐页往后找。"""
    refs = _references(head)
    if refs["count"] > 0:
        return refs
    extra: list[dict] = []
    for pno in range(first_pass, min(limit, pages)):
        extra += _lines(doc[pno], pno)
        if any(_norm(ln["text"]).lower() == "references" for ln in extra):
            return _references(extra)
    return refs


def parse_structure(path: str, max_pages: int = 25) -> dict:
    """返回结构化字段；抽不到就是空值，不抛异常。"""
    empty = {
        "title": "", "authors": [], "affiliations": [], "emails": [], "abstract": "",
        "keywords": [], "sections": [], "tables": [], "figures": [], "formulas": [],
        "references": {"page": None, "count": 0, "first": "", "last": ""}, "appendix": [], "pages": 0,
    }
    try:
        import pymupdf  # 延迟导入：只有要解析版式时才需要

        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001 —— 解析失败不能拖垮上传
        return empty

    try:
        pages = len(doc)
        first = _lines(doc[0], 0)
        # 章节/题注/公式/参考文献要往后看，否则 References 都看不到
        head = first[:]
        for pno in range(1, min(max_pages, pages)):
            head += _lines(doc[pno], pno)
        body = _body_size(head)
        title = _title(first, body)
        title_y = next((ln["y"] for ln in first if ln["text"] in title), 0.0)
        sections = _sections(head, body)
        tables, figures = _captions(head)
        return {
            "title": title,
            "authors": _authors(first, title_y, body),
            "affiliations": _affiliations(first, title_y),
            "emails": _emails(first),
            "abstract": _abstract(first) or _abstract_fallback(first, body),
            "keywords": _keywords(first),
            "sections": sections,
            "tables": tables,
            "figures": figures,
            "formulas": _formulas(head),
            "references": _find_references(doc, head, pages),
            "appendix": _appendix(sections),
            "pages": pages,
        }
    except Exception:  # noqa: BLE001
        return empty
    finally:
        doc.close()


def section_tree_text(structure: dict) -> str:
    """把章节树渲染成缩进文本（给技术文档/答辩用）。"""
    return "\n".join(
        f"{'  ' * (s['level'] - 1)}{s['number']} {s['title']} (p{s['page']})"
        for s in structure.get("sections", [])
    )
