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
# 人名：首字母大写的单词串联。
# 上限放到 5 是为了 "Henrique Ponde de Oliveira Pinto"（Codex）这种西语全名；
# 原来的上限 3 会把这一条切成 "Henrique Ponde" + "Oliveira Pinto" 两个人。
# 中间的小写虚词（de / van / von / del / der / la / le）是姓名的一部分，要允许。
# 名字里允许带重音字母（María / François / Gérard / ˇSaˇsko），
# 只认 ASCII 会把 BLOOM 这类多国作者表里的名字漏掉。
# 首词必须是大写字母开头，后续词也以大写开头——小写虚词见 _NAME_LINK。
# **不能吃掉数字**：所以不用 \w（含数字），只列字母和姓名里真会出现的符号。
_NAME_PART = r"[^\W\d_a-z](?:[^\W\d_.\-'’]|\.|\-|'|’)*"
_NAME_LINK = r"(?:de|van|von|del|der|la|le|da|di|dos|das|bin|al)"
_NAME_RE = re.compile(rf"^{_NAME_PART}(?: {_NAME_LINK} {_NAME_PART}| {_NAME_PART}){{1,5}}$")
# 合著论文的作者块常写成 "Alec Radford * 1 Jong Wook Kim * 1 Chris Hallacy 1 ..."：
# 姓名之间靠数字上标和 * / † / ∗ 分隔，没有逗号。所以人名后面允许跟标记。
_NAME_TOKEN = rf"{_NAME_PART}(?: {_NAME_LINK} {_NAME_PART}| {_NAME_PART}){{1,5}}"
_NAME_LOOSE_RE = re.compile(rf"^{_NAME_TOKEN}$")
# 连字：Raffel 里常是 "Raﬀel"（ﬀ = U+FB00），不同字体抽出来不一样，先归一
_LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st", "\u00ad": "",
}
# 脚注符号 + 上标编号：出现在名字首尾，不是名字的一部分
_MARK_CHARS = r"\s*†‡∗⋄§¶,;:.0123456789"
_TRAIL_MARKS = re.compile(rf"[{_MARK_CHARS}]+$")
_LEAD_MARKS = re.compile(rf"^[{_MARK_CHARS}]+")
# 作者可能直接写成机构名（GPT-4 的 "OpenAI∗"、DeepSeek 的 "DeepSeek-AI"）
# 这些词单独出现时是作者，跟着别的词（"Google Brain"）时才是机构。
# 这些机构在论文首页把自己当作"作者"署名，而不是"所属单位"。
# 注意：Google Brain / Microsoft Research / Google Research 之类是**单位**，
# 不能进这张表——它们的行紧跟在真人名后面，进了就会混进作者列表。
_ORG_AUTHOR_RE = re.compile(
    r"^(?:OpenAI|DeepSeek-AI|DeepSeek|Anthropic|Meta AI|Meta|Llama Team|Llama Team, AI"
    r"|Qwen Team|Qwen|Alibaba Group|Google DeepMind|DeepMind)$"
)
# 大团队论文（BLOOM 这种 60+ 作者）把作者按贡献分组，每个组前面是一个小标题：
#   "BigScience Workshop∗" / "Major Contributors" / "Dataset" / "Tokenization"
# 这些行的字形和真人名一模一样（两个首字母大写的词），姓名正则拦不住，
# 只能靠词表。下面这些词出现在名字里，说明它是分组标题而不是人名。
_AUTHOR_GROUP_WORDS = frozenset(
    """
    workshop contributors contributor team group members member authors author
    participants participant reviewers reviewer advisors advisor leads lead
    dataset tokenization tokenisation modeling modelling evaluation prompt
    engineering contributors-list core maintainers editors
    """.split()
)


def _is_author_group_heading(text: str) -> bool:
    """这是贡献分组小标题（"Major Contributors" / "BigScience Workshop∗"），不是人名。

    判定是"含任一分组词"，不是"整行都是分组词"——BLOOM 写的是
    "BigScience Workshop∗"（机构名 + 分组词），后半截就够定性了。
    真人名不会包含 contributor / workshop / dataset 这类词。
    """
    words = re.findall(r"[A-Za-z]+", _norm(text).lower())
    return any(w in _AUTHOR_GROUP_WORDS for w in words)
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


