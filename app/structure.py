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
from pathlib import Path

# "3.2 Model Architecture" / "1 Introduction" 这类章节标题
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9 ,:\-]{2,70})$")
# 附录章节：字母编号 "A Appendix" / "A.1 Proof of ..." / "B.5 Comparison"。
# 老口径只认数字编号，实测 10 篇有附录的论文里只认出 3 篇——
# 不是因为附录不存在，而是 `A` / `B.5` 这种编号压根进不了 `_SECTION_RE`。
# 单独开一条正则而不是放宽 `_SECTION_RE`：后者还用于判断
# "这一行是不是章节标题"，放宽后 "As shown in Table 2" 这种正文也会命中。
_APPENDIX_SECTION_RE = re.compile(r"^([A-Z](?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9 ,:\-]{2,70})$")
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
# 强数学信号：真公式里才常见的符号/写法。_MATH_RE 太宽松（任何带等号的正文行都过），
# 只用于粗筛；判断"是不是公式"用这一条。
_MATH_STRONG_RE = re.compile(
    r"[∑∏∫√∞∂∇≈≠≤≥±∈∉⊂⊆∪∩∀∃Θθαβγδεζηλμνξπρστυφχψω"     # 运算符 + 希腊字母
    r"ΓΔΛΞΠΣΦΨΩ]"                                          # 大写希腊字母
    r"|\^|_\{|\\frac|\\sum|\\int"                           # 上下标 / LaTeX 原样残留
)
# 纯数学符号（不含任何英文字母）：像 "√dk"、"θ"、"∈Rd" 这种短行不靠上面的关键词也算公式。
# 单独看"有没有英文字母"是关键——"r = 1" 里有 r，属于正文小等式，不算。
_MATH_ONLY_SYMBOLS = re.compile(r"[∑∏∫√∞∂∇≈≠≤≥±∈∉⊂⊆∪∩∀∃]")
# "名字(参数…) = 表达式" 形状的函数式：FFN(x) = max(...)、MultiHead(Q, K, V) = Concat(...)。
# 括号里允许出现一层嵌套括号——"max(0, xW1 + b1)" 这种参数里套函数的情况很常见，
# 不允许嵌套会把 Attention 论文里的 FFN 公式整条漏掉。
# `[^()=]` 而不是 `[^()]`：要排除 "x = f(a)" 这种正文行（等号出现在括号前面）。
_FUNC_CALL_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]{0,15}\("
    r"(?:[^()=]|\([^()=]*\))*"
    r"\)\s*="
)
# LaTeX 里常见的三角/对数函数名，单独出现在赋值的右边也算强信号
_MATH_FUNC_WORD = re.compile(
    r"\b(?:sin|cos|tan|exp|log|softmax|sigmoid|relu|tanh|sqrt|argmax|argmin|max|min|Concat)\b"
)


def _is_formula_line(t: str) -> bool:
    """判断一行是不是公式（宁缺毋滥，但别把真公式漏掉）。

    通过条件（任一）：
      1. 有强数学信号（希腊字母 / √∑∫ / 上下标）；
      2. 含纯数学符号、且**整行没有英文字母**（"√dk"、"θ"）；
      3. 形如 "名字(参数) = 表达式"（含 sin/softmax/Concat 这类函数名 + 括号）。
    拒绝条件：
      - 英文单词 ≥ 8 个（成句正文）；
      - 长度 > 160。
    """
    if not (2 <= len(t) <= 160):
        return False
    if len(re.findall(r"[A-Za-z]{2,}", t)) >= 8:
        return False
    if _MATH_STRONG_RE.search(t):
        return True
    if _MATH_ONLY_SYMBOLS.search(t) and not re.search(r"[A-Za-z]", t):
        return True
    # "PE(pos,2i) = sin(pos/10000)"：左边是函数式，右边出现数学函数名
    if "=" in t and _FUNC_CALL_RE.match(t):
        rhs = t.split("=", 1)[1]
        # "PE(pos,2i) = sin(pos/10000)"：右边出现数学函数名
        if _MATH_FUNC_WORD.search(rhs):
            return True
        # "FFN(x) = max(0, xW1 + b1)W2 + b2"：右边含运算符
        return bool(re.search(r"[+\-*/^×·⋅∑√]|_\{", rhs))
    return False
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
                    "w": max(s.get("bbox", (0, 0, 0, 0))[2] for s in spans) - min(s.get("bbox", (0, 0, 0, 0))[0] for s in spans),
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
    """单位/机构：标题下方带机构关键词的行（去重、保序、剔邮箱、剔正文段落）。"""
    out: list[str] = []
    for ln in first_page:
        if ln["y"] <= title_y or ln["y"] - title_y > 300:
            continue
        text = ln["text"].strip()
        # 邮箱行不能进单位列表：老口径靠"含 @ 的机构词"混进来 61 条邮箱，
        # 赛题要的是"单位"，邮箱另有 emails 字段。
        if "@" in text:
            continue
        # 章节标题（Abstract / 1 Introduction）不是单位。
        # ZeRO 论文里 "Abstract" 下面那段正文含 "researchers have used the system ..."，
        # 因为带 research 这个关键词被当成单位收了进去——必须挡掉整句正文。
        if _is_section_heading_text(text):
            continue
        if len(text) > 60:
            continue
        low = text.lower()
        if any(w in low for w in _AFFIL_WORDS) and len(text) <= 120:
            out.append(text)
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))][:12]


# 脚注里这些词说明不是单位（"†Work performed while at Google Brain"）
_SKIP_AFFIL = re.compile(r"(?i)work\s+performed|equal\s+contribution|listing\s+order|corresponding\s+author")


