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

import contextlib
import re
from pathlib import Path

from app.layout import layout_summary
from app.logging_config import get_logger
from app.papers import PAPERS_DIR, _load_index, load_structures, paper_path

logger = get_logger(__name__)

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

    两种命名都要认：手动拷进 data/papers 的保留原名，走上传接口存进来的是
    `<paper_id>.pdf`。后者磁盘上根本没有 `paper.filename` 这个文件，只按名字直查
    会一律 404（实测 44 篇里有 5 篇是这样：GPT-2、1602.07776、1705.03122、
    1610.08613、2312.00752）。所以直查落空时回落到索引，用 paper_id 找。
    """
    if not filename or filename != Path(filename).name:
        return None
    if Path(filename).suffix.lower() != ".pdf":
        return None
    root = PAPERS_DIR.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent == root and candidate.is_file():
        return candidate

    # 直查落空：按索引里的 filename 反查 paper_id，再走 paper_path（它认两种命名）。
    # 索引项必须重新校验名字，别信 index.json 里存的任意值。
    with contextlib.suppress(Exception):
        for row in _load_index():
            if row.filename != filename:
                continue
            found = paper_path(row.paper_id)
            if found is None:
                continue
            resolved = found.resolve()
            if resolved.parent == root and resolved.is_file():
                return resolved
    return None


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


# --------------------------------------------------------------------- 引用句定位
#
# 背景：写作助手生成的正文是中文，本库论文的 PDF 是英文。
# 点「读原文」要跳到「这篇论文的哪一部分支撑了那句话」，直接拿中文句去搜命中率是 0；
# 退化成用标题词定位，高亮落在标题上，等于没回答问题。
#
# 实测出来的两条硬事实（决定了下面对算法的选择）：
#   1. 整句字面匹配几乎必然失败——PDF 文本层里句子被换行切断
#      （"The fundamental\nconstraint of sequential computation"），
#      还有连字符断行（"transduc-\ntion"）和连字（"eﬃcient" 里的 ﬃ）。
#   2. 就算把换行归一化，整句也常常搜不到——回译出来的英文是我们自己的说法，
#      不是论文原话。例：正文「计算与内存开销随序列长度呈二次增长」，
#      Attention 原文根本没有 quadratically 这个词，写的是
#      "a self-attention layer connects all positions with a constant number of
#       sequentially executed operations"。所以不能指望整句命中。
#
# 因此改用「关键词密度 + 滑窗」：
#   把英文句子拆成实词，逐页统计命中数选页，再在该页找一个包含最多实词、
#   跨度最紧的词窗口，那个窗口的矩形就是高亮区。
#   实测 Attention 第 2 页命中 9/14 词，滑窗定位到的正是讲位置依赖的那段。

# 英文虚词表：这些词在任何学术论文里都出现，拿来当检索词只会让每页都"命中"
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "this", "that", "these", "those", "there", "their", "them", "they", "than",
        "then", "with", "without", "from", "into", "onto", "over", "under", "upon",
        "about", "above", "after", "before", "between", "during", "through",
        "which", "while", "where", "when", "what", "whose", "whom", "have", "has",
        "had", "been", "being", "were", "was", "are", "is", "am", "will", "would",
        "shall", "should", "could", "can", "may", "might", "must", "also", "such",
        "more", "most", "much", "many", "some", "any", "all", "both", "each",
        "other", "another", "only", "same", "very", "just", "even", "still",
        "however", "therefore", "thus", "hence", "moreover", "furthermore",
        "does", "did", "doing", "done", "not", "nor", "but", "and", "for", "the",
        "via", "per", "although", "though", "because", "since",
        "allow", "allows", "allowed", "make", "makes", "made", "use", "uses",
        "used", "using", "based", "given", "show", "shows", "shown", "propose",
        "proposes", "proposed", "achieve", "achieves", "require", "requires",
        "provide", "provides", "large", "small", "high", "low",
    }
)

# 句子拆词时丢掉这些尾巴，否则 "positions." 和 "positions" 匹配不上
_WORD_TRIM = ".,;:!?()[]{}\"'“”‘’·…"

# 一个窗口最多取多少个词（约 3-4 行正文）。太长会把整页包进去，太短装不下一个论断。
_WINDOW_WORDS = 45
# 窗口里至少要有这么多个实词命中，否则不算定位成功，宁可退回标题页
_MIN_WINDOW_HITS = 3
# 实词总数少于这个数，说明这句话本身没信息量，别硬定位
_MIN_KEYS = 3


def _content_words(sentence: str) -> list[str]:
    """把英文句子拆成可检索的实词（小写、去虚词、去标点、长度 ≥4）。"""
    keys: list[str] = []
    for raw in (sentence or "").split():
        w = raw.strip(_WORD_TRIM).lower()
        if len(w) >= 4 and w not in _STOP_WORDS and w.isascii():
            keys.append(w)
    return keys


def locate_claim_in_pdf(filename: str, english: str) -> dict | None:
    """在一篇本库 PDF 里找到最能支撑 ``english`` 这句话的位置。

    返回 ``{"page": 1 基页码, "rect": [x0,y0,x1,y1], "snippet": 命中窗口原文,
    "hits": 命中实词数, "keys": 实词总数}``；定不到就返回 None，调用方退回标题页。

    为什么返回矩形而不是整页：前端要把这一块涂黄，只给页码等于让用户自己找。
    矩形用的是 PDF 页面坐标系（左上原点），前端按 canvas 的实际缩放换算。
    """
    path = safe_paper_path(filename)
    if path is None or not english:
        return None
    keys = _content_words(english)
    if len(keys) < _MIN_KEYS:
        return None
    key_set = set(keys)

    import pymupdf  # 延迟导入：只有这条路径需要，别让阅读页元数据也背上启动开销

    best: tuple[int, int, int, list, str] | None = None  # (hits, -span, page, rect, snippet)
    try:
        with pymupdf.open(path) as doc:
            for pno in range(doc.page_count):
                words = doc[pno].get_text("words")
                if len(words) < 10:
                    continue
                low = [w[4].strip(_WORD_TRIM).lower() for w in words]
                hit_flags = [w in key_set for w in low]
                # 页级命中数先过滤：一页都不沾几下的，没必要滑窗
                if sum(hit_flags) < _MIN_WINDOW_HITS:
                    continue
                win = min(_WINDOW_WORDS, len(words))
                for start in range(0, max(1, len(words) - win + 1)):
                    end = min(start + win, len(words))
                    hits = sum(1 for f in hit_flags[start:end] if f)
                    if hits < _MIN_WINDOW_HITS:
                        continue
                    # 命中多的优先；命中一样多时取跨度更紧的（更可能是一段而非一整页）。
                    # 用元组整体比较：拆成 `score > (best[0], -best[1])` 时，
                    # 命中数与跨度都相等的情况会一直判为"不更大"，
                    # 结果取决于遍历顺序——实测同一篇论文两次调用落在第 7 页和第 10 页。
                    score = (hits, -(end - start), -pno)
                    if best is None or score > best[0]:
                        x0 = min(w[0] for w in words[start:end])
                        y0 = min(w[1] for w in words[start:end])
                        x1 = max(w[2] for w in words[start:end])
                        y1 = max(w[3] for w in words[start:end])
                        snippet = " ".join(words[i][4] for i in range(start, end))
                        best = (score, hits, pno + 1, [x0, y0, x1, y1], snippet)
    except Exception as exc:  # 坏 PDF 不该让「读原文」整条链路失败
        logger.warning("定位引用句失败 %s: %s", filename, exc)
        return None

    if best is None:
        return None
    score, hits, page, rect, snippet = best
    return {
        "page": page,
        "rect": [round(v, 2) for v in rect],
        "snippet": " ".join(snippet.split())[:300],
        "hits": hits,
        "keys": len(keys),
        "span": -score[1],
    }
