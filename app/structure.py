"""PDF 结构化解析：把「标题 / 作者 / 单位 / 章节树」从版式里抽出来。

为什么需要它：
    检索只认"字面像不像"。"第六作者是谁"这种问题，原文里没有 author / sixth
    这样的词，BM25 和向量都搜不到；但作者列表在版式上非常明显（标题下面、
    字号略大的那一行）。把它抽成字段后，"作者题"就变成查字段，不再靠猜。

核心做法（不猜内容，只看版式信号）：
    1. 用 PyMuPDF 读每个文本行的字号、坐标、是否加粗；
    2. 全文最常见字号 = 正文字号；
    3. 标题 = 第一页里字号 ≥ 正文 1.3 倍的那几行；
    4. 作者 = 标题下方到 Abstract 之间、"人名样式"且不带邮箱/机构词的行；
    5. 章节 = 形如 "3.2 Model Architecture" 且字号略大于正文的行。

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


def parse_structure(path: str) -> dict:
    """返回 {"title", "authors", "sections", "pages"}，抽不到就是空值。"""
    try:
        import pymupdf  # 延迟导入：只有要解析版式时才需要

        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001 —— 解析失败不能拖垮上传
        return {"title": "", "authors": [], "sections": [], "pages": 0}

    try:
        pages = len(doc)
        first = _lines(doc[0], 0)
        # 章节树多看几页（12 页）才够用；标题/作者永远只看第一页
        head = first[:]
        for pno in range(1, min(12, pages)):
            head += _lines(doc[pno], pno)
        body = _body_size(head)
        title = _title(first, body)
        title_y = next((ln["y"] for ln in first if ln["text"] in title), 0.0)
        return {
            "title": title,
            "authors": _authors(first, title_y, body),
            "sections": _sections(head, body),
            "pages": pages,
        }
    except Exception:  # noqa: BLE001
        return {"title": "", "authors": [], "sections": [], "pages": 0}
    finally:
        doc.close()


def section_tree_text(structure: dict) -> str:
    """把章节树渲染成缩进文本（给技术文档/答辩用）。"""
    return "\n".join(
        f"{'  ' * (s['level'] - 1)}{s['number']} {s['title']} (p{s['page']})"
        for s in structure.get("sections", [])
    )