def _author_affiliations(first_page: list[dict], title_y: float, authors: list[str]) -> dict[str, list[str]]:
    """作者 → 单位/邮箱 的映射（赛题 1.1.2 里"作者含顺序和单位"的"单位"部分）。

    依据是绝大多数论文首页的版式：同一 y 上并排多名作者，**每个作者正下方**
    依次是本人的单位和邮箱。实测 Attention 论文首页：

        y=184.9  Ashish Vaswani* | Noam Shazeer* | ...    ← 同一 y，四列
        y=197.9  Google Brain   | Google Brain  | ...     ← 各自正下方
        y=209.4  avaswani@...   | noam@...      | ...     ← 再下一行是邮箱

    所以做法是：把标题下方、Abstract 之前的行按列（x 聚类）切开，
    在每一列里按 y 向下找，遇到邮箱和机构词就记到该列最上面那个人名下。

    映射不上就不写这个键（而不是给空列表），这样消费方能区分"没有单位信息"和"有但为空"。
    单栏排版的论文（作者一行、单位紧跟一行）也适用：那一列里只有一个人名，
    后续的单位行全部归他——虽然不精确，但比"完全没有映射"对检索友好。
    """
    if not authors:
        return {}
    # 作者行里常把多人挤在一行（"Peter Albert Amjad Almahairi Yasmine Babaei"），
    # 直接拿整行去比对一个人都对不上。这里只按"整行原样"和"逗号切开"两种粒度比对，
    # 不再猜名字边界——试过按词长切分，会把 "Henrique Ponde de Oliveira Pinto"
    # 切成 "Oliveira Pinto"，等于用一个更糟的错误换一个错误。
    want_loose: dict[str, str] = {a: a for a in authors}
    if title_y <= 0:
        return {}
    # 作者区在标题下方；Abstract 之前的那几行才是"作者+单位"块。
    # 上限 260pt 与 `_authors()` 保持一致，再放宽会把 Abstract 正文第一行也框进来。
    band = [ln for ln in first_page if title_y < ln["y"] <= title_y + 260]
    if not band:
        return {}
    # 章节标题（Abstract / 1 Introduction）不是单位，先剔掉——否则每个名字下面都会挂一个 "Abstract"
    band = [ln for ln in band if not _is_section_heading_text(ln["text"])]

    # 按 x 聚类成列：同一作者的 x 浮动超过 20pt 是常事（"Google Brain" 与邮箱居中宽度不同），
    # 但不同作者之间至少要隔开 60pt（Attention 论文首页四列相距 107pt）。
    # 用"与上一列**最右边界**的距离"判断，避免列内长单位行把相邻作者吸进来。
    #
    # 特例：论文首页上半部分常常是"一行并排三个作者、下面整块是全体单位清单"
    # （Llama 2 就是 `Hugo Touvron | Louis Martin | Kevin Stone` 之后直接 `GenAI, Meta`）。
    # 遇到这种整行很宽（> 半页）的行，说明它不是"某个作者正下方"的私有单位，
    # 而是全体共享，按单栏处理：所有暂未拿到单位的作者都记上这一条。
    cluster_gap = 60
    band.sort(key=lambda ln: ln["x"])
    cols: list[list[dict]] = []
    col_right = 0.0
    for ln in band:
        w = ln.get("w", 0.0)
        if cols and ln["x"] - col_right <= cluster_gap and ln["x"] - cols[-1][0]["x"] <= cluster_gap:
            cols[-1].append(ln)
            col_right = max(col_right, ln["x"] + w)
        else:
            cols.append([ln])
            col_right = ln["x"] + w

    skip = _SKIP_AFFIL.findall(" ".join(ln["text"] for ln in first_page))
    shared_wide = max((ln.get("w", 0.0) for ln in band), default=0.0) > 260

    out: dict[str, list[str]] = {}
    pending: list[str] = []          # 见过面但还没拿到单位的作者（等共享单位清单）
    for col in cols:
        col.sort(key=lambda ln: ln["y"])
        # 这一列最上面那个人名就是"列主"
        owner = ""
        for ln in col:
            name = _TRAIL_MARKS.sub("", _strip_marks(ln["text"].strip())).strip(" ,;")
            if not owner:
                if name in want_loose:
                    owner = want_loose[name]
                    pending.append(owner)
                continue
            if name in want_loose:
                # 换人了（竖排单位表里常见：作者 1 / 单位 1 / 作者 2 / 单位 2）
                owner = want_loose[name]
                pending.append(owner)
                continue
            text = ln["text"].strip()
            if not text:
                continue
            email = _EMAIL_RE.search(text)
            if email:
                pick = email.group(0)
            elif any(w in text.lower() for w in _AFFIL_WORDS) and len(text) <= 120:
                # "†Work performed while at Google Brain" 这类脚注不是单位
                if any(s and s in text for s in skip):
                    continue
                pick = text
            else:
                continue
            if pick in _SKIP_AFFIL.findall(pick):
                continue
            if pick not in out.setdefault(owner, []):
                out[owner].append(pick)
    # 共享单位清单：首页上半部分有整行宽的单位行时，未命中的人共用这一条
    if shared_wide and pending:
        shared = [
            ln["text"].strip()
            for ln in band
            if ln.get("w", 0.0) > 260
            and any(w in ln["text"].lower() for w in _AFFIL_WORDS)
            and len(ln["text"]) <= 120
            and "@" not in ln["text"]
        ]
        for ln in shared:
            for name in pending:
                if name not in out and _SKIP_AFFIL.search(ln) is None:
                    out[name] = [ln]
    return {k: v[:4] for k, v in out.items() if v}


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


def _is_section_heading_text(t: str) -> bool:
    """这一行是不是章节标题（Abstract / 1 Introduction / A Appendix 之类）。

    作者-单位映射要用：标题下方的行里，"Abstract" 是章节标题不是单位，
    不剔掉的话每个作者名下都会挂一条 "Abstract"。
    """
    core = _TRAIL_MARKS.sub("", _strip_marks(t.strip())).strip(" :：")
    if core in _SPECIAL_SECTIONS:
        return True
    return bool(_SECTION_RE.match(core) or _APPENDIX_RE.match(core))


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
        # 附录的字母编号（"A Appendix" / "B.5 Comparison"）单独认一遍
        if not m:
            am = _APPENDIX_SECTION_RE.match(t)
            # 只有真的像附录标题才收，避免把表格里的 "N Examples of ..." 当章节
            if am and (ln["size"] >= body * 1.05 or ln.get("bold")):
                heading = am.group(2).strip()
                if _APPENDIX_RE.search(heading) or am.group(1).count(".") >= 1:
                    out.append({"number": am.group(1), "title": heading,
                                "level": am.group(1).count(".") + 1, "page": ln["page"]})
                    prev_number = ""
                    continue
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

    # 第二遍：附录编号常被切成两行——"F" 单占一行、下一行才是 "Appendix: Full Prompts"。
    # 第一遍只认识"编号 + 标题同一行"的写法，所以这里补一遍：
    # 纯字母行（A~Z 或 A.1）后面的那一行若像标题，就当成附录章节收进来。
    out = _appendix_sections(lines, out, body)
    return out