def _strip_marks(text: str) -> str:
    """把连字归一（Raﬀel → Raffel），其它字符原样保留。"""
    out = []
    for ch in text:
        out.append(_LIGATURES.get(ch, ch))
    return "".join(out)


def _split_author_block(text: str) -> list[str]:
    """把一段作者块切成一个个名字。

    作者块有四种写法，都要吃得下：
    1. 逗号分隔：``Ashish Vaswani, Noam Shazeer``
    2. 空格 + 上标数字分隔（LaTeX 模板最常见）::

           Alec Radford * 1 Jong Wook Kim * 1 Chris Hallacy 1 ...

    3. 名字和编号粘连：``Haotian Liu1∗, Chunyuan Li2∗``
    4. 连逗号都没有：``Bo Peng1,2∗Eric Alcaide2,3,4∗``

    做法：先按逗号/分号切段，再用"姓 + 名"正则从每段里逐个抽出名字。
    因为正则允许名字中间有空格，``Radford * 1 Jong Wook Kim`` 会先匹配到
    ``Radford``（后面 ``*`` 断了），再匹配到 ``Jong Wook Kim``，正好切开。
    """
    s = _strip_marks(text)
    out: list[str] = []
    for chunk in re.split(r"[,;，、]", s):
        chunk = chunk.strip()
        if not chunk:
            continue
        pos = 0
        token_re = re.compile(_NAME_TOKEN)
        while pos < len(chunk):
            m = token_re.search(chunk, pos)
            if not m:
                break
            name = m.group(0).strip()
            if _NAME_LOOSE_RE.match(name):
                out.append(name)
            pos = m.end()
    return out


def _title_bottom(first_page: list[dict], title: str, body: float) -> float:
    """标题块的底部纵坐标（跨行标题取最后一行的 y + 行高）。

    为什么需要：``_title`` 把跨行标题用空格拼成一句，作者区如果从"包含标题
    文字的任意一行"开始算，第二行标题（如 "TRANSFORMERS FOR IMAGE
    RECOGNITION AT SCALE"）会落进作者区，被姓名规则切成假名。

    做法和 ``_title`` 保持一致：先按"字号 ≥ 正文 1.3 倍"筛出候选行，
    再取最上面那行往下 ``size * 2.2`` 的带状区域，区域里最靠下的一行的底部
    就是标题块底部。
    """
    if not title:
        return 0.0
    cand = [ln for ln in first_page if ln["size"] >= body * 1.3 and len(ln["text"]) >= 8]
    if not cand:
        return 0.0
    top = min(ln["y"] for ln in cand)
    band = [ln for ln in cand if ln["y"] - top <= ln["size"] * 2.2]
    last = max(band, key=lambda ln: ln["y"])
    return last["y"] + last["size"]


def _looks_like_body_line(text: str, size: float, body: float) -> bool:
    """像正文行：正文字号 + 有连续的英文小写单词（人名里不会出现）。"""
    if size > body * 1.05:
        return False
    t = _norm(text)
    if len(t) < 45:
        return False
    # 连续两个以上小写单词（≥3 字母）——人名是首字母大写，机构名也不会有句子
    return bool(re.search(r"\b[a-z]{3,}\s+[a-z]{3,}\b", t))


def _is_author_zone_stop(text: str, size: float, body: float) -> bool:
    """作者区到哪儿为止。

    作者区在标题和 Abstract/正文之间，短行密集。终止条件：
    1. 遇到 "Abstract" / "1 Introduction" 标题
    2. 遇到像正文的行（见 _looks_like_body_line）

    LLaMA 3 那种没有 "Abstract" 标题的模板（正文段落直接从标题下方开始），
    靠条件 2 拦截；否则整段摘要都会被当成作者区，切出 "Llama Guard" 这种假名。
    """
    t = _norm(text)
    low = t.lower()
    if low.startswith("abstract") or low == "introduction":
        return True
    if re.match(r"^1\.?\s+introduction\b", low):
        return True
    return _looks_like_body_line(t, size, body)


