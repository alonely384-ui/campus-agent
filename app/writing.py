"""写作助手（赛题 1.6）：论文框架生成 / 架构图生成 / 数据可视化 / 中译英。

## 这个模块的组织方式

四个子项各是一个函数，都接收"用户给的东西"，返回"可以直接塞给前端的 dict"。
不在这里写 HTTP，接口在 ``app/main.py``。

## 为什么 1.6.1 要这么绕（核心设计）

赛题原文：「给定一个 idea，让论文自动生成论文的框架，包括：Abstract、Introduction、
Related Work 和 References 部分。**确保 References 是真实没有幻觉**。」

最直觉的做法是 `给模型一个 idea → 让模型写完包括参考文献 → 完事`。
**这个做法一定会出现幻觉引用**，因为模型写参考文献时是在"续写文本"，
它会把格式模仿得很像，但 DOI / 作者 / 年份大概率是编的。

这里用的做法叫 **候选集约束生成**：

    1. 用 idea 去检索真实文献，得到候选池（每篇都有 key：C1、C2…）
    2. 把候选池**连 key 一起**塞进 prompt，明确告诉模型「只能引用池里的 key」
    3. 模型输出时只写 ``[C3]`` 这种标记，不写完整的参考文献条目
    4. 参考文献列表由**我们的代码**按 key 从候选池里取——模型碰不到
    5. 事后校验：抓出输出里所有 ``[Cn]``，凡是不在池里的标记一律标红

这样「出现一篇不存在的论文」在结构上就不可能，而不是靠模型自觉。

## 为什么摘要要从库里取而不是让模型编

Related Work 里说「BERT 通过掩码语言建模预训练」这种话，如果是从真实摘要里
摘出来的，评委会更信；如果模型自己写，容易把 A 论文的贡献安到 B 论文头上。
候选池里本库论文带摘要的，优先给模型看摘要。
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app import literature as lit
from app.llm import chat
from app.logging_config import get_logger

logger = get_logger(__name__)

# 引用标记：模型应输出 [C3] 这种形式，也可能写成 [C3, C5] / [C3;C5]
# 只用一个正则，按组内逗号切开，避免两个正则互相打架漏标
_MARK_GROUP_RE = re.compile(r"\[\s*(C\d{1,3}(?:\s*[,;，、]\s*C\d{1,3})*)\s*\]")

# 参考文献里最多列几位作者，超出加 et al.。IEEE 用 6，这里跟 IEEE 对齐。
MAX_REF_AUTHORS = 6

# 引用句回译（claim_to_english）：太短的残句不值得花一次调用，翻出来也搜不到。
# 上限防模型把一句话扩写成小作文，PDF 里不可能逐字命中。
_MIN_CLAIM_CHARS = 8
_MAX_CLAIM_CHARS = 240


# --------------------------------------------------------------------------- 候选池 → prompt


def render_pool(pool: list[lit.Candidate], with_abstract: bool = True) -> str:
    """把候选池渲染成给模型看的文本。

    刻意用固定格式的纯文本而不是 JSON：模型对「编号. 标题 (作者, 年份)」这种
    列表格式的遵循度明显更高，JSON 有时会漏读字段。

    本库论文若挂了 ``evidence``（正文证据块），会紧跟在摘要后面渲染一段原文。
    **截断只在这里发生**：``Candidate.evidence`` 里存的是完整原文，
    渲染时截到 ``_EVIDENCE_EXCERPT_CHARS``，够模型判断这段讲什么就行。
    """
    lines: list[str] = []
    for c in pool:
        year = c.year if c.year else "年份不详"
        authors = ", ".join(c.authors[:3]) if c.authors else ""
        if len(c.authors) > 3:
            authors += " et al."
        head = f"[{c.key}] {c.title} ({authors}, {year})"
        if c.venue:
            head += f" — {c.venue}"
        lines.append(head)
        if with_abstract and c.abstract:
            # 摘要只截前 240 字，够模型判断这篇讲什么，又不至于把 prompt 撑爆
            lines.append(f"    摘要：{c.abstract[:240]}")
        for ev in c.evidence or []:
            page = ev.get("page")
            where = f"第 {page} 页" if page else "页码未定位"
            lines.append(f"    本库原文（{where}）：{_excerpt(ev.get('text') or '')}")
    return "\n".join(lines)


#: 渲染 prompt 时每条正文证据留多少字。真实 chunk 长度中位数 228、上限 298，
#: 留 260 基本等于整块保留，又给极端长块兜底。完整原文仍在 candidate.evidence 里。
_EVIDENCE_EXCERPT_CHARS = 260


#: 关于「本库原文」的说明段。只在池子里真的有条目带证据时才追加，
#: 否则会教模型去找一段根本不存在的原文。措辞刻意不承诺「逐论点验证」：
#: 这些块是检索命中并通过话题相关的，不是「已证明支撑你这句话」。
_EVIDENCE_NOTE = (
    "【关于「本库原文」】部分候选条目下面附了「本库原文」片段，那是从本系统已收录的"
    "PDF 正文中检索到的真实段落。它比摘要更贴近原文措辞，可以据此判断这篇是否"
    "真的讲了你要说的事；引用时可以更具体地概括它的做法。但请注意：这些片段只是"
    "「话题相关」的原文，**并没有被逐句验证过支撑某个具体论点**，不要把它当成"
    "「已证明」而写出原文里没有的结论。条件允许时，优先用「本库原文」覆盖到你"
    "论断的那些候选。\n\n"
)


def _excerpt(text: str) -> str:
    """把一块原文压成一行、截到 ``_EVIDENCE_EXCERPT_CHARS``。

    chunk 里常带换行（PDF 断行），直接塞进 prompt 会让「原文：」后面的内容
    看起来像新的候选条目，模型会把它当成另一篇文献的标题。所以先合并空白。
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= _EVIDENCE_EXCERPT_CHARS:
        return flat
    return flat[:_EVIDENCE_EXCERPT_CHARS].rstrip() + "…"