def _appendix_sections(lines: list[dict], sections: list[dict], body: float) -> list[dict]:
    """补抽"字母编号单独一行"的附录章节。

    动机（实测）：Chain-of-Thought 论文第 30 页是 `E` 一行、`Additional Details` 一行；
    第 32/35 页是 `F` / `Appendix: Input/Output Examples`。
    这类版本的第一行只有单个字母，`_SECTION_RE` 和 `_APPENDIX_SECTION_RE` 都不会命中，
    于是整篇附录在章节树里彻底消失——赛题 1.1.2 的「附录」项因此只有 3/41 覆盖。
    """
    if not lines:
        return sections
    already = {(s["number"], s["page"]) for s in sections}
    added: list[dict] = []
    for i, ln in enumerate(lines):
        t = ln["text"].strip()
        if not re.fullmatch(r"[A-Z](?:\.\d+)+|[A-Z]", t):
            continue
        if ln["size"] < body * 1.05 and not ln.get("bold"):
            continue
        title = ""
        # 情况一：编号和标题是同一 y 上的两个行对象（"C" + "Extended Related Work"）。
        # 同一行、同一字号、x 只差一个编号宽度——这是最常见的排法。
        for other in lines[i + 1:i + 3]:
            if other["page"] != ln["page"]:
                break
            if abs(other["y"] - ln["y"]) > ln["size"] * 0.8:
                continue
            hop = other["x"] - ln["x"]
            if 0 < hop <= ln["size"] * 4:
                title = _TRAIL_MARKS.sub("", _strip_marks(other["text"].strip())).strip(" :：")
                break
        # 情况二：编号单独一行，标题在下一行（"F" 换行 "Appendix: Full Prompts"）
        if not title:
            for other in lines[i + 1:i + 4]:
                if other["page"] != ln["page"] or other["y"] <= ln["y"]:
                    continue
                if abs(other["x"] - ln["x"]) > 40:
                    continue
                title = _TRAIL_MARKS.sub("", _strip_marks(other["text"].strip())).strip(" :：")
                break
        if not title or len(title) > 70 or not title[:1].isupper():
            continue
        # 标题必须"像标题"：带 Appendix 字样，或 Title Case 短语（排除 "Version Control"
        # 这种编号后紧跟正文小标题的情况——它其实也是附录子节，收下没问题）。
        looks_heading = bool(_APPENDIX_RE.search(title)) or (
            len(title.split()) <= 8 and not title.endswith((".", ",", ";"))
        )
        if not looks_heading:
            continue
        # 单字母编号只在"标题里带 Appendix"或"该字母在文档里成序列"时才认，
        # 否则会把 checklist / 表格里的 A/B/C 编号当附录。
        num = t
        if (num, ln["page"]) in already:
            continue
        already.add((num, ln["page"]))
        added.append({"number": num, "title": title, "level": 1 + num.count("."), "page": ln["page"]})
    if not added:
        return sections
    # 只保留"像附录"的：带 Appendix 字样，或字母编号有兄弟（A、B、C 至少两个）
    letters = {a["number"][0] for a in added if re.fullmatch(r"[A-Z]", a["number"])}
    added = [
        a for a in added
        if _APPENDIX_RE.search(a["title"]) or "." in a["number"] or len(letters) >= 2
    ]
    if not added:
        return sections
    merged = [s for s in sections if (s["number"], s["page"]) not in {(a["number"], a["page"]) for a in added}]
    merged += added
    merged.sort(key=lambda s: (s["page"], s["level"]))
    return merged


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
    """公式行：含**强数学信号**、长度 ≤ 160、且不像成句英文（英文单词少于 8 个）。

    为什么不再用「含等号就算」：旧口径全库抓了 1538 条，抽查只有 46% 真有数学符号，
    另外 53% 是 `i = wl`、`r = 1`、`r = 4` 这种正文小等式或表格数字，
    看着像公式其实不是。改成必须有强信号之后，这些噪声被挡在外面。

    「强信号」= 数学运算符/希腊字母，或出现 softmax/exp/log 这类函数名，
    或带上下标（`W^Q`、`d_{model}`）。单个 `=` / `·` / `≤` 太容易出现在正文里，不算。
    宁可少抓：漏掉一条真公式只是少个条目，混进一堆假公式会让"公式解析"这项直接失分。

    另外去掉了原来写死的 `[:120]` 上限——全篇读之后 120 这条线会把
    FlashAttention / TinyLlama / LoRA 等公式多的论文直接截断。
    """
    out: list[dict] = []
    for ln in lines:
        t = ln["text"].strip()
        if not _is_formula_line(t):
            continue
        out.append({"page": ln["page"], "text": t})
    return out


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