def _is_name_continuation(prev_line: str, next_word: str) -> bool:
    """上一行末尾被断开的名字，下一行开头是不是它的续接。

    只认两种**有版面证据**的断行，不猜：
    1. 硬连字符断词：``Joydeep Bhattachar-`` + ``jee`` → ``Joydeep Bhattacharjee``
       要求上一行以 ``-`` 结尾、下一行开头是小写字母。
    2. 未写完的姓名片段：``..., Alexander M.`` 换行 ``Rush, Stella ...``
       上一行最后一段是"首字母大写串 + 可选的单个大写字母缩写"（如 ``Alexander M.``），
       且它整体不过姓名正则（说明只有 1~2 个词，还没写完），下一行以一个大写词开头。
    """
    prev_line = prev_line.rstrip()
    if not prev_line:
        return False
    if prev_line.endswith("-") and next_word[:1].islower():
        return True
    tail = re.split(r",\s*", prev_line)[-1].strip(" ∗*†‡,")
    if not tail or not next_word[:1].isupper():
        return False
    # "Alexander M."：行尾是单字母缩写，说明姓被换行断到下一行了
    if not _looks_unfinished(tail):
        return False
    return bool(_NAME_RE.match(f"{tail} {next_word}"))


def _join_broken_names(text: str) -> str:
    """把被硬连字符断开的词接回去：``Bhattachar-`` 换行 ``jee`` → ``Bhattacharjee``。

    只处理行尾 ``-`` 且下一个词全小写的情况；行内正常的连字符名字
    （``Herbert-Voss``、``Gonzalez-Dios``）不受影响，因为它们不在行尾。
    """
    return re.sub(r"([A-Za-z])-\s+([a-z]{2,})", r"\1\2", text)


def _looks_unfinished(name: str) -> bool:
    """这个名字是不是还没写完（只是行尾的半截）。

    判据只有一条，且是版面事实而不是猜：**最后一个词是单字母缩写**
    （``Alexander M.``）——缩写后面一定还有姓，只是被换行断开了。
    完整的写法是 ``Christopher D. Manning`` 这种"缩写 + 词"，最后一个是词。

    以连字符结尾的（``Joydeep Bhattachar-``）也属于没写完。
    """
    name = name.strip()
    if name.endswith("-"):
        return True
    last = name.split()[-1] if name.split() else ""
    return bool(re.fullmatch(r"[A-Za-z]\.?", last))