FRAMEWORK_SYSTEM = """你是一位资深的学术论文写作助手，帮助研究者从零起草论文框架。

**最重要的规则（违反即作废）：**
你只能引用下面「候选文献」里给出的编号（如 C1、C2）。绝对不允许：
- 编造不存在于候选列表中的论文
- 编造作者、年份、期刊名、DOI
- 写完整的参考文献条目（参考文献由系统根据你给的编号自动生成，你只管写编号）

引用写法：在正文里要引用某篇时写 `[候选编号]`，例如 `[C3]` 或 `[C1, C4]`。
如果某句话找不到合适的候选文献支撑，就**不要写这句话的引用**，也不要把编号硬凑上去。

**编号必须与论断对得上（这条最容易违规，重点自查）：**
每写一个编号前，先核对它对应的标题与摘要是否真的支撑你要说的这句话。
- 不要凭「我记得领域里有篇讲 X 的论文」就去挑一个看起来像的编号。
  候选池里未必真有那篇论文，硬凑会变成「编号是真的、引用关系是假的」，
  这比干脆不引用更糟，因为读的人会去查，一查就露馅。
- 如果池子里没有真正支撑该论断的文献，就**改写这句话**：去掉具体归因，
  不要写「某某方法提出了 X」，改成「该方向已有工作探索过类似思路」这类
  不过度归因的表述，或者干脆删掉这句。
- 一句话引用多篇时，每一篇都必须各自独立支撑它。
- 例子（反面）：池子里有《RAG 检索失败分析》，你写「混合专家式稀疏化提升了效率[C4]」。
  这两者毫无关系，属于典型错配。正确做法是不引用 C4，或改写成池中确实支撑的说法。

**语言纯净化：**
正文用中文完整表述，不要在中文句子里夹英文普通词。
专有名词（Transformer、Attention、FlashAttention）和论文标题可以保留英文，
但 complement、baseline、pipeline、benchmark、framework 这类普通词一律写中文
（正交、基线、流水线、基准、框架）。

**输出语言（必须遵守）：**
正文语言跟随【研究 idea】的语言——idea 是中文就用中文撰写，是英文就用英文撰写。
即使候选文献的标题和摘要是英文，正文也必须用 idea 的语言写，不要因为看到英文文献就切换成英文。
`## Abstract` 这类章节标题保持英文原样；参考文献条目由系统生成，不归你管。

**写作质量要求：**
这是一篇要投出去的真实论文草稿，不是提纲，也不是要点罗列。具体做到：
- Abstract 按「背景问题 → 现有方法的不足 → 本文提出什么 → 预期效果」四段式推进，
  用完整句子写成一个自洽段落，不要出现「本文将…」「本方法…」这种空泛套话充数；
- Introduction 要有学术叙事的推进感：先用具体的研究现状和已知局限把问题立起来，
  再说明为什么这个问题现在值得解决，最后一段明确列出本文的 2-3 条具体贡献；
  每条贡献都要说得足够具体（做了什么、在什么设定下、预期改善什么），
  不要写「提出了一个新颖的方法」这类无法证伪的句子；
- Related Work 要按**技术路线**分组而不是按时间罗列，每组开头先概括这一路的共同思路，
  再指出它的局限，最后说明本文与它的差异；引用必须落在具体论断上，
  不要为了凑引用在句尾硬挂编号；
- 凡是能用候选文献摘要支撑的具体说法，优先用具体说法代替笼统描述
  （例如「基于检索增强的生成」优于「相关方法」）。
- 不要写「综上所述」「值得注意的是」这类公文腔过渡词；不要用感叹号和口语化表达。

输出格式严格按下面四节，用 Markdown 标题：

## Abstract
（一个完整段落，180-260 词或等量篇幅，说明问题、方法、预期贡献。不要引用文献。）

## Introduction
（4-6 段。第一段交代研究背景与具体问题，中间段落分析现有工作的局限，
最后一段列出本文的具体贡献。可以引用候选文献。）

## Related Work
（按技术路线分 2-4 组，每组一个 `### ` 小标题。每组先概括共同思路，
再指出局限，最后点明与本文的关系。必须大量引用候选文献，且编号必须来自候选列表。）

## References
（这一节**只写你用到的编号列表**，每行一个，格式如 `[C3]`。不要展开成完整条目。）
"""