def _figure_shots(pdf_path: str, figures: list[dict], out_dir: str, paper_key: str) -> None:
    """把图表**图片本身**渲染出来存成 PNG，写回 `fig["image"]`。

    赛题 1.1.2 要求解析"图表"，原来只存了题注文字和页码——那只能算"知道有这张图"，
    不能算"解析了这张图"。这里改用整页渲染再按题注上方的区域裁切，原因有两个：

      1. 只有一张图时，原图应该位于题注正上方；整页渲染后裁出"上一行文字到题注"
         之间那一块，得到的就是图本身，不需要识别矢量图还是位图。
      2. 有的图不是位图而是矢量绘制的（Attention 论文第 6 页那张注意力可视化），
         `get_image_info()` 根本看不到它，只有渲染整页才拿得到。

    一页多图的论文（Attention 论文第 3 页的 Figure 2 左右两栏）裁出来会是整栏，
    属于"宁全勿缺"的取舍：宁可多带一点正文，也不能只给个空白框。
    真值判断放在 `_figure_layout()` 里，这里只负责按布局渲染。

    out_dir 为空表示不导出图片（默认），保持纯解析路径不产生任何文件写入。
    """
    if not out_dir:
        return
    try:
        import pymupdf
    except Exception:  # noqa: BLE001
        return
    pages = sorted({f["page"] for f in figures})
    if not pages:
        return
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return
    try:
        with pymupdf.open(pdf_path) as doc:
            for pno in pages:
                if not 0 <= pno < len(doc):
                    continue
                page = doc[pno]
                blocks = [
                    (b[1], b[4].strip())
                    for b in page.get_text("blocks")
                    if b[4].strip()
                ]
                in_page = [f for f in figures if f["page"] == pno]
                for f in in_page:
                    cap_top = _caption_top(blocks, f)
                    bottom = cap_top - 4
                    top = 0.0
                    for by, text in blocks:
                        # 取"离题注最近、且在题注上方、又不是题注本身"的那一行文字
                        if by >= bottom or _FIGURE_CAPTION_RE.match(text):
                            continue
                        top = max(top, by)
                    box = pymupdf.Rect(6, max(top - 2, 0), page.rect.width - 6, bottom)
                    if box.height < 30 or box.width < 30:
                        continue
                    try:
                        pix = page.get_pixmap(clip=box, dpi=110)
                        name = f"{paper_key}_fig{f['number']}.png"
                        pix.save(str(Path(out_dir) / name))
                        f["image"] = name
                    except Exception:  # noqa: BLE001 - 单张失败不影响其它字段
                        continue
    except Exception:  # noqa: BLE001
        return


def _caption_top(blocks: list[tuple[float, str]], fig: dict) -> float:
    """找到这条图表题注在页面上的上边界 y。

    题注可能被 PDF 切成多行，所以按"含 Figure N 的那一行"定位，
    拿不到就退化成整页高度的一半——总比不裁强。
    """
    pat = re.compile(rf"(?i)^\s*(?:figure|fig\.)\s*{re.escape(fig['number'])}\b")
    for by, text in blocks:
        if pat.match(text):
            return by
    return 400.0


def _table_rows_pymupdf(pdf_path: str, pages: list[int]) -> dict[int, list[list[list[str]]]]:
    """备用抽表路径：直接用 PyMuPDF 的 `find_tables().extract()`。

    为什么需要备用：pdfplumber 的 text 策略对"没有横线、只有一两列"的表完全无能，
    实测 LoRA 的表 13（GPT-2 各适配方法对比）、Mistral 的表 7（Elo 评分）
    在 pdfplumber 下一条都抽不出来，但 PyMuPDF 自己的抽表能正确还原。
    全库 21 张"有表格框、pdfplumber 却没抽到行"的表里，这条备用路径救回 8 张。

    只在上一条路径失败时调用，结果仍要过 `_rows_ok` 质量线，
    所以不存在"为了凑数把正文当表"的风险。

    跳过有内嵌图片的页：那多是"示例对话/提示词"式排版，
    PyMuPDF 会把图文混排切成一堆单字符碎片，过不了质量线也会白跑一遍。
    """
    if not pages:
        return {}
    try:
        import pymupdf
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, list[list[list[str]]]] = {}
    try:
        with pymupdf.open(pdf_path) as doc:
            for pno in pages:
                if not 0 <= pno < len(doc):
                    continue
                page = doc[pno]
                if page.get_images(full=True):
                    continue
                best: list[list[str]] = []
                for tab in page.find_tables().tables:
                    try:
                        raw = tab.extract()
                    except Exception:  # noqa: BLE001
                        continue
                    rows = [[_norm_cell(c or "") for c in r] for r in raw if r]
                    rows = [r for r in rows if any(r) and not any(_CAPTION_ROW.match(c) for c in r)]
                    if _rows_ok(rows) and len(rows) > len(best):
                        best = rows
                if best:
                    out[pno] = [best]
    except Exception:  # noqa: BLE001 - 备用路径失败不影响主流程
        return {}
    return out


def _ocr_pages(path: str, pages: int) -> dict[int, str]:
    """读 `data/papers/ocr/<stem>.json` 缓存，返回 {0 基页号: 文字}。

    只读缓存、**绝不触发 OCR**：单页 OCR 实测 8.6 秒（200 DPI / 2 核），
    在请求路径上做等于把接口打挂。要 OCR 请先用 `scripts/ocr_scanned.py` 批处理。
    """
    try:
        from app.ocr import load_cache
    except Exception:  # noqa: BLE001 - 没装 OCR 依赖就当没有缓存
        return {}
    try:
        raw = load_cache(path)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    for key, text in (raw or {}).items():
        try:
            pno = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= pno < pages and str(text).strip():
            out[pno] = str(text)
    return out


def _ocr_lines(path: str, pages: int) -> list[dict]:
    """把 OCR 缓存的纯文字包成 `_lines()` 那套 dict。

    **字号是真的没有**：扫描件里一个字都没有，没有 font size。
    所以给的 size 只是"用来通过 _sections 的字号门槛"的常量，
    不能拿它做任何版式判断（"这是不是大标题"这类判断在扫描件上无意义）。
    """
    body = 10.0
    head = 12.0
    out: list[dict] = []
    for pno in sorted(_ocr_pages(path, pages)):
        for raw in _ocr_pages(path, pages)[pno].split("\n"):
            t = raw.strip()
            if not t:
                continue
            is_head = bool(
                re.fullmatch(r"(?:abstract|references|bibliography|appendix|acknowledg(?:e)?ments?|conclusion)s?[:：]?", t, re.I)
            )
            out.append({
                "text": t,
                "size": head if is_head else body,
                "x": 0.0,
                "y": float(len(out)),
                "bold": is_head,
                "w": 0.0,
                "page": pno,
            })
    return out