def _authors(first_page: list[dict], title_y: float, body: float) -> list[str]:
    """作者 = 标题下方、Abstract 之前，长得像人名的行（按 y 分行、按 x 排序）。"""
    names: list[str] = []
    # 上一行末尾被断开的那一小段（如 "Alexander M."），等下一行来了再决定接不接
    carry = ""
    for ln in sorted(first_page, key=lambda x: x["y"]):
        # 连字要先归一："Colin Raﬀel" 里的 ﬀ（U+FB00）过不了 _NAME_RE，
        # 不先替换就会只抽到 "Colin Ra"
        t = _strip_marks(ln["text"].strip())
        if ln["y"] <= title_y or ln["y"] - title_y > 260:
            continue
        if not t:
            continue
        if carry:
            # 上一行留下的半截名字，看这一行的开头能不能接上
            words = _LEAD_MARKS.sub("", t).split()
            head = words[0].strip(" ∗*†‡,.-") if words else ""
            if head and _is_name_continuation(carry, head):
                if carry.endswith("-"):
                    # 硬连字符断词：把连字符去掉直接拼
                    carry = carry[:-1] + head
                else:
                    carry = f"{carry} {head}"
                # 去掉已经吃进 carry 的那个词，剩下的接在后面
                rest = t[len(words[0]):].lstrip(" ,;") if words else ""
                t = f"{carry}, {rest}" if rest else carry
            carry = ""
        if _is_author_zone_stop(t, ln["size"], body):
            break
        if "@" in t:
            # "Llama Team, AI @ Meta1" 这种署名里带 @，但前半段是人名/团队名
            head = re.split(r"@", t)[0].strip(" ,;")
            if head and _ORG_AUTHOR_RE.match(_TRAIL_MARKS.sub("", head)):
                names.append(_ORG_AUTHOR_RE.match(_TRAIL_MARKS.sub("", head)).group(0))
            continue
        # 整行就是一个机构署名（"OpenAI∗" / "DeepSeek-AI"）时保留。
        # 必须放在人名分支之前：这类行既没有逗号、也过不了 _NAME_RE，
        # 掉进 _split_author_block 会返回空列表，作者就丢了。
        org = _ORG_AUTHOR_RE.match(_TRAIL_MARKS.sub("", t).strip())
        if org:
            names.append(org.group(0))
            continue
        # 贡献分组小标题（"BigScience Workshop∗" / "Major Contributors"）不是人名。
        # 必须放在机构署名分支**之后**：Qwen Team / Llama Team 既有 "team"
        # 这种分组词，又确实是论文作者署名，得让机构分支先认领。
        if _is_author_group_heading(t):
            continue
        low = t.lower()
        if any(w in low for w in _AFFIL_WORDS):
            # 机构行（"Tsinghua University"、"Google Brain"）跳过
            continue
        # 一行多个作者（"Ashish Vaswani, Noam Shazeer"）按逗号拆
        parts = [p.strip(" ∗*†‡,") for p in re.split(r",\s*", t)]
        hits = [p for p in parts if _NAME_RE.match(p)]
        if hits:
            tail = parts[-1] if parts else ""
            # 最后一段没写完（"Alexander M." / "Joydeep Bhattachar-"），
            # 留给下一行接，不单独成条
            if len(hits) == len(parts) and not (tail == hits[-1] and _looks_unfinished(tail)):
                names.extend(hits)
            else:
                complete = hits[:-1] if hits[-1] == tail else hits
                names.extend(complete)
                if hits[-1] == tail:
                    carry = tail
            continue
        # 逗号拆不开时（LaTeX 作者块），改用姓名边界逐个抽。
        # 只对短行做——长行多半是正文段落，硬切会切出 "Large Language" 这种假名
        if len(t) <= 200:
            names.extend(_split_author_block(t))
    if carry:
        names.append(carry)
    # 行内硬连字符断词接回去（"Bhattachar- jee" → "Bhattacharjee"）
    names = [_join_broken_names(n) for n in names]
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


_CAPTION_LEAD = "|｜—–-:："


def _caption_text(raw: str) -> str:
    """去掉题注开头的竖线/破折号之类分隔符：'| Our model' -> 'Our model'。"""
    return raw.strip().lstrip(_CAPTION_LEAD).strip()


def _looks_like_caption(text: str) -> bool:
    """区分「题注」和「正文里提到图表的那句话」。

    正文引用长这样（实测 LayerNorm 第 7 页）：
        "Figure 1 illustrates the validation curves of the models, …"
    题注长这样：
        "Figure 1: Recall@K curves using order-embeddings with and without …"

    最省事又站得住的判据：**题注是名词短语，首词大写；正文引用是句子成分，
    首词是小写动词/连词**（shows / illustrates / from the paper / and Table 4 …）。
    实测 39 篇、624 条题注里，55 条误判的首词全是小写，没有一条真题注是小写开头。
    另外题注一般不会超过 200 字（实测最长 127 字）。
    """
    t = _caption_text(text)
    if len(t) < 4 or len(t) > 200:
        return False
    first = t[0]
    if first.islower():
        return False
    return first.isupper() or first.isdigit() or first in "('\"（“［["