def build_framework_prompt(
    idea: str,
    pool: list[lit.Candidate],
    gaps: list[str] | None = None,
) -> list[dict]:
    """组装 1.6.1 的对话消息。抽出来是为了测试时能单独检查 prompt 内容。

    ``gaps`` 是 idea 里在候选池中没有对应文献的概念词（见
    ``lit.pool_coverage_gaps``）。把它如实告诉模型，比让它自己发现要可靠得多——
    模型看不见「池子里缺了什么」，只会觉得「我记得有篇讲这个的」然后硬挑一个编号。
    """
    pool_text = render_pool(pool) or "（没有检索到候选文献，请如实说明无法给出有依据的 Related Work）"
    has_evidence = any(c.evidence for c in pool)
    gap_note = ""
    if gaps:
        gap_note = (
            "\n【覆盖提示】你的 idea 里这些概念在本次候选池中**没有检索到对应文献**："
            + "、".join(gaps)
            + "。写到这些方向时不要挂任何编号——池子里没有能支撑它的论文，"
            "硬挂就是编造引用关系。改用「该方向已有工作探索过类似思路」这类不归因的写法。\n"
        )
    user = (
        f"【研究 idea】\n{idea.strip()}\n\n"
        f"【候选文献】（共 {len(pool)} 篇，全部经过真实检索核实，你只能引用这些编号）\n"
        f"{pool_text}\n\n"
        f"{gap_note}"
        "【重要】候选池是按关键词挖来的，**其中有一部分和你的 idea 无关**"
        "（比如你研究的是「用引用图谱约束 RAG」，池子里可能混着讲引用网络统计、"
        "或泛泛讲 RAG 评测的论文）。挑选时只认真正支撑你论点的那些，"
        "不相关的文献完全不必引用——少引几篇没关系，引错一篇就是学术事故。\n\n"
        + (_EVIDENCE_NOTE if has_evidence else "")
        + "请按上面的格式要求，生成 Abstract / Introduction / Related Work / References 四节。"
    )
    return [
        {"role": "system", "content": FRAMEWORK_SYSTEM},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- 校验


@dataclass
class CitationCheck:
    """引用校验结果。前端据此把悬空引用标红。"""

    cited_keys: list[str]
    unknown_keys: list[str]
    unused_candidates: list[str]
    #: 编号在池内、但所在句子与对应论文标题/摘要几乎没有词面重叠的引用。
    #: 这类问题比 unknown_keys 更隐蔽：引用关系看起来成立，实际张冠李戴。
    suspicious_keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unknown_keys


# 可疑引用的判定门槛。刻意设得很低：只要引用句和论文本身有一个实义词重叠就放过，
# 只在「完全对不上」时报出来——宁可漏报，也不要把正常引用全标黄让人不敢用。
_SUSPICIOUS_MIN_OVERLAP = 1

# 判定时要忽略的高频虚词。中英文分开列：中文按二字词切，英文按整词。
_STOP_ZH = {
    "本文", "我们", "这个", "这一", "这些", "那些", "因此", "所以", "但是", "然而",
    "可以", "能够", "需要", "进行", "通过", "由于", "对于", "关于", "以及", "例如",
    "相关", "工作", "方法", "过程", "问题", "结果", "效果", "作用", "思路",
    "已有", "现有", "其他", "其中", "这种", "那样", "同时", "并且", "而且", "如果",
}
_STOP_EN = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "which",
    "are", "was", "were", "have", "has", "been", "can", "may", "not", "its", "their",
    "into", "than", "then", "they", "such", "also", "more", "use", "uses", "used",
    "based", "toward", "towards", "via", "our", "your", "his", "her",
}

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_ZH_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _tokens_for_overlap(text: str) -> set[str]:
    """把一段文本切成用于比对的实义词集合。

    英文取整词（长度 ≥3），中文按二字滑窗切——不引分词库，
    因为这里只求「有没有沾边」，粗糙的双字切分已经够用，且零依赖。
    """
    out: set[str] = set()
    for w in _EN_WORD_RE.findall(text or ""):
        wl = w.lower()
        if wl not in _STOP_EN:
            out.add(wl)
    for seg in _ZH_WORD_RE.findall(text or ""):
        for i in range(len(seg) - 1):
            bigram = seg[i : i + 2]
            if bigram not in _STOP_ZH:
                out.add(bigram)
    return out


def _sentence_for(text: str, pos: int) -> str:
    """取出 pos 所在的句子（引用标记往往贴在句尾，所以要往前找句首）。"""
    start = 0
    for m in _SENT_SPLIT_RE.finditer(text or "", 0, pos):
        start = m.end()
    m = _SENT_SPLIT_RE.search(text or "", pos)
    end = m.start() if m else len(text or "")
    return (text or "")[start:end]


def _candidate_corpus(c: lit.Candidate) -> set[str]:
    """一篇候选文献可用于比对的词集合：标题 + 摘要 + 作者 + 年份 + 预置别名。"""
    parts = [c.title or "", c.abstract or "", " ".join(c.authors or [])]
    if c.year:
        parts.append(str(c.year))
    tokens = _tokens_for_overlap(" ".join(parts))
    # 英文标题与中文正文天然不共享字面，靠一张小别名表把常见概念的英中打通。
    # 只收高频、无歧义的概念词，宁可漏几个也不要滥加导致误判为「沾边」。
    low = " ".join(parts).lower()
    for en, zh in _CONCEPT_ALIAS:
        if en in low:
            tokens.update(zh)
    return tokens


#: 英文概念 → 中文表述（双字词）。中文正文与英文标题词面不交叉，
#: 没有这张表时「基于 Transformer 的自注意力[C1]」这种正确引用会被误判成可疑。
_CONCEPT_ALIAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("attention", ("注意", "留意", "关注")),
    ("self-attention", ("自注", "注意")),
    ("transformer", ("变换", "架构", "模型")),
    ("language model", ("语言", "模型")),
    ("question answer", ("问答", "提问", "回答")),
    ("long document", ("长文", "文档", "长篇")),
    ("sparse", ("稀疏", "稀化")),
    ("memory", ("记忆", "存储")),
    ("retrieval", ("检索", "召回")),
    ("generation", ("生成", "产生")),
    ("agent", ("智能", "代理")),
    ("hierarch", ("层级", "分层", "层次")),
    ("reasoning", ("推理", "推断")),
    ("fine-tun", ("微调", "调优")),
    ("pre-train", ("预训", "预练")),
    ("complexity", ("复杂", "开销")),
    ("efficien", ("效率", "加速")),
    ("visual", ("视觉", "图像")),
    ("multimodal", ("多模", "模态")),
    ("benchmark", ("基准", "评测")),
    ("dataset", ("数据", "集合")),
    ("evaluation", ("评估", "评价", "评测")),
    ("embedding", ("嵌入", "向量")),
    ("token", ("标记", "词元")),
)


