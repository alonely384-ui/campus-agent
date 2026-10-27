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
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9 ,:\-]{2,70})$")
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
_NUMBER_ONLY_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?$")
# 目录页的点引导："Model and Architectures . . . . . . . . 8"
_TOC_LEADER_RE = re.compile(r"(?:\.\s){4,}|…{3,}")
# 目录标题行
_TOC_TITLE_RE = re.compile(r"(?i)^(table of )?contents$")
# 无编号的特殊章节
_SPECIAL_SECTIONS = ("Abstract", "Introduction", "Conclusion", "References", "Appendix", "Acknowledgments", "Acknowledgements")
# ICLR/ACL 常见的 run-in 小标题：加粗的 "Datasets." 直接接正文，没有编号
_RUNIN_RE = re.compile(r"^([A-Z][A-Za-z0-9 &’'\-/]{2,40})\.(?:\s+|$)")
# run-in 标题里不能当成小节的词（它们是大标题或题注）
_RUNIN_STOP = frozenset({
    "abstract", "introduction", "conclusion", "conclusions", "references",
    "acknowledgements", "acknowledgments", "appendix", "figure", "fig", "table",
})
# Title Case 判断时允许的小写虚词
_RUNIN_SMALL = frozenset({"of", "and", "the", "in", "on", "for", "with", "to", "a", "an", "or", "at", "by", "via", "vs"})


def _runin_title(t: str) -> str | None:
    """把加粗的 "Datasets." / "Terminologies and Conventions" 认成二级小标题。

    要求 Title Case（虚词可小写）+ 不超过 6 个词，否则 "WikiSQL is introduced in
    Zhong et al." 这种加粗句子也会被当成标题。
    """
    core = t.rstrip()
    if core.endswith(":"):            # 图里的 "Encoder:" / "Decoder:" 是标注，不是标题
        return None
    m = _RUNIN_RE.match(core)
    if m and not core[: m.end()].endswith("),"):
        core = m.group(1).strip()
    elif "." in core:
        return None
    words = core.split()
    if not 1 <= len(words) <= 6:
        return None
    if len(words) == 1 and len(core) < 4:      # "Rd"、"Fig" 这种不成标题
        return None
    if not all(w.strip("()[]:,")[:1].isupper() or w.lower() in _RUNIN_SMALL for w in words):
        return None
    if core.lower() in _RUNIN_STOP:
        return None
    return core
# 纯单位/表头的"标题"（LLaMA 碳排表的 "36 MWh" 会被 _SECTION_RE 当成章节）
_UNIT_WORDS = frozenset({"mwh", "kwh", "wh", "mw", "kw", "gb", "tb", "ms", "kg", "tonnes", "tons"})


# 表格单元格 / 公式片段冒充标题：带括号、等号、绝对值、比例符号，或干脆没有英文单词
_TABLE_TITLE_RE = re.compile(r"[()=|%×≥≤∝∂]|ℒ|𝑃|𝑁")
_HAS_WORD_RE = re.compile(r"[a-z]{3,}")
# 题注不是标题："Table 4: Fine-tuning …"、"Figure 1.12 Multiple images …"
_CAPTION_TITLE_RE = re.compile(r"(?i)^(table|figure|fig\.|algorithm|listing)\s*\d")


def _title_ok(t: str) -> bool:
    """标题得像标题：长度 3–80、不是"36 MWh"这种数量+单位、也不是表格单元格/公式片段。"""
    if not 3 <= len(t) <= 80:
        return False
    words = t.split()
    if len(words) == 1 and words[0].lower() in _UNIT_WORDS:
        return False
    if _CAPTION_TITLE_RE.match(t):
        return False
    if _TABLE_TITLE_RE.search(t):
        return False
    if _HAS_WORD_RE.search(t):
        return True
    # ICLR/ICML 爱用全大写标题（"INTRODUCTION"、"AREN'T EXISTING SOLUTIONS GOOD ENOUGH?"）
    letters = "".join(ch for ch in t if ch.isalpha())
    return len(letters) >= 4 and letters.isupper()


def _left_margins(lines: list[dict], body: float) -> list[float]:
    """正文左边界，可能有多个：单栏论文回来一档，双栏（CLIP/Codex）回来两档。

    不能直接取 min(x)：有的 PDF 会冒出一两条 x≈10 的怪行（GPT-3 就有），
    一取最小值整个判定就全歪了。所以按整数 x 聚类，只取"够常见"的几档。
    """
    xs = sorted(ln["x"] for ln in lines if ln["size"] >= body * 0.95 and len(ln["text"]) >= 40)
    if not xs:
        return [0.0]
    counts: dict[int, int] = {}
    for x in xs:
        k = round(x)
        counts[k] = counts.get(k, 0) + 1
    need = max(3, int(len(xs) * 0.03))
    common = sorted(k for k, c in counts.items() if c >= need)
    if not common:
        return [float(xs[0])]
    # 两栏之间会撒一些短行（题注/公式），间隔 < 40px 的并成一档
    merged: list[int] = []
    for k in common:
        if merged and k - merged[-1] < 40:
            continue
        merged.append(k)
    return [float(k) for k in merged[:3]]


def _left_margin(lines: list[dict], body: float) -> float:
    """最左边的正文边界（旧接口，保留给只需要一档的地方）。"""
    return _left_margins(lines, body)[0]


def _num_ok(number: str) -> bool:
    """编号得像章节：每段 ≤2 位，首段 1–40。

    表格里的 "68.0"、"356"（LLaMA 的碳排表）靠这一条排除。
    """
    parts = number.split(".")
    if not 1 <= len(parts) <= 3:
        return False
    if not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):  # noqa: SIM103
        return False
    return 1 <= int(parts[0]) <= 40


