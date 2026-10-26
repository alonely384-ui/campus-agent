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


def _norm_cell(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


_CAPTION_ROW = re.compile(r"^(table|figure|fig\.)\s*\d+\s*[:.]", re.IGNORECASE)


def _rows_ok(rows: list[list[str]]) -> bool:
    """判断抽出来的表是真数据表还是"碎片拼起来的假表"。

    质量差的特征（Attention 论文里常见）：列数忽多忽少、满屏单字符。
    """
    if len(rows) < 2:
        return False
    widths = [len(r) for r in rows]
    cols = max(widths)
    if cols < 3:
        return False
    if sum(1 for w in widths if w == cols) / len(widths) < 0.7:   # 列数要基本一致
        return False
    cells = [c for r in rows for c in r if c]
    if not cells:
        return False
    if sum(1 for c in cells if len(c) <= 1) / len(cells) > 0.35:  # 太多单字符碎片
        return False
    return sum(1 for r in rows for c in r if c) / len(rows) >= 2.0  # 每行至少两个非空格


def _pymupdf_table_boxes(pdf_path: str, pages: list[int]) -> dict[int, list[tuple[float, float, float, float]]]:
    """用 PyMuPDF 的线框检测拿到"表格大概在哪"的边框。

    pdfplumber 偶尔会把正文里的半张图误当成表格（Attention 论文就踩过），
    所以要求它的表和 PyMuPDF 检出的表格区域有足够重叠，否则不认。
    """
    try:
        import pymupdf
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    try:
        with pymupdf.open(pdf_path) as doc:
            for pno in pages:
                if not 0 <= pno < len(doc):
                    continue
                boxes = [tuple(t.bbox) for t in doc[pno].find_tables().tables]
                if boxes:
                    out[pno] = boxes
    except Exception:  # noqa: BLE001
        return {}
    return out


def _overlaps(a: tuple, b: tuple) -> float:
    """两个矩形交集面积 / 较小的那个面积（0–1）。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = max((a[2] - a[0]) * (a[3] - a[1]), 1e-6)
    area_b = max((b[2] - b[0]) * (b[3] - b[1]), 1e-6)
    return inter / min(area_a, area_b)


def _table_rows(pdf_path: str, pages: list[int]) -> dict[int, list[list[list[str]]]]:
    """用 pdfplumber 的文本策略把表格抽成"行 × 列"。

    为什么用 text 策略：这批 LaTeX 论文的表格有竖线但没有完整横线，
    线框策略（默认）一张都认不出来；按文字间距推断列反而能还原数据行。
    返回 {0 基页号: [表1 的行, 表2 的行, ...]}，每行是字符串列表。
    只保留「≥2 行且 ≥3 列」的表，避免把两行小表也当结构化结果。
    """
    if not pages:
        return {}
    try:
        import pdfplumber
    except Exception:  # noqa: BLE001 - 没装就当没有表格内容
        return {}
    settings = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "intersection_tolerance": 3,
        "min_words_vertical": 2,
    }
    out: dict[int, list[list[list[str]]]] = {}
    boxes_map = _pymupdf_table_boxes(pdf_path, pages)   # 线框检测给的"大概位置"，用来排除误检
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pno in pages:
                if not 0 <= pno < len(pdf.pages):
                    continue
                boxes = boxes_map.get(pno, [])
                if not boxes:          # 这一页没有线框表格，就别硬抠正文
                    continue
                found: list[list[list[str]]] = []
                page = pdf.pages[pno]
                for box in boxes:
                    try:
                        cropped = page.crop(box)      # 只抠出线框表格那一块，避免整页被当表
                        ptabs = cropped.find_tables(settings) or []
                    except Exception:  # noqa: BLE001
                        continue
                    for tab in ptabs:
                        try:
                            raw = tab.extract()
                        except Exception:  # noqa: BLE001
                            continue
                        rows = [[_norm_cell(c or "") for c in row] for row in raw if row]
                        # 题注行（"Table 2: ..."）常被并进表格，先剔掉再用质量规则判断
                        rows = [r for r in rows if any(r) and not any(_CAPTION_ROW.match(c) for c in r)]
                        if _rows_ok(rows):
                            found.append(rows)
                if found:
                    out[pno] = found
    except Exception:  # noqa: BLE001 - 单篇失败不影响其它字段
        return {}
    return out


def parse_structure(path: str, max_pages: int = 25, with_tables: bool = True) -> dict:
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
        if with_tables and tables:
            rows_map = _table_rows(path, sorted({t["page"] for t in tables}))
            # pdfplumber 在每页按从上到下的顺序返回表格，和题注顺序对齐
            for t in tables:
                found = rows_map.get(t["page"], [])
                t["rows"] = found.pop(0) if found else []
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