def check_citations(text: str, pool: list[lit.Candidate]) -> CitationCheck:
    """抓出正文里所有引用标记，分类成「池内」「池外」「可疑」。

    池外的 key 意味着模型编了候选池里没有的编号——这是我们要暴露出来的问题，
    不能悄悄删掉当作没发生。

    池内但「可疑」的意思是：编号本身合法，可这句话讲的跟那篇论文压根不是一回事。
    典型成因是模型想引用一篇候选池里没有的经典论文（如 Switch Transformer），
    于是硬塞一个看着像的编号。只查编号在不在池里抓不到这种错，
    所以额外做一次词面重叠检查：引用所在句与论文标题/摘要零重叠，判为可疑。

    **References 段要排除在「可疑」判定之外**：模型常在末尾自己写一个
    ``## References``，按行罗列 ``[C1]`` ``[C2]``——这些标记前面紧挨换行，
    取句子会拿到空串（或拿到下一行列出的另一篇编号），重叠必然为 0，
    于是整篇的真实引用会被集体误判成可疑（实测 8/8 全中）。
    那段本来就是编号列表，没有论述可校验；编号合法性由 cited/unknown 覆盖。
    """
    known = {c.key for c in pool}
    found: list[str] = []
    for group in _MARK_GROUP_RE.findall(text or ""):
        for k in re.split(r"[,;，、]", group):
            k = k.strip()
            if k and k not in found:
                found.append(k)

    cited = [k for k in found if k in known]
    unknown = [k for k in found if k not in known]
    unused = [c.key for c in pool if c.key not in cited]

    body, _ref_head, ref_tail = _split_references(text or "")
    # 有 References 标题时只校验前半段；没有时整篇都算正文
    prose = body if ref_tail or _ref_head else (text or "")
    # 编号列表行（整行只有标记和空白/逗号）一律不参与词面重叠判定
    prose = re.sub(r"(?m)^[\s\[,;，、C\d】\]]+$", "", prose)

    by_key = {c.key: c for c in pool}
    suspicious: list[str] = []
    for m in _MARK_GROUP_RE.finditer(prose):
        sentence = _sentence_for(prose, m.start()).strip()
        # 取不到所在句子就无从判断，不能凭「空句子零重叠」定罪
        if not sentence:
            continue
        sent_tokens = _tokens_for_overlap(sentence)
        # 同一句里可能有 [C1, C4]，逐个判：只要这篇沾边就不算它可疑
        for k in re.split(r"[,;，、]", m.group(1)):
            k = k.strip()
            c = by_key.get(k)
            if not c or k in suspicious:
                continue
            overlap = len(sent_tokens & _candidate_corpus(c))
            if overlap < _SUSPICIOUS_MIN_OVERLAP:
                suspicious.append(k)

    return CitationCheck(
        cited_keys=cited,
        unknown_keys=unknown,
        unused_candidates=unused,
        suspicious_keys=suspicious,
    )


def normalize_headings(text: str) -> str:
    """给模型漏写的 Markdown 标题补上 `## ` / `### `。

    为什么需要这一步：prompt 里要求用 `## Abstract` 这种格式输出，实际生成时模型
    经常只写裸的 `Abstract` / `Introduction` / `Related Work` / `References`。
    前端渲染靠 `^## ` 识别标题，裸标题会被当成普通段落——整篇看起来就没有章节结构了。
    与其在前端猜「这一行是不是标题」，不如在后端按已知的章节名补井号，
    因为章节名是我们在 prompt 里规定死的，不存在歧义。

    只处理「整行恰好是章节名」的情况，避免误伤正文里提到这些词的地方。
    """
    if not text:
        return ""
    level2 = ("Abstract", "Introduction", "Related Work", "Method", "Methods", "Experiments",
              "Results", "Discussion", "Conclusion", "References")
    pat = re.compile(
        r"^[ \t]*(?:" + "|".join(re.escape(n) for n in level2) + r")[ \t]*[:：]?[ \t]*$",
        re.MULTILINE,
    )
    text = pat.sub(lambda m: "## " + m.group(0).strip().rstrip(":：").strip(), text)
    # `## ## Abstract` 这种补了两遍的情况归一化一次
    return re.sub(r"^##[ \t]+##[ \t]+", "## ", text, flags=re.MULTILINE)