# 合并 OCR 的空格断字："AshishVaswani" / "GoogleBrain" / "Was trainedourmodels"。
# 先补函数词（最安全的锚），再补常见复合词，最后按固定轮数补"小写紧跟大写"的驼峰边界。
# 只补"小写 + 大写"这一种：扫描件的真粘名词几乎都是这个形状，
# 而 "Attention Is All You Need" 这种真词之间本来就有空格，不会被误伤。
_PHRASES_EN: tuple[str, ...] = (
    "the", "of", "and", "in", "to", "on", "is", "are", "was", "were", "be", "been",
    "for", "with", "that", "this", "these", "those", "we", "it", "as", "at", "by",
    "from", "which", "our", "their", "can", "has", "have", "had", "not", "but", "or",
    "an", "a", "all", "each", "more", "than", "when", "where", "while", "if", "so",
    "such", "very", "would", "should", "could", "will", "may", "also", "into", "over",
    "one", "two", "both", "same", "only", "most", "some", "any", "no", "every",
)
_COMPOUNDS: tuple[str, ...] = (
    "SelfAttention", "Self-Attention", "MultiHead", "Multi-Head", "FeedForward",
    "Feed-Forward", "PositionalEncoding", "GoogleBrain", "GoogleResearch",
    "UniversityofToronto", "ScaledDotProduct", "MaskedMultiHead",
)
# 补空格时用的紧凑区间：保留原始坐标以便对回去
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])")
# 驼峰拆分会把"本来就是一整词"的东西拆坏：Attention / Transformer / Softmax
# 都是单个词，`(?<=[a-z])(?=[A-Z])` 对它们没有边界（词首大写、后面全小写），
# 但扫到词中间时（"MultiHeadAttention"）就会拆出 "Attention" 之外的东西。
# 保护方式：先把它们换成长度相同的掩码串（保持字符数，方便后续按位置回填），
# 拆完再换回来——**不能**用会改变长度的占位符，那会让所有偏移失效。
_PROTECT: tuple[str, ...] = ("Attention", "Transformer", "Softmax", "WordPiece")
_FUNC_ONLY = re.compile(r"^[A-Za-z]+$")


def _despace_ocr(text: str) -> str:
    """给 OCR 出来的粘合文本补回空格。

    扫描件 OCR 的典型毛病是**丢空格**："Was trainedourmodels" / "GoogleBrain"。
    这会影响 `_SECTION_RE`（"5.2Hardware andSchedule" 认不出）、摘要可读性、
    以及择优打分（粘在一起的标题没法比对）。

    处理顺序：
      1. 编号后面紧跟字母：`5.2Hardware` → `5.2 Hardware`
      2. 函数词接大写字母：`trainedOur` → `trained Our`
      3. 单字掩码保护（_PROTECT）：`Softmax` 这类单词不能被第 4 步拆开
      4. 驼峰边界补空格：`GoogleBrain` → `Google Brain`
      5. 已知复合词放行：`FeedForward` → `Feed Forward`（本来就是两个词）

    第 4 步是"有损"的，所以第 3 步先把不该拆的单词换成等长掩码，
    拆完再原样换回来——用等长串是为了不破坏任何偏移。
    """
    if not text:
        return text
    out = text
    # 1) 编号 / 数字后接大写字母
    out = re.sub(r"(\d)\s*(?=[A-Z])", r"\1 ", out)
    # 2) 函数词前后补空格。
    #    不能用"前补 + 后补"两条 lookbehind：`trainedourmodels` 里 `our` 前面是 `d`、
    #    后面是 `m`，两个条件互斥，一条都匹配不上。
    #    这里只补"左侧粘字母、右侧粘大写"这一种，够用且不会重复插空格。
    for w in _PHRASES_EN:
        out = re.sub(rf"(?<=[A-Za-z]){w}(?=[A-Z])", f" {w} ", out)
    # 3) 单字掩码：把不该拆的整词换成长度相同、无驼峰边界的串。
    #    只掩"独立成词"的情况（前面不是小写字母）：
    #    `Softmax` / `the Transformer` 要保住，
    #    但 `SelfAttention` / `ScaledDotProductAttention` 里的 `Attention`
    #    是复合词的后半截，前面粘着小写字母——那正是需要拆的地方，不能掩。
    masks: dict[str, str] = {}
    for i, w in enumerate(_PROTECT):
        mask = f"\x01{i:02d}" + "_" * max(0, len(w) - 3)
        pat = re.compile(rf"(?<![a-z]){w}")
        if pat.search(out):
            out = pat.sub(mask, out)
            masks[mask] = w
    # 4) 驼峰边界
    out = _CAMEL_SPLIT.sub(" ", out)
    # 5) 还原掩码
    for mask, w in masks.items():
        out = out.replace(mask, w)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# 扫描件里"看起来像章节标题"的短行：`7Conclusion` / `5.2Hardware andSchedule`。