def _captions(lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """表格/图表题注：返回 (tables, figures)，每项 {number, caption, page}。

    正则只负责"以 Table/Figure + 编号开头"，是不是真题注交给 `_looks_like_caption`：
    正文里 "Figure 1 illustrates …" 这种句子也会匹配正则，不能当题注收进来。
    """
    tables: list[dict] = []
    figures: list[dict] = []
    for ln in lines:
        t = ln["text"]
        m = _TABLE_CAPTION_RE.match(t)
        if m:
            caption = _caption_text(m.group(2))
            if _looks_like_caption(caption):
                tables.append({"number": m.group(1), "caption": caption, "page": ln["page"]})
            continue
        m = _FIGURE_CAPTION_RE.match(t)
        if m:
            caption = _caption_text(m.group(2))
            if _looks_like_caption(caption):
                figures.append({"number": m.group(1), "caption": caption, "page": ln["page"]})
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


# 参考文献章节标题：论文惯例是 References，整本书（如 Understanding Deep Learning）用 Bibliography。
# 这个集合必须和 :628 的章节识别保持一致，否则会出现「章节树认出来了、参考文献数却是 0」。
_REF_TITLES = ("references", "bibliography")


def _is_ref_title(text: str) -> bool:
    return _norm(text).lower() in _REF_TITLES


def _is_toc_line(lines: list[dict], i: int) -> bool:
    """判断 lines[i] 是正文标题，还是目录里的同名条目。

    目录条目的特征是「标题行 + 紧跟一行纯页码」或「标题行带引导点」。
    注意不能看**上一行**是不是纯页码：`_lines` 按 PDF 块顺序返回，
    页脚页码常常紧挨在正文标题上方（1607.06450v1 第 10 页就是这样），
    拿它当判据会把每一页开头的标题都误判成目录。
    """
    text = lines[i]["text"]
    if _TOC_LEADER_RE.search(text):
        return True
    if i + 1 < len(lines):
        nxt = lines[i + 1]["text"].strip()
        if re.fullmatch(r"\d{1,4}", nxt):     # 目录里页码单独成行，紧跟标题
            return True
    return False


def _references(lines: list[dict]) -> dict:
    """参考文献：从 References / Bibliography 标题后开始数条目。

    两种常见格式都支持：
    1. 编号式："[1] Jimmy Lei Ba, Jamie Ryan Kiros..."
    2. 作者-年份式（LLaMA 3、Llama 2 等）："Amro Abbas, Kushal Tirumala..."

    取**最后一个**非目录命中：整本书的目录里往往也有 Bibliography 条目，
    而正文的参考文献在最后。论文通常只有一个，取最后一个不影响。
    """
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if _is_ref_title(lines[i]["text"]) and not _is_toc_line(lines, i):
            start = i
            break
    if start is None:
        return {"page": None, "count": 0, "first": "", "last": ""}
    entries: list[str] = []
    # 有些模板（ACL/NeurIPS 的 [ACDE12] 这类 key 式编号）把编号单独放一行，
    # 条目正文在下一行。这时剥掉编号会得到空串，要把下一行接上。
    pending_key = False
    for ln in lines[start + 1:]:
        t = ln["text"].strip()
        if _norm(t).lower() in ("appendix", "appendices", "a appendix"):
            break
        if _is_ref_entry(t):
            body = re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", t)
            if body:
                entries.append(body)
                pending_key = False
            else:
                pending_key = True          # 编号独占一行，正文在下一行
        elif pending_key:
            entries.append(t)
            pending_key = False
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


def _find_references(doc, head: list[dict], pages: int, first_pass: int = 25, limit: int | None = None) -> dict:
    """参考文献常在正文之后才出现，所以先扫已读行，没有就逐页往后找。

    limit 默认扫到最后一页：早先写死 90 页，导致 541 页的书（参考文献在第 525 页）
    永远返回 0 条。按页扫只在前面没命中时触发，代价是每页一次 get_text。

    first_pass 不能是硬下限：短论文（14 页）的参考文献在第 10 页，
    而 `head` 只覆盖 max_pages 页，写死 `range(25, pages)` 在短文档上是空循环。
    这里直接接在 `head` 已覆盖的页之后继续扫，`first_pass` 只作为下限兜底。
    """
    refs = _references(head)
    stop = pages if limit is None else min(limit, pages)
    scanned = (max((ln["page"] for ln in head), default=-1) + 1) if head else 0
    start = min(scanned if scanned > 0 else first_pass, stop)
    extra: list[dict] = []
    for pno in range(start, stop):
        extra += _lines(doc[pno], pno)
        hit = _references(extra)
        # 只要找到标题就记下来：条目正则可能认不全某本冷门排版，
        # 但「参考文献在第几页」这个信息仍然有价值，不该退化成 None。
        if hit["page"] is not None:
            refs = hit
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
        # 标题可能跨多行（"AN IMAGE IS WORTH 16X16 WORDS:" + "TRANSFORMERS FOR ..."）。
        # 作者区必须从标题**最后一行**之下开始，否则第二行标题会被当成作者行，
        # 切出 "TRANSFORMERS FOR IMAGE RECOGNITION" 这种假名。
        title_y = _title_bottom(first, title, body)
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