def _bare_name(filename: str) -> str:
    """`1706.03762v1.pdf` → `1706.03762v1`，用作正文里的可读标签。"""
    name = (filename or "").strip()
    for suf in (".pdf", ".PDF"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def annotate_citations(text: str, pool: list[lit.Candidate]) -> str:
    """把正文里的 `[C5]` 就地改写成 `[C5·HierDoc]` 这样的带名形式。

    为什么在后端做、而不是前端渲染时临时拼：
    用户会复制正文去改、贴进 Word 或别的编辑器。如果论文名只存在于前端渲染结果里，
    一复制就回到只有编号，错配的引用又变得看不出来了。写进正文本身才留得住。

    只改写正文部分。`## References` 之后的编号列表故意保持纯编号：
    那一节紧跟着代码生成的真实条目，名字就在下一行，再摊一遍纯属噪音。

    简称取「冒号前的部分」，没冒号取前两个词。中文标题（Crossref 那批）不按词数截，
    因为中文没有空格，取两个「词」等于整句；改为按字数截。本库论文额外附带文件名，
    它是唯一能直接落到本机 PDF 上的标识。拿不到标题就只写编号，绝不编名字。
    """
    if not text:
        return ""
    by_key = {c.key: c for c in pool}

    # References 节整体跳过，只对前面那部分做替换
    head, sep, tail = _split_references(text)

    def label(c: lit.Candidate) -> str:
        title = (c.title or "").strip()
        if not title:
            return ""
        colon = re.search(r"[:：]", title)
        if colon and colon.start() > 0:
            short = title[: colon.start()].strip()
        elif re.search(r"[\u4e00-\u9fff]", title):
            short = title if len(title) <= 18 else title[:17].rstrip() + "…"
        else:
            short = " ".join(title.split()[:2])
        if len(short) > 22:
            short = short[:21].rstrip() + "…"
        bare = _bare_name(c.local_file)
        return f"{short}·{bare}" if bare else short

    def repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        keys = [k.strip() for k in re.split(r"[,;，、]", inner) if k.strip()]
        out: list[str] = []
        for k in keys:
            c = by_key.get(k)
            name = label(c) if c else ""
            out.append(f"{k}·{name}" if name else k)
        return "[" + ", ".join(out) + "]"

    return _MARK_GROUP_RE.sub(repl, head) + sep + tail


_CLAIM_MARK_RE = re.compile(r"\[\s*C\d{1,3}(?:[^\]]*)?\]")

_CLAIM_TO_EN_SYSTEM = (
    "你是学术论文的英文回译助手。用户会给你一句中文的论文陈述，"
    "请你把它回译成英文，用来在一篇英文学术论文的 PDF 里做逐字搜索。\n"
    "规则：\n"
    "1. 输出**只有英文译文**，不要解释、不要加引号、不要输出原文。\n"
    "2. 译文要贴近英文学术论文的**常见原始措辞**，不要为了通顺去意译或润色。"
    "学术写法优先：用 differs from / compared with / suffers from / we propose 这类固定表达。\n"
    "3. 保留专业术语的英文原词（attention、Transformer、retrieval-augmented generation 等），"
    "不要把术语译成描述性短语。\n"
    "4. 句子只保留最核心的一个论断，删掉排比和修饰从句，"
    "但要保证被 PDF 搜到的是完整的短语而不是零散单词。\n"
    "5. 如果输入不是一个可翻译的陈述句，输出空字符串。"
)


def _strip_claim_markers(text: str) -> str:
    """去掉引用标记，只留送给翻译的实义文本。

    引用标记形如 `[C5]` 或 `[C5·HierDoc]`。留在句子里会让模型去翻译
    「C5·HierDoc」这种人名/编号，既浪费 token 又污染译文。
    另外去掉行首的 Markdown 标记（`## `、`- `、`1. `），否则译文会带出这些符号，
    拿去 PDF 里搜自然搜不到。
    """
    out = _CLAIM_MARK_RE.sub("", text or "")
    out = re.sub(r"^\s*#{1,6}\s*", "", out)
    out = re.sub(r"^\s*[-*+]\s+", "", out)
    out = re.sub(r"^\s*\d{1,3}[.)]\s+", "", out)
    return out.strip()


async def claim_to_english(text: str) -> str:
    """把一句中文的论文陈述回译成英文，供阅读器在英文 PDF 里逐字搜索。

    为什么需要它：写作助手生成的正文是中文，而本库的论文 PDF 是英文原文。
    点「读原文」时如果直接拿中文句去搜，命中率是 0——正文语言和原文语言不通。
    退而求其次用论文标题里的词定位，高亮落在标题上，等于没告诉用户
    「这篇论文的哪一部分支撑了那句话」。所以这里先回译再搜。

    刻意要求「贴近原始措辞、不要润色」：目标是字面匹配，宁可译文生硬一点。
    纯数字/空串/翻不出东西时返回空串，调用方据此退回标题定位，不能让跳转失败。
    """
    raw = _strip_claim_markers(text or "")
    # 少于 8 个字符的多半是「如图 1 所示」「见上文」这类无实义残句，翻出来也是废话
    if len(raw) < _MIN_CLAIM_CHARS:
        return ""
    try:
        answer = await chat(
            [
                {"role": "system", "content": _CLAIM_TO_EN_SYSTEM},
                {"role": "user", "content": raw},
            ],
            temperature=0.0,
        )
    except Exception as exc:  # 翻译是锦上添花，失败必须退回标题定位而不是报错
        logger.warning("引用句回译失败，退回标题定位: %s", exc)
        return ""
    line = (answer or "").strip().splitlines()[0] if (answer or "").strip() else ""
    line = line.strip().strip('"').strip("'").strip()
    # 模型偶尔会回「英文（中文解释）」或加前缀，只取纯 ASCII 主体
    if not re.search(r"[A-Za-z]{3,}", line):
        return ""
    if len(line) > _MAX_CLAIM_CHARS:
        line = line[:_MAX_CLAIM_CHARS].rstrip()
    return line


def _split_references(text: str) -> tuple[str, str, str]:
    """把正文切成「References 之前」「References 标题本身」「之后」三段。"""
    m = re.search(r"^##[ \t]*References[ \t]*$", text, flags=re.MULTILINE)
    if not m:
        return text, "", ""
    return text[: m.start()], text[m.start() : m.end()], text[m.end() :]


def _format_authors(authors: list[str]) -> str:
    """参考文献里的作者串。超过 MAX_REF_AUTHORS 位截断加 et al.。"""
    names = [a.strip() for a in (authors or []) if a and a.strip()]
    if not names:
        return "作者信息缺失"
    if len(names) <= MAX_REF_AUTHORS:
        return ", ".join(names)
    return ", ".join(names[:MAX_REF_AUTHORS]) + ", et al."


def format_reference(c: lit.Candidate) -> str:
    """按候选的真实元数据生成一条参考文献。**只有这里能产生条目**。

    作者按学术惯例截断：超过 MAX_REF_AUTHORS 位就只留前几位加 et al.。
    T5 有 12 位作者、RWKV 有十几位，全列出来一条参考文献能占三行，
    既不像论文也不便读；截断是 IEEE/ACM 都接受的做法。
    """
    authors = _format_authors(c.authors)
    year = c.year if c.year else "n.d."
    parts = [f"{authors}", f"{c.title}", str(year)]
    if c.venue:
        parts.append(c.venue)
    if c.source == lit.SOURCE_ARXIV and c.arxiv_id:
        parts.append(f"arXiv:{c.arxiv_id}")
    elif c.doi:
        parts.append(f"https://doi.org/{c.doi}")
    # 片段自己可能已以句点结尾（"et al." / "n.d."），再拼 "." 会成 "et al.."，
    # 所以先把末尾句点去掉再统一按「. 」连接，最后补一个句点收尾。
    cleaned = [p.rstrip(". ") for p in parts if p]
    return ". ".join(cleaned) + "."


def build_reference_list(cited: list[str], pool: list[lit.Candidate]) -> list[dict]:
    """按模型引用的顺序生成参考文献条目，每条附来源标记。"""
    by_key = {c.key: c for c in pool}
    out: list[dict] = []
    for k in cited:
        c = by_key.get(k)
        if c is None:
            continue
        out.append(
            {
                "key": k,
                "text": format_reference(c),
                "source": c.source,
                "title": c.title,
                "year": c.year,
                "arxiv_id": c.arxiv_id,
                "doi": c.doi,
                "url": c.url,
                "local_file": c.local_file,
            }
        )
    return out


# --------------------------------------------------------------------------- 主流程


# 中文虚词表：从检索词里剥掉这些，剩下的才是实义词块。
# 之所以列成表而不是用分词库，是因为按字数硬切会把「的论文推荐系统」这种
# 虚词打头的碎片当成检索词（旧实现就是按 4-12 字硬切，实测对 Crossref 全废）。
_ZH_FUNCTION_WORDS = (
    "想做一个",
    "我想",
    "我们",
    "想做",
    "希望",
    "能够",
    "可以",
    "基于",
    "通过",
    "利用",
    "使用",
    "采用",
    "本文",
    "这个",
    "一种",
    "一个",
    "进行",
    "实现",
    "以及",
    "并且",
    "同时",
    "因此",
    "从而",
    "用于",
    "关于",
    "对于",
    "在",
    "的",
    "和",
    "与",
    "及",
    "让",
    "把",
    "被",
    "对",
    "从",
    "为",
    "就",
)

# 切分中文短语的分隔符：标点 + 上面这些虚词（当作切点）
_ZH_SPLIT_RE = re.compile(
    r"[，。；：、！？,.;:!?\s（）()【】\[\]\"'“”‘’]+|"
    + "|".join(re.escape(w) for w in _ZH_FUNCTION_WORDS)
)


def _tokens_of(idea: str, limit: int = 3) -> list[str]:
    """从 idea 里切出能拿去检索的词。

    不做真正的关键词抽取（那又要一次 LLM 调用，慢且不稳）。
    做法分两路：

    1. **英文词组**：取长度 >= 3 的英文连续词组（含连字符），这类词检索最准。
    2. **中文实义词块**：先按标点和虚词（的/基于/可以…）切开，再丢掉不足 2 字
       的碎片。切完之后每块都是一个完整的名词短语，而不是从前一个虚词尾巴
       上截下来的半句话。

    旧实现用 ``re.findall(r"[\\u4e00-\\u9fff]{4,12}", idea)`` 按字数硬切，
    「想做一个基于引用关系图谱的论文推荐系统」会被切成
    ``['想做一个基于引用关系图谱', '的论文推荐系统', ...]``——第二段以「的」
    开头，拿去查 Crossref 只能靠字面命中，捞回来的还是别的领域的老论文。

    实测「attention mechanism transformer」这类组合检索效果够用；
    真要更准，后面可以让 LLM 出查询词，但那是可选优化。
    """
    idea = idea or ""
    q: list[str] = []
    # 英文词组（含连字符），取长度 >= 3 的
    for m in re.findall(r"[A-Za-z][A-Za-z\-]{2,}(?:\s+[A-Za-z][A-Za-z\-]{2,}){0,3}", idea):
        q.append(m.strip())
    # 中文实义词块：先去虚词、再按标点切。
    # 门槛设 3 字：2 字块大多是「用户」「改进」「系统」这类泛词或动词，
    # 拿去检索只会把结果拉向无关领域，宁可不要。
    for piece in _ZH_SPLIT_RE.split(idea):
        piece = piece.strip()
        # 只留纯中文块；纯数字/字母块上一步已经收过了
        if len(piece) >= 3 and re.fullmatch(r"[\u4e00-\u9fff]+", piece):
            q.append(piece)
    # 去重保序
    seen: list[str] = []
    for x in q:
        if x and x not in seen:
            seen.append(x)
    if not seen and idea.strip():
        seen = [idea.strip()[:40]]
    return seen[:limit]


def extract_idea_queries(idea: str) -> list[str]:
    """对外暴露的查询词抽取（测试用）。"""
    return _tokens_of(idea)


_ZH_QUERY_PROMPT = (
    "把下面这组中文检索词翻译成英文检索词，用于检索 arXiv。"
    "要求：只输出英文，词与词之间用英文逗号分隔，不要编号、不要解释、不要中文。\n\n"
    "**每条必须是论文标题里会出现的核心术语**，2-4 个词，例如：\n"
    "  引用图谱 → citation graph\n"
    "  检索增强生成 → retrieval augmented generation\n"
    "  多智能体校验 → multi-agent verification\n"
    "  幻觉缓解 → hallucination mitigation\n\n"
    "**严禁**翻译成带修饰语的整句描述，下面这些是错误示范：\n"
    "  学术问答系统 → academic question answering system ❌（「学术」是修饰语，进检索必然 0 条）\n"
    "  引入引用图谱约束 → introducing citation graph constraints ❌（动词+修饰语，不是术语）\n"
    "  降低幻觉 → reduce hallucination ❌（动词短语，术语应为 hallucination mitigation）\n\n"
    "判据：这个词组应该能直接出现在某篇论文标题里。宁可短，不要长。\n\n"
    "{queries}"
)


def _english_exprs(queries: list[str]) -> list[str]:
    """把英文检索词拼成若干条 arXiv 布尔表达式（每个是 2 个概念的 AND）。

    为什么不能拼成散词串：``_arxiv_expr`` 会按词切、只取前 5 个实词，
    中文 idea 翻出来的 ``retrieval-augmented generation academic question answering``
    会被截断成前 5 个词，把 ``citation`` / ``graph`` 这两个最关键的词直接丢掉。

    为什么是**多条**而不是一条：实测同一课题（GOOD = CG-RAG / GRAG / SciRAG /
    LitFM / VeriCite，hit = 结果里 GOOD 的篇数）——

        all:"retrieval augmented generation" AND all:"question answering"  → 10 条, hit 0
        all:"retrieval augmented generation" AND all:"citation graph"      →  5 条, hit 3
        all:"question answering" AND all:"citation graph"                  →  3 条, hit 1
        上面任一条再加 all:"hallucination"                                  →  0 条, hit 0

    也就是说「哪两个概念配成一对」决定了成败，而模型给出的概念顺序是随机的
    （它不知道哪个有区分度）。只拼前 2 个等于掷骰子：实测命中 0。
    所以把所有两两组合都拼出来，每条各查一次，让 RRF 按实际召回结果排序——
    命中的组合自然会把正主顶到前面，打空的组合贡献为空、不占名额。

    为什么上限是 2 个概念而不是 3 个：三概念几乎必然全灭（上面实测 0 条）。
    偶尔 3 概念能捞出 1 条正主，但那是运气，不值得为它牺牲其余 19 个组合。
    """
    phrases: list[str] = []
    seen: set[str] = set()
    for piece in queries:
        p = piece.strip().strip("`'\".,;:")
        # 连字符归一成空格：arXiv 的短语匹配对 ``retrieval-augmented`` 和
        # ``retrieval augmented`` 处理不一致——实测前者 0 条、后者 5 条（命中 3）。
        # 这个差异不好从文档里看出来，只能靠实测撞出来。
        p = re.sub(r"[\s\-‐-―]+", " ", p).strip()
        if not p or not re.search(r"[A-Za-z]{2,}", p):
            continue
        if p.lower() in _EN_QUERY_STOPWORDS or p.lower() in seen:
            continue
        seen.add(p.lower())
        phrases.append(p)

    if len(phrases) < 2:
        # 只有一个可用概念时退化成单概念查询（AND 需要两个操作数）
        return [f'all:"{p}"' for p in phrases[:1]]
    pairs = list(itertools.combinations(phrases[:_PAIR_SOURCE_MAX], 2))
    return [' AND '.join(f'all:"{p}"' for p in pair) for pair in pairs]


# 参与两两配对的概念上限。概念数 n 的组合数是 C(n,2)：
# 4 个概念 6 条、5 个概念 10 条。每条 arXiv 请求约 1.6 秒，10 条约 16 秒——
# 所以截到 4 个概念（6 条），既能覆盖主要组合，又不让生成慢到不可用。
_PAIR_SOURCE_MAX = 4


# 单个概念上限的由来（保留作参考：为什么不是 3 个概念一条）：
#   2 概念  all:"retrieval augmented generation" AND all:"citation graph"  →  5 条 / 命中 3
#   3 概念  + all:"hallucination"                                          →  0 条 / 命中 0
#   3 概念  + all:"academic question answering"                            →  0 条 / 命中 0
#   3 概念  + all:"citation graph constraint"（constraint 是多出来的词）     →  0 条 / 命中 0
_CONCEPT_MAX = 2


async def _english_queries(queries: list[str]) -> list[str]:
    """把中文检索词翻成英文，用于补检索。全英文或翻译失败时返回空列表。

    arXiv 和 Crossref 的索引都不做中文分词：实测「引用关系图谱」「论文推荐系统」
    「文献推荐」三个查询在 Crossref 返回的是同一批无关结果，arXiv 返回 0 条，
    而对应的英文 ``citation graph recommendation`` 能打到高相关文献。
    所以中文 idea 必须多一条英文检索路径，否则候选池里只有标题带「图」字的
    数学论文，模型照着引用就是引用错文献——比不引用更糟。

    翻译时**额外产出一条概念合并查询**（排在第 1 条），这是实测出来的关键：
    逐词翻译出来的英文查询彼此不重叠，``retrieval-augmented generation`` 单独查
    arXiv 只会回泛 RAG 论文，``citation graph constraint`` 单独查只会回引用网络分析，
    两边都打不到「用引用图谱约束 RAG」的论文。而把核心概念拼成布尔表达式能命中
    （实测 ``all:"retrieval augmented generation" AND all:"citation graph"``
    一次捞出 GRAG / CG-RAG / SciRAG）。

    注意**不是**把概念串成一条自然语言句子：句子会被 ``_arxiv_expr`` 按词切、
    只留前 5 个实词，把关键的 ``citation`` / ``graph`` 截掉。拼表达式
    （见 ``_english_expr``）能保住短语，打空时还有 ``_relax_api_expr`` 逐级放宽。
    多出来的这一列交给 RRF 融合，不会挤掉其他查询的结果。

    这是**增强**不是必需：模型不可用、超时、返回中文都当失败处理，
    返回空列表让主流程退回纯中文检索。
    """
    zh = [q for q in queries if re.search(r"[\u4e00-\u9fff]", q)]
    if not zh:
        return []
    try:
        raw = await chat(
            [{"role": "user", "content": _ZH_QUERY_PROMPT.format(queries="\n".join(zh))}],
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 - 检索增强，失败就退回中文检索
        logger.warning("1.6.1 检索词英译失败，只用中文词: %s", exc)
        return []

    out: list[str] = []
    for piece in re.split(r"[,，;；\n]+", raw or ""):
        # 模型常带编号/项目符号（"1. citation graph"、"- citation graph"），
        # 不剥掉会变成检索词的一部分，把 arXiv 查询打空。
        piece = re.sub(r"^\s*(?:\d{1,2}\s*[.、)）]|[-*•·])\s*", "", piece)
        piece = piece.strip().strip("`'\"。.、 ")
        # 只要纯英文短语：含中文说明模型没按要求输出，直接丢
        if not piece or re.search(r"[\u4e00-\u9fff]", piece):
            continue
        if not re.search(r"[A-Za-z]{3,}", piece):
            continue
        if len(piece) > 120 or piece in out:
            continue
        out.append(piece)
    merged = _english_exprs(out)
    # 多概念各自检索 + 两两概念组合，两条路都要走。
    #
    # 为什么不能只靠单概念：实测「retrieval augmented generation」单独查 10 条，
    # 命中正主 0 篇（全是 Ragas / FAIR-RAG 这类泛 RAG）；「citation graph」单独查
    # 10 条，命中 0 篇（全是引用网络分析）。正主标题里**同时**有两个词，
    # 单查任何一个都被泛论文淹没。
    # 为什么不能只靠组合：组合很脆弱，概念选错就全灭（见 _english_exprs 注释）。
    # 两条路一起走，概念对组合保精度、单概念保广度，交给 RRF 融合排序。
    # 这也是 GPT Researcher / STORM 的做法——从不指望一条查询打中正主，
    # 而是出多条查询、广收结果、让下游挑。
    return (merged + out)[:_MAX_QUERIES]


# 英文查询总数上限。每个查询要打 arXiv + Crossref + OpenAlex 三个外部源。
# 实测每条约 1.6 秒，7 条约 11 秒，是可接受的上限。
_MAX_QUERIES = 7


# 拼合并查询时要扔掉的英文虚词/空泛词（这些单独当概念 AND 进表达式只会全灭）
_EN_QUERY_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "these", "those",
        "using", "based", "via", "into", "toward", "towards", "their", "its",
        "are", "was", "were", "have", "has", "been", "can", "may", "not",
        "system", "systems", "approach", "method", "methods", "study", "framework",
        "paper", "research", "analysis", "based-on",
    }
)