# OCR 版式全塌了，只能靠"编号开头 + 后面是大写开头的短标题 + 不是句子"来认。
_OCR_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s*([A-Z][A-Za-z ,&'’\-]{0,60})$")


def _ocr_looks_like_heading(text: str) -> bool:
    """扫描件的章节标题判据（没有字号可依据，只能看形状）。

    正例：`1Introduction` `2Background` `5.2Hardware andSchedule` `7Conclusion`
    反例：`34.`（正文里的行号）`2014English-Frenchdatasetconsisting of36Msentences...`（长句）
    """
    t = text.strip()
    if not (4 <= len(t) <= 70):
        return False
    m = _OCR_HEADING_RE.match(t)
    if not m:
        return False
    title = m.group(2).strip()
    if len(title) < 3:
        return False
    words = title.split()
    if len(words) > 8:                     # 标题不会很长
        return False
    if sum(1 for w in words if w[:1].islower() and w not in {"and", "of", "the", "in", "on", "for", "with", "to"}) >= 2:
        return False                       # 小写开头的词太多 = 正文句子
    if re.search(r"[.;:!?]\s+\S", t):      # 出现句子标点 = 正文
        return False
    return True


def _parse_scanned(path: str, pages: int, figure_dir: str) -> dict:
    """扫描件专用解析路径：全部料来自 OCR 缓存，**没有任何版式信息**。

    与 `parse_structure` 主路径的区别：
      - 标题/作者/单位：没有字号和坐标，改用"行序 + 形状"判定；
      - 章节：`_SECTION_RE` 用不上（"5.2Hardware" 没有空格），改用 `_ocr_looks_like_heading`；
      - 表格/图表：题注能认出，但**行内容抽不出来**（图中文字 OCR 不可靠），留空；
      - 图表图片：照常渲染（扫描件本身就是图片，裁出来即图纸）；
      - 公式：`_is_formula_line` 照用（OCR 出来的公式反而更干净）。

    抽不到就返回空值，**绝不抛异常**。
    """
    text_map = _ocr_pages(path, pages)
    if not text_map:
        return {}
    lines = _ocr_lines(path, pages)
    if not lines:
        return {}

    # ---- 标题：第 0 页前若干行里，第一个"像论文标题"的行 ----
    first_page = [ln for ln in lines if ln["page"] == 0]
    title = ""
    title_i = -1
    for i, ln in enumerate(first_page[:12]):
        t = _despace_ocr(ln["text"])
        if len(t) < 8 or "@" in t:
            continue
        if t.lower() in ("abstract", "abstract:", "abstract：", "arxiv"):
            continue
        if _is_affil_text(t) or _looks_like_person(_despace_ocr(_strip_marks(t))):
            continue
        # OCR 常冒出 arXiv 水印的碎片（本文件第 0 页就有一行 ".CL]"、" [cs."），
        # 这类行没有完整单词，不能当标题。
        if not re.search(r"[A-Za-z]{3,}", t) or len(re.findall(r"[A-Za-z]{2,}", t)) < 3:
            continue
        title = t[:200]
        title_i = i
        break

    # ---- 作者 / 单位 / 邮箱：标题行之后、Abstract 之前 ----
    authors: list[str] = []
    affiliations: list[str] = []
    emails: list[str] = []
    author_affiliations: dict[str, list[str]] = {}
    if title_i >= 0:
        zone: list[str] = []
        for ln in first_page[title_i + 1:title_i + 40]:
            # 先判 Abstract：它是摘要的分界线，走到这里作者区就结束了。
            # 而且 "Abstract" 命中 `_AFFIL_WORDS`（机构词表里就有它），
            # 不先拦会被当成一个工作单位收进去。
            if re.match(r"(?i)^\s*abstract\b", ln["text"]):
                break
            t = _despace_ocr(ln["text"])
            if not t or re.fullmatch(r"[\d\W_]+", t):   # 纯数字/纯符号行（角标、页眉日期）
                continue
            zone.append(t)
        # ---- 作者 / 单位配对 ----
        # 扫描件没有坐标，但版面顺序是"作者行…单位行…邮箱行…"：OCR 逐行吐，
        # 单位的相对顺序和作者的列顺序是对应的。
        # 分列规则：先按"尾名首次出现的顺序"重排成列（见 `_ocr_name_columns`），
        # 然后第 i 个单位（去重后）给第 i%列数 列的所有作者、第 i+1 个给下一列……
        # 实测 Attention 扫描件：4 个作者列 × 2 位，2 个不同单位正好转一圈。
        zone_names: list[str] = []
        zone_affils: list[str] = []
        for t in zone:
            hit = _EMAIL_RE.findall(t)
            if hit:
                emails += [e for e in hit if e not in emails]
                continue
            # OCR 把邮箱拆到两行时会剩半个地址，见到 @ 或 .edu/.com 都放行不进单位
            if "@" in t or re.search(r"(?i)\.(?:edu|com|org|cn)\b", t):
                continue
            if _is_affil_text(t):
                zone_affils.append(t)
                continue
            name = _fix_ocr_leading_i(_clean_ocr_name(t))
            if name and _looks_like_person(name):
                zone_names.append(name)
        # 去重但保序
        zone_names = [a for i, a in enumerate(zone_names) if a not in zone_names[:i]]
        affil_uniq = [a for i, a in enumerate(zone_affils) if a not in zone_affils[:i]]
        authors.extend(zone_names)
        affiliations.extend(zone_affils)
        # OCR 会把角标单独甩成一行（Attention 扫描件的 "llia Polosukhin*" 下面是 "2"、
        # 再下面是邮箱），那一位作者的人名行根本进不来。这里按**邮箱推定姓名**补回来：
        #   "lukaszkaiser@google.com" -> "Lukasz Kaiser"（拿已知作者做全小写比对）
        #   "avaswani@google.com"     -> 对不上就补不了，如实留空
        # 只认"能对上某位已知作者的姓名"的，不凭空造名字。
        known = {re.sub(r"[^a-z]", "", a.lower()): a for a in authors}
        for addr in emails:
            local = re.sub(r"[^a-z]", "", addr.split("@")[0].split(".")[0].lower())
            if not local:
                continue
            for flat, name in known.items():
                if name in authors:
                    continue
                if flat == local or (len(local) >= 5 and flat.replace(" ", "").endswith(local)):
                    authors.append(name)
                    break
        # 先分列再配对：单位的相对顺序就是列的先后顺序
        cols = _ocr_name_columns(authors)
        if cols and affil_uniq:
            for i, col in enumerate(cols):
                affil = affil_uniq[i % len(affil_uniq)]
                for a in col:
                    author_affiliations.setdefault(a, [])
                    if affil not in author_affiliations[a]:
                        author_affiliations[a].append(affil)
        # OCR 丢了句点的邮箱（"avaswani@google"）在纯邮箱行里补不回来，如实留空
    authors = [a for i, a in enumerate(authors) if a not in authors[:i]]
    affiliations = [a for i, a in enumerate(affiliations) if a not in affiliations[:i]][:12]

    # ---- 摘要 ----
    abstract = ""
    joined = "\n".join(ln["text"] for ln in lines)
    m = re.search(r"(?im)^\s*abstract[:：]?\s*$", joined)
    if m:
        # 首页剩下的那截 + 后续各页，都要收进摘要候选
        tail = (joined[m.end():] + "\n" + "\n".join(
            ln["text"] for ln in lines if ln["page"] > 0
        )).split("\n")
        buf: list[str] = []
        for t in tail:
            tt = _despace_ocr(t.strip())
            if not tt:
                continue
            # arXiv 水印会插在摘要中间（扫描件的第 0 页实测有 ".CL]"、" [cs."、"a rXiv:170 ..."），
            # 判据：长度 ≤12、只有字母数字方括号点斜杠、且不含真实英文单词。
            if len(tt) <= 12 and re.fullmatch(r"[\[\]()./A-Za-z0-9:]{0,14}", tt) and not re.search(r"[a-z]{5,}", tt):
                continue
            if re.search(r"(?i)a\s*r\s*[Xx]\s*iv\s*:", tt):
                continue
            if _ocr_looks_like_heading(tt) or re.fullmatch(r"(?i)1\s*introduction", tt):
                break
            buf.append(tt)
            if sum(len(x) for x in buf) > 800:
                break
        abstract = " ".join(buf)[:800].strip()
    elif title_i >= 0:
        # 没抓到 "Abstract" 标题：取首页里最长的几行正文
        cand = [_despace_ocr(ln["text"]) for ln in first_page if len(ln["text"]) >= 60]
        abstract = " ".join(cand)[:800].strip()

    # ---- 章节树 ----
    sections: list[dict] = []
    seen: set[tuple[str, int]] = set()
    last: dict[int, int] = {}
    for ln in lines:
        t = _despace_ocr(ln["text"])
        if not _ocr_looks_like_heading(t):
            continue
        mm = _OCR_HEADING_RE.match(t)
        if not mm:
            continue
        num = mm.group(1)
        heading = mm.group(2).strip()
        if not _num_ok(num):
            continue
        level = num.count(".") + 1
        if not _number_follows(num, last):
            # 扫描件 OCR 常把 4 读成 5，连续性判据太严会整棵树断掉；
            # 宽松处理：只要不是"重复编号"就收，靠后面的去重挡重复行。
            if any(s["number"] == num for s in sections):
                continue
        _number_bump(num, last)
        key = (num, ln["page"])
        if key in seen:
            continue
        seen.add(key)
        sections.append({"number": num, "title": heading, "level": level, "page": ln["page"]})
        if len(sections) >= 400:
            break

    # ---- 表格 / 图表题注（内容抽不出来，只留题注）----
    tables, figures = _captions(lines)
    for t in tables:
        t.setdefault("rows", [])

    # ---- 参考文献 ----
    references = _references_ocr(lines)

    # ---- 图表图片 ----
    if figure_dir and figures:
        _figure_shots(path, figures, figure_dir, Path(path).stem)

    return {
        "title": title,
        "authors": authors,
        "affiliations": affiliations[:12],
        "author_affiliations": author_affiliations,
        "emails": emails,
        "abstract": abstract,
        "keywords": [],
        "sections": sections,
        "tables": tables,
        "figures": figures,
        "formulas": _formulas(lines),
        "references": references,
        "appendix": _appendix(sections),
        "pages": pages,
        "source": "ocr",
    }


def _is_affil_text(t: str) -> bool:
    """OCR 出来的这一行是不是工作单位。

    判据顺序很重要：**先查机构词，再谈"像不像人名"**。
    "Google Brain" / "Google Research" 都是两个首字母大写的词，形状和人名没法区分，
    只能靠机构词表（brain / research / google ...）判定。
    而 `_AFFIL_WORDS` 全是小写，比较时必须先 lower()——否则 "Google Brain" 永远不命中。
    """
    s = t.strip()
    if not s:
        return False
    low = s.lower()
    return any(w in low for w in _AFFIL_WORDS)


_NAME_LEAD_SKIP = frozenset({
    "van", "von", "del", "der", "la", "le", "da", "di", "dos", "das", "bin", "al",
    "de", "the", "and", "of", "for", "with", "in", "on", "at", "by", "to", "our",
    "we", "this", "these", "that", "a", "an", "is", "are", "as", "it", "its",
})


def _ocr_name_columns(names: list[str]) -> list[list[str]]:
    """把 OCR 切出来的作者行按"列"分组。

    扫描件首页的作者是按列排的，OCR 逐行吐出来的顺序是
    `W1 W2 W3 W4 A1 A2 A3 A4 M1 M2 M3 M4`（每列行数相同时，行序完全打乱列边界）。
    这里按"名字的尾名首次出现的顺序"重新分列：
      - 先按尾名（姓）给每个名字编号，同时记下每个姓第一次出现时的位置；
      - 位置越靠前，说明这一列越靠左；
      - 同一列的名字按出现顺序排列。
    实测 Attention 扫描件 8 位作者正好分成 4 列 × 2 个，与原文版式一致。
    """
    order: list[str] = []
    pos: dict[str, int] = {}
    for i, nm in enumerate(names):
        parts = nm.split()
        if len(parts) < 2:
            continue
        key = parts[-1]
        if key not in pos:
            pos[key] = i
            order.append(key)
    order.sort(key=lambda k: pos[k])
    cols: list[list[str]] = [[] for _ in order]
    idx = {k: i for i, k in enumerate(order)}
    for nm in names:
        parts = nm.split()
        if len(parts) < 2:
            continue
        cols[idx[parts[-1]]].append(nm)
    return [c for c in cols if c]


def _clean_ocr_name(t: str) -> str:
    """把 OCR 粘起来的人名还原成 `Ashish Vaswani`。

    OCR 会在名字后面留脚注符号，甚至把角标单独甩一行：
    "AshishVaswani*" / "2" / "llia Polosukhin*"。
    所以先从行尾剥掉 `* † ‡ ∗ ⋄ § ¶` 和数字，再做补空格。
    """
    s = _despace_ocr(t.split("\n")[0])
    # 行尾的字母角标（"Aidan N. Gomez*t" 的 "*t"）：先剥离符号再剥单个尾字母，
    # 否则 "Gomez*t" 里那个 t 会留在名字里，害得整行认不出是人名。
    s = re.sub(r"\s*[*†‡∗⋄§¶]+\s*[a-zA-Z]?$", "", s)
    s = re.sub(r"[*†‡∗⋄§¶\d]+\s*$", "", s).strip()
    s = _despace_ocr(s)
    return s.strip()


# OCR 常把行首的 `I` 认成 `l` 或 `|`（"Illia" -> "llia"、"IIlia"）。
# 只修第一段是名字的情况：后面紧跟 ≥2 个小写字母，避免把 "Long" 改成 "Iong"。
_OCR_LEADING_I = re.compile(r"^(l|I|\|)(?=[a-z]{2,}\s+[A-Z])")


def _fix_ocr_leading_i(name: str) -> str:
    return _OCR_LEADING_I.sub("I", name)


def _looks_like_person(t: str) -> bool:
    """OCR 版的人名判定：2~4 个首字母大写的词，且不含机构词。

    上限放到 5 是为了 "Henrique Ponde de Oliveira Pinto" 这种西语全名；
    但标题（"Attention Is All You Need" = 5 词）也会落进这个区间，
    所以再加一条：**虚词（of/and/the/is...）出现在名字里就不像人名**。

    例外：中间名缩写 `Aidan N. Gomez` 里的 "N." 去掉句点后是 "N"，
    只由一个字母组成，它不是虚词，不能拿 _PHRASES_EN 去挡。
    """
    s = t.strip()
    if not (4 <= len(s) <= 60):
        return False
    if not _NAME_LOOSE_RE.match(s):
        return False
    words = s.split()
    if not 2 <= len(words) <= 5:
        return False
    # 机构词：注意不能直接调 _is_affil_text —— 它对人名行有豁免，这里要的是"查词"
    flat = s.lower().replace(" ", "")
    if any(w in flat for w in _AFFIL_WORDS):
        return False
    if any(w.lower() in _PHRASES_EN and not _is_initial(w) for w in words):
        return False
    # OCR 常把行首 I 认成 l/|；`_fix_ocr_leading_i` 已经修过一遍，
    # 这里再兜底一次，保证判定和落库用的是同一个名字。
    return bool(_NAME_LOOSE_RE.match(_fix_ocr_leading_i(s)))


def _is_initial(word: str) -> bool:
    """`N` / `N.` / `J.` 这种单字母中间名缩写。"""
    return bool(re.fullmatch(r"[A-Z]\.?", word))


def _references_ocr(lines: list[dict]) -> dict:
    """扫描件的参考文献：OCR 把 `[15]Lukasz Kaiser` 粘在一起，先补空格再用通用逻辑。"""
    fixed = []
    for ln in lines:
        t = re.sub(r"^(\s*\[\d{1,3}\])\s*", r"\1 ", ln["text"])
        fixed.append({**ln, "text": t})
    return _references(fixed)


def parse_structure(path: str, max_pages: int = 0, with_tables: bool = True,
                    figure_dir: str = "") -> dict:
    """返回结构化字段；抽不到就是空值，不抛异常。

    max_pages=0（默认）= 整篇都读。

    以前默认 25 页，实测代价很大：43 篇里 26 篇超过 25 页，被砍掉
    52% 的表格/图表、51% 的章节、38% 的公式。而赛题 1.1.2 明确要求"粒度越细越好"，
    并且点名了 541 页的书籍。改成整篇读之后，541 页的书重解析仍是 12 秒
    （下面 `_find_references` 本来就把全书扫了一遍，主要开销早就在那儿了），
    GPT-4 那份 100 页的报告从 6.4s 变成 10.2s，都在上传路径的一次性成本里。

    传一个正整数仍可用作上限（大文件保险），但不再有"默认只看前 25 页"。

    figure_dir 非空时，把每张图表的图片本身渲染成 PNG 存进这个目录，
    并把文件名写进 `fig["image"]`。默认为空 = 不写任何文件，
    保证"纯解析"这条路径依旧是只读的（测试与调用方不受影响）。
    """
    empty = {
        "title": "", "authors": [], "affiliations": [], "author_affiliations": {}, "emails": [], "abstract": "",
        "keywords": [], "sections": [], "tables": [], "figures": [], "formulas": [],
        "references": {"page": None, "count": 0, "first": "", "last": ""}, "appendix": [], "pages": 0,
        "source": "",
    }
    try:
        import pymupdf  # 延迟导入：只有要解析版式时才需要

        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001 —— 解析失败不能拖垮上传
        return empty

    try:
        pages = len(doc)
        first = _lines(doc[0], 0)
        # 章节/题注/公式/参考文献要往后看，否则 References 都看不到。
        # max_pages<=0 表示不限：章节、表格、图表、公式都按整篇读，
        # 只有版式分析（标题/作者/单位/摘要）留在首页。
        limit = pages if max_pages <= 0 else min(max_pages, pages)
        head = first[:]
        for pno in range(1, limit):
            head += _lines(doc[pno], pno)
        # 扫描件：文字层整篇为空（"1706.03762v1_img.pdf" 每页 0 字、只有一张内嵌图）。
        # 这种情况走 OCR 缓存那条路——缓存是离线批处理好的，这里只读文件、不做 OCR。
        # 判据用 `head` 而不是只测首页：有的 PDF 首页有页眉文字、正文却整页是图。
        if not head:
            scanned = _parse_scanned(path, pages, figure_dir)
            if scanned:
                return scanned
            return empty
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
            # 备用路径：pdfplumber 对"只有一两列、没有横线"的表常常一条都抽不出。
            # 只在确实有表没抽到行时才跑，且每页只跑一次。
            missed = sorted({t["page"] for t in tables if not t["rows"]})
            if missed:
                backup = _table_rows_pymupdf(path, missed)
                for t in tables:
                    if t["rows"]:
                        continue
                    cand = backup.get(t["page"])
                    if cand:
                        t["rows"] = cand[0]
        # 图表图片：只在这里统一渲染一次（上面已经拿到全部题注和页码）
        if figure_dir and figures:
            _figure_shots(path, figures, figure_dir, Path(path).stem)
        return {
            "title": title,
            "authors": _authors(first, title_y, body),
            "affiliations": _affiliations(first, title_y),
            "author_affiliations": _author_affiliations(first, title_y, _authors(first, title_y, body)),
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
            "source": "text",
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