def _number_follows(number: str, last: dict[int, int]) -> bool:
    """编号连续性：一级 1、2、3…；二级必须挂在最近的一级下面且末位递增。

    加粗的纯编号行（字号等于正文）只有满足连续性才认，避免正文里的 "11"、"68.0" 被当标题。
    """
    parts = [int(p) for p in number.split(".")]
    if len(parts) == 1:
        prev = last.get(1)
        return prev is None or parts[0] == prev + 1
    if last.get(1) != parts[0]:
        return False
    if len(parts) == 2:
        prev = last.get(2)
        return prev is None or parts[1] == prev + 1
    if last.get(2) != parts[1]:
        return False
    prev = last.get(3)
    return prev is None or parts[2] == prev + 1


def _number_bump(number: str, last: dict[int, int]) -> None:
    """记住这个编号，供下一条做连续性判断；上层换了就把更深的层清空。"""
    parts = [int(p) for p in number.split(".")]
    last[1] = parts[0]
    if len(parts) >= 2:
        last[2] = parts[1]
    else:
        last.pop(2, None)
    if len(parts) >= 3:
        last[3] = parts[2]
    else:
        last.pop(3, None)


def _sections(lines: list[dict], body: float) -> list[dict]:
    """章节树：支持两种版式——
    1. 编号与标题同一行："3.2 Model Architecture"；
    2. 编号单独一行："3.2" 换行 "Model Architecture"（本批 PDF 常见）。
    """
    out: list[dict] = []
    prev_number = ""
    prev_x = 0.0
    lefts = _left_margins(lines, body)
    left = lefts[0]

    def near_left(x: float) -> bool:
        """是不是贴着某一栏的正文左边（双栏论文有左右两档）。"""
        return any(abs(x - m) <= 8 for m in lefts)

    last: dict[int, int] = {}          # level -> 最近一次的编号数字
    toc_page = -1
    in_toc = False                     # 目录标题之后、真正标题之前的那一坨都跳过
    seen_refs = False                  # 进了参考文献之后不再抽 run-in 小标题（那里全是正文条目）
    for ln in lines:
        t = ln["text"].strip()
        if ln["page"] != toc_page:     # 目录只影响它所在那一页
            toc_page = ln["page"]
            in_toc = False
        if not seen_refs and t.lower() in ("references", "bibliography"):
            seen_refs = True
        if _TOC_TITLE_RE.match(t):
            in_toc = True
            prev_number = ""
            continue
        if in_toc:
            if ln["size"] >= body * 1.05:   # 字号回到标题级，说明真正文开始了
                in_toc = False
            else:
                prev_number = ""
                continue
        if ln["size"] < body * 0.95 or _TOC_LEADER_RE.search(t):   # 页脚、脚注、目录行
            prev_number = ""
            continue

        m = _SECTION_RE.match(t)
        # 同一行的编号 + 标题必须是「标题样式」（字号更大或加粗）：
        # 双栏论文的正文行也常以数字开头（LLaMA2 的 "4 To complement the human evaluation, we"），
        # 不加这条会被当成一级标题。
        if (
            m
            and _num_ok(m.group(1))
            and _title_ok(m.group(2).strip())
            and (ln["size"] >= body * 1.05 or ln.get("bold"))
        ):
            num = m.group(1)
            out.append({"number": num, "title": m.group(2).strip(),
                        "level": num.count(".") + 1, "page": ln["page"]})
            _number_bump(num, last)
            prev_number = ""
            continue

        n = _NUMBER_ONLY_RE.match(t)
        # 二级/三级标题常和正文同字号、只是加粗（Attention 论文的 "3.2.2" 就单独一行、
        # 字号 10.0 = 正文），所以除了"字号明显更大"，加粗的纯编号行也认作标题编号。
        if n:
            num = n.group(1)
            big = ln["size"] >= body * 1.05
            if big:
                # 大字号也要挡住表格里的数字（LLaMA 碳排表是 "[356] MWh" 这种）
                if _num_ok(num):
                    prev_number = num
                    prev_x = ln["x"]
                continue
            # 加粗但同字号：要求编号合理、左对齐、且编号连续，三重条件才认
            if (
                ln.get("bold")
                and _num_ok(num)
                and near_left(ln["x"])
                and _number_follows(num, last)
            ):
                prev_number = num
                prev_x = ln["x"]
                continue

        # 上一行是纯编号，这一行就是标题（标题可能相对编号缩进几十像素）
        if prev_number and _title_ok(t) and (t[:1].isupper() or ln.get("bold")):
            indented = left - 4 <= ln["x"] <= prev_x + 46
            # 标题也得贴着某一栏的左边：表格单元格里的 "Switch-Base" 缩在中间，不算标题
            column_ok = near_left(ln["x"]) or ln["x"] <= left + 60
            if column_ok and (indented or ln["size"] >= body * 1.05):
                out.append({"number": prev_number, "title": t,
                            "level": prev_number.count(".") + 1, "page": ln["page"]})
                _number_bump(prev_number, last)
                prev_number = ""
                continue
            prev_number = ""    # 编号行下跟了一段缩进正文，说明不是标题
            continue

        # ICLR/ACL 的 run-in 小标题：加粗的 "Datasets." 后面直接接正文，没有编号
        if out and not seen_refs and ln.get("bold") and near_left(ln["x"]):
            runin = _runin_title(t)
            if runin:
                out.append({"number": "", "title": runin, "level": 2, "page": ln["page"]})
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