async def generate_framework(idea: str, total_candidates: int = 32) -> dict[str, Any]:
    """1.6.1 论文框架生成：检索 → 约束生成 → 校验 → 拼装参考文献。

    返回结构直接给前端：
        {
          "idea": ...,
          "queries": [...],          # 实际用来检索的词
          "candidates": [...],       # 候选池（每篇真实）
          "markdown": "...",         # 模型正文（可能含悬空引用，原样保留）
          "references": [...],       # 由代码生成，模型碰不到
          "check": {...},            # 校验结果
        }
    """
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("idea 不能为空")

    queries = _tokens_of(idea)
    logger.info("1.6.1 检索词: %s", queries)

    # 中文 idea 的检索词打不进 arXiv/Crossref：实测三个不同的中文查询词
    # （引用关系图谱 / 论文推荐系统 / 文献推荐）在 Crossref 返回的是同一批完全无关的
    # 结果，arXiv 更是 0 命中——两个源的索引都不做中文分词。于是候选池里只剩
    # 「关于…的一个猜想」这类标题里带「图」字的数学论文，模型引用它们等于引用错文献。
    # 补救：把中文检索词翻成英文再检索一遍，英文查询实测能打到高相关文献。
    # 翻译失败不影响主流程（返回 None 就只用中文词），因为这是增强而非必需。
    english = await _english_queries(queries)
    if english:
        logger.info("1.6.1 英文检索词: %s", english)
        queries = queries + english

    pool = lit.build_candidates(queries, total_limit=total_candidates)

    # 用本库 PDF 的正文补证据（app/writing_evidence.py）。**必须在 pool_coverage_gaps
    # 和空池早退之前**：补候选的意义就是「元数据检索一篇本库论文都没捞到时，
    # 正文检索还能捞到几篇」，放到早退后面就永远不会执行。
    evidence_info = await _attach_local_evidence(idea, queries, english, pool, total_candidates)

    gaps = lit.pool_coverage_gaps(idea, pool)
    if gaps:
        logger.info("1.6.1 候选池覆盖缺口: %s", gaps)

    if not pool:
        # 一个候选都没有时不能硬生成——那样必然编造。如实返回。
        return {
            "idea": idea,
            "queries": queries,
            "candidates": [],
            "markdown": "",
            "references": [],
            "check": {
                "cited_keys": [],
                "unknown_keys": [],
                "unused_candidates": [],
                "suspicious_keys": [],
                "ok": False,
            },
            "evidence": evidence_info,
            "warning": "没有检索到任何真实文献，为避免编造引用，未生成框架。请换个更具体的 idea。",
        }

    messages = build_framework_prompt(idea, pool, gaps)
    markdown = await chat(messages, temperature=0.4)

    # 先校验再改写：check_citations 认的是 `[C5]` 这种纯编号，
    # annotate 之后变成 `[C5·HierDoc]`，正则虽然兼容但从原始文本判定最稳。
    check = check_citations(markdown, pool)
    markdown = normalize_headings(markdown)
    markdown = annotate_citations(markdown, pool)
    references = build_reference_list(check.cited_keys, pool)

    return {
        "idea": idea,
        "queries": queries,
        "candidates": [c.to_dict() for c in pool],
        "markdown": markdown,
        "references": references,
        "check": {
            "cited_keys": check.cited_keys,
            "unknown_keys": check.unknown_keys,
            "unused_candidates": check.unused_candidates,
            "suspicious_keys": check.suspicious_keys,
            "ok": check.ok,
            "coverage_gaps": gaps,
        },
        "evidence": evidence_info,
    }


async def _attach_local_evidence(
    idea: str,
    tokens: list[str],
    english: list[str],
    pool: list[lit.Candidate],
    total_candidates: int,
) -> dict[str, Any]:
    """把本库 PDF 的正文证据接进候选池（原地改 ``pool``）。

    ``retrieve_scored`` 是同步的、内部要读 chroma 并跑重排模型，
    直接 ``await`` 里调用会把事件循环整个卡住（最坏 2 秒），所以丢线程池。

    这一步是**增强不是必需**：任何异常都吞掉并记 warning，退回「只有元数据候选」
    的老行为——总比整个框架生成失败强。
    """
    from app import writing_evidence as we

    try:
        info = await run_in_threadpool(
            we.promote_from_evidence,
            idea,
            english,
            pool,
            total_candidates,
            tokens,
        )
    except Exception as exc:  # noqa: BLE001 - 证据层失败不能拖垮主流程
        logger.warning("本地正文证据层失败，退回纯元数据候选: %s", exc)
        return {"status": "error", "error": str(exc)}
    logger.info(
        "1.6.1 本地证据: status=%s 补入=%d 池内已有=%d 命中论文=%d 过闸块=%d",
        info.get("status"),
        len(info.get("promoted") or []),
        (info.get("gates") or {}).get("papers_in_pool", 0),
        (info.get("gates") or {}).get("papers_seen", 0),
        (info.get("gates") or {}).get("passed", 0),
    )
    return info


# --------------------------------------------------------------------------- 导出给测试


def _dump_pool(pool: list[lit.Candidate]) -> str:
    """调试用：把候选池打成一行 JSON，方便人在日志里看。"""
    return json.dumps([c.to_dict() for c in pool], ensure_ascii=False)[:2000]