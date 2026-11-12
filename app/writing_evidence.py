"""1.6.1 写作助手的**本地正文证据**层：把本库 PDF 的正文块接进候选池。

## 为什么需要这一层

``app/literature.py`` 的 ``build_candidates`` 只做**元数据检索**：``search_local``
拿 idea 的词去匹配 catalog 的 title/topic/category，命中的论文才有资格进池。
实测一条真实 idea（「用引用图谱约束的检索增强生成方法」）下：

    search_local('citation graph retrieval augmented generation') -> 0 篇

也就是说本库 40 篇论文一篇都没进候选池——**本地知识库在写作链路里完全没被用上**。
而同一时间 ``app/rag/pipeline.py`` 的 ``retrieve_scored`` 能在正文里召回到
十几篇本库论文的段落。两边信息不通，模型自然看不到自己库里的原文。

这一层就是那个「通」：正文检索命中 → 反查 catalog → 补一个 ``Candidate`` →
把命中的块挂成 ``evidence``。**只补候选和证据，不改任何检索算法**。

## 三道门（为什么不能直接拿 top_k 命中当候选）

实测 ``retrieve_scored`` 返回的 ``score`` 衡量的是「这段文字**像不像**查询词」，
不衡量「支撑研究议题」。真实反证（top_k=12，query=``retrieval augmented generation
literature review``）：

    score=4.29  'Riedel, and Kiela Douwe.\\nRetrieval-augmented generation for '  ← 被切断的参考文献
    score=4.24  'S. Krishna, K. Krishna, A. Mohananey, S. Schwarcz, ...'          ← 作者名单续行
    score=3.02  'Table 24| FRAMES (Factuality, Retrieval, And reasoning ...)'     ← 表格残片

拿 top_k 直接补，实测会塞进 13 篇，绝大多数是这几种噪声。所以补之前过三闸：

* **G1 引用块**：``"citation" in channels`` ∨ ``pipeline._looks_like_citation(text)``
  ∨ ``_looks_like_ref_fragment(text)``。三个判据并联的理由：pipeline 的判据是
  「短块 + ≥2 个引用格式特征」，而**分块切割会切断参考文献条目**，断完之后
  ``et al.`` / ``arXiv:`` / ``NeurIPS`` 都落在邻块里，``citation`` 标记就漏了。
  实测那条 4.29 分的 top1 **没有** ``citation`` 标记，正是这个原因。
* **G2 文本质量**：长度下限 + 字母占比 + 换行密度。挡表格残片、纯数字行。
* **G3 议题相关**：该块必须真的谈到 idea 里的概念。判据见 ``_g3_verdict``。

门按 G1 → G2 → G3 **固定顺序短路求值**，命中第一个不通过的门就 return——
否则一个真残块会同时被三座门计数，台账就解释不了「这篇为什么没进池」。

## 诚实的边界

这一层解决的是「本地正文没被使用」，**不解决「本地没有正主文献」**。
本库 40 篇里没有 CG-RAG / GRAG / SciRAG 这类直接正主的论文；补进来的多半是
「议题相关但方法不同」的论文（例如 GPT-3 里讲 RAG 的那段）。它能给模型提供
真实的原文出处，但**不构成逐论点的语义验证**——门判的是「这块文本谈到这个话题」，
不是「这块文本支持模型要写的那句话」。
"""

from __future__ import annotations

import re
from typing import Any

from app import literature as lit
from app.logging_config import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------- 常量

#: 全局英文检索最多打几条 query。每条一次 retrieve_scored，实测热进程约 300~500ms。
GLOBAL_QUERY_LIMIT = 3
#: 全局检索每条 query 取几块。pipeline 内部 ``_MAX_PER_SOURCE=2`` 会把同篇限流，
#: 所以 12 块通常落到 8~10 篇论文上。
GLOBAL_TOPK = 12
#: 逐篇补检时每篇取几块（source 已限定，pipeline 不做限流）
PER_PAPER_TOPK = 2
#: 每篇候选最多挂几条证据交给模型
MAX_EVIDENCE_PER_PAPER = 3
#: 补进候选池的篇数上限 = 原池容量的四分之一（32 → 8）。
#: 不扩大到补满：名额是外部检索和本地正文共享的，全给本地会把外部文献挤没。
MAX_PROMOTED_DIVISOR = 4

#: G2：真实 chunk 长度实测 n=72，min=112 / median=228 / max=298（见分块上限 300）。
#: 门槛定 120：挡掉「作者名单续行」这类被切碎的残段（实测 85~109），
#: 又不碰任何正常正文（112 是本次样本最短，仍属正文）。
MIN_TEXT_CHARS = 120
#: G2：字母（非 CJK/数字/标点）占比下限。参考文献条目里作者名多，实测 0.66~0.82，
#: 单纯靠这条挡不住；它主要挡表格残片和纯编号行。
MIN_ALPHA_RATIO = 0.45
#: G2：换行密度上限。作者名单是一行一个名字，换行密度明显高于正文段落。
MAX_NEWLINE_RATIO = 0.10
#: G2：表格/图注残片的开头特征。实测漏网的那块原文以
#: ``Table 24| FRAMES (Factuality, Retrieval, And reasoning MEasurement Set) ...``
#: 开头——它同时命中 ``retrieval`` 和 ``rag``，靠 G3 挡不住（G3 判的是「谈到没有」，
#: 而残片确实谈到了）。这类块的通病是开头就是表/图编号，正文段落不会这样起头。
_TABLE_FIGURE_HEAD_RE = re.compile(r"^\s*(?:Table|Figure|Fig\.|Algorithm|Listing)\s*\d", re.I)
#: G2：**整块就是参考文献列表**的开头特征。实测漏网的那块以
#: ``References Julien Abadji, Pedro Javier Ortiz Suárez, ...`` 开头——
#: 它是参考文献首块：以 ``References`` 起头，后面跟着**全名**作者串，
#: 所以 ``_looks_like_citation``（要 ≥2 个 ``et al.``/``arXiv:``/会议名）和
#: ``_looks_like_ref_fragment``（要缩写人名 ``S. Krishna,``）都认不出来。
#: 正文段落不会以 ``References`` 起头，所以这个判据本身极低误报。
_REFERENCE_SECTION_HEAD_RE = re.compile(
    r"^\s*(?:References|REFERENCES|Bibliography|BIBLIOGRAPHY)\b", re.I
)
#: 参考文献条目的典型形态：``名字, 名字, 和 名字. 标题. In 会议...``，
#: 一行里塞两个以上「名 姓，」并且含年份。用来补 G2 的第三条判据。
_REF_ENTRY_LINE_RE = re.compile(r"[A-Z][a-zA-Z\u00C0-\u024F\-]+,\s*[A-Z]", re.M)
#: G3 最多保留几个议题概念。
#:
#: 4 → 6（2026-11-12 第二轮）：真实 idea
#: 「用引用图谱约束的检索增强生成方法，缓解大模型幻觉并提升文献综述的可追溯性」
#: 正文上同时覆盖了引用/图谱/检索增强（3 个泛词）+ 幻觉 + 文献综述 + 可追溯。
#: 4 个名额装不下 6 个概念，无论怎么排都会丢掉一侧：丢掉泛词 → citation+graph
#: 的拒绝判定失效；丢掉主题词 → 永远凑不齐 topic_concept_with_support。
#: 概念只是「命中与否」的标记，多留两个不会放宽任何一条判定——
#: 泛词凑再多也过不了 G3，所以放宽名额没有放大风险。
MAX_CONCEPTS = 6

#: G3 概念分级用：**判断一个英文片段还剩下多少区分度**时，从里面减掉的泛词。
#: 直接复用 ``literature._GENERIC_EN``，不另起一份——两份词表迟早会走岔。
#: 现有内容：retrieval / generation / augmented / system / model / large / language。
_GENERIC_FRAG_EN = frozenset(lit._GENERIC_EN)

#: 上面的词表覆盖不到的泛词，只在**本模块的分级判定**里补进来。
#: 为什么要补：``_GENERIC_EN`` 是给「概念词是否被候选池覆盖」用的，
#: 它没把 graph / network / citation / agent 算进去，但这些东西拿来当
#: 「这篇论文跟我的选题有关」的证据同样站不住——实测 Understanding Deep
#: Learning 第 255 页讲引用网络的那句，靠 citation + graph 两个泛词
#: 就凑成了 two_concepts 被放行（2026-11-12）。
#: 逐条说明为什么算泛词：
#: - graph / network：任何图神经网络、社媒、通信网络论文都含，不指向选题；
#: - citation / cite / reference：任何论文都有一节 References；
#: - retrieval / answer / recommend / summar：通用动作词；
#: - rag：``检索增强`` 与 ``增强生成`` 共享的缩写，任何 RAG 论文都有，
#:   不能因为一段文字写了 RAG 就说它谈了你研究的选题（2026-11-12 补）；
#: - agent：多智能体、强化学习、软件工程都在用；
#: - attention / transformer：如今几乎是所有序列模型的默认组件。
_EXTRA_GENERIC_FRAG = frozenset(
    {
        "graph",
        "network",
        "citation",
        "cite",
        "reference",
        "retrieval",
        "rag",
        "answer",
        "recommend",
        "summar",
        "agent",
        "attention",
        "transformer",
    }
)

#: 判定一个英文片段是否「泛」。两个表取并集。
_GENERIC_FRAGS = _GENERIC_FRAG_EN | _EXTRA_GENERIC_FRAG

#: 专有名词的**加固黑名单**：这些词以小写形式出现在 idea 里时，虽然
#: ``_tokens_of`` 可能因为句首大写把它们提上来，但它们不是某篇具体工作，
#: 不能当作「这篇文章讲的就是我在研究的东西」的证据。
#: 与 ``_GENERIC_FRAGS`` 分开维护：这张表管的是「大小写形态」，
#: 例如 ``BERT`` / ``GPT`` 必须是全大写才算锚点，``Bert`` 这种拼写不算。
_GENERIC_PROPER = frozenset(
    {
        "attention",
        "transformer",
        "graph",
        "network",
        "rag",
        "retrieval",
        "generation",
        "citation",
        "reference",
        "agent",
        "language",
        "model",
        "system",
        "survey",
        "deep",
        "neural",
    }
)

#: 主题概念（topic_concept）词根：有研究意义，但**单独出现不足以证明**
#: 这篇论文围绕我的选题展开。
#:
#: 为什么把 hallucination 从锚点降下来（2026-11-12 第二轮）：
#: 真实索引复测发现，补进候选池的三篇（LLaVA / GPT-4 / Llama 3）**全部**只靠
#: “hallucination” 一个词过关，而它们讲的是通用幻觉缓解，不是
#: 「引用图谱约束的 RAG」。幻觉是每篇大模型论文都会提的词，
#: 它证明「相关领域」可以，证明「围绕核心选题」不够。
#:
#: 词根一律按**词边界 + 词形归一**匹配（见 ``_root_matches``），
#: 且长度 >=4，避免 ``code`` 这类短词误命中 ``encode``/``decode``。
_TOPIC_ROOTS = (
    "hallucinat",  # 幻觉
    "verif",  # 校验
    "validat",  # 校验
    "academic",  # 学术
    "scholar",  # 学术检索
    "multilingual",  # 多语言
    "trace",  # 可追溯 / 溯源（traceable / traceability 均由词形归一覆盖）
    "literature",  # 文献综述
    "survey",  # 综述
)


#: 中文主题词 → 英文词根。``_ZH_CONCEPT_MAP`` 里没有的、但真实 idea 会写的
#: 研究方向词，在这里补一条，用来把「可追溯性」这类词变成可检索的英文片段。
#:
#: 真问题（2026-11-12 第二轮）：真实 idea 的结尾是「提升文献综述的**可追溯性**」，
#: 但 ``_ZH_CONCEPT_MAP`` 只认「引用/图谱/检索增强/增强生成/幻觉」等 14 条，
#: 抽出来只有 4 个概念（3 泛 1 主题）。于是「幻觉」以外的主题概念一个都没有，
#: topic_concept_with_support 永远凑不齐——正文讲得再对也会被 topic_only 拒掉。
#: ``trace`` 同时覆盖 traceable / traceability / trace。
#:
#: **键按长度降序匹配**，长词优先：``可追溯`` 里含 ``追溯``，不先匹配长词
#: 会漏掉「可追溯性」（``可`` 不是虚词，切不出来）。
_ZH_EXTRA_TOPIC = {
    "可追溯": ("trace",),
    "追溯": ("trace",),
    "溯源": ("trace",),
    "文献综述": ("literature", "survey"),
    "多语言": ("multilingual",),
    "学术": ("academic", "scholar"),
}

#: 补充主题词的英文片段，分类时若减完泛词还剩这些，直接归 topic_concept
#: （``trace`` / ``literature`` 不在 ``_TOPIC_ROOTS`` 里也无所谓，两处都认）。
_EXTRA_TOPIC_ROOTS = ("trace", "literature", "survey", "multilingual")

#: 强锚点（strong_anchor）词根：**具体到某个模型 / 方法 / 明确命名的研究对象**，
#: 单独命中即可作为「这篇论文谈的就是我在研究的东西」的证据。
#:
#: 为什么不放 code / math / reason：这三个词指向的是任务类型而不是研究对象，
#: 「代码生成」「数学推理」各自是一个庞大的子领域，命中它们不代表围绕我的选题。
#: 实测风险：``code`` 做词根时用宽松子串匹配会命中 encode/decode，
#: 即便改成词边界，``code`` 本身在 ML 论文里的出现频率也太高。
_STRONG_ROOTS = (
    "knowledge",  # 知识图谱：整串 knowledge graph 两词都出现才算，单 graph 是泛词
    "multi-agent",  # 多智能体（agent 单独是泛词，multi- 前缀让它具体）
    "question answering",  # 问答：整段短语，不是 answer 单词
    "long document",  # 长文档 / long-document 由归一化覆盖
    "long-document",
)

#: 全部词根（两档合并），只用于「减完泛词还剩没剩东西」的判断。
_ANCHOR_ROOTS = tuple(dict.fromkeys([*_STRONG_ROOTS, *_TOPIC_ROOTS]))

#: 专有名词的**形态克制表**：这些缩写形似模型名，但含义过泛或被反复复用，
#: 不能仅凭「是全大写」就无条件当成强锚点。
#: - gpt：GPT-2/3/4、GPTs、以及大量「基于 GPT 的 XX」都在用，单写 GPT
#:   并不指向某篇具体工作；只有和别的强信号一起出现才够格（见 _concept_class）。
#: - llm / cv / nlp / ai 同理。
_AMBIGUOUS_PROPER = frozenset({"gpt", "llm", "llms", "ai", "cv", "nlp", "sota", "api"})

#: 专有名词的形态要求：全长 ≥3、至少 2 个大写字母开头的音节，
#: 或者全大写（BERT / GPT）。这条替代原来的「含一个大写字母就算」。
_PROPER_CAMEL_RE = re.compile(r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z]{2,}[a-z]?[A-Za-z0-9]*")


# --------------------------------------------------------------------------- 纯函数：文本判据

#: 缩写人名串：「S. Krishna, K. Krishna, A. Mohananey, ...」
#: 实测在 4 类引用残块里命中 2 类，在 7 类正常正文/表格/模型名正文里命中 **0** 类。
_ABBREV_NAME_RUN_RE = re.compile(r"(?:\b[A-Z]\.\s+[A-Z][a-zA-Z\-]+,\s*){2,}")

#: 中文虚词表：切中文 token 的时候当分隔符，避免切出「生成方法」这种半截词。
_ZH_SPLIT_RE = re.compile(r"[，。；：、！？,.;:!?\s（）()【】\[\]\"'“”‘’]+")

#: 中文 token 内部切四字滑窗时，这些字开头/结尾的片断多半是虚词残留，不要。
_ZH_UNIGRAM_DROP = frozenset("的大了在是和与及并或对为以于从把被实现用能可")


def _looks_like_ref_fragment(text: str) -> bool:
    """G1 的第三条判据：像不像「被切断的参考文献条目」。

    只认一种强信号——缩写人名串。为什么不用「含年份」：实测正常正文里
    出现年份是常态（"In 2020, RAG was proposed..." 一句里就有两个），
    拿年份当判据会误杀大量正文。

    **已知覆盖不到的两类**（只能靠 G2/G3 兜）：全名换行名单
    （``Xin Xie\\nXingchao Liu\\n...``）和全名逗号串（``Kelvin Guu, Kenton Lee, ...``）。
    这两类在台账里会落在 ``passed`` 里，属于已知局限。
    """
    return bool(_ABBREV_NAME_RUN_RE.search(text or ""))


def _alpha_ratio(text: str) -> float:
    """拉丁字母占比。中文块会因为 CJK 字符拉低这个值，所以只在英文块上有意义。"""
    if not text:
        return 0.0
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return latin / len(text)


def _newline_ratio(text: str) -> float:
    if not text:
        return 0.0
    return text.count("\n") / len(text)


def _looks_like_table_fragment(text: str) -> bool:
    """G2：这块是不是从表格/图注里切出来的残片。

    实测这里漏过一块，原文以 ``Table 24| FRAMES (...)`` 开头，
    只报一个事实先记下：**G3 挡不住表格残片**——残片确实「谈到了 RAG」，
    G3 判的是话题相关，不是「这段文字读得通」。
    """
    return bool(_TABLE_FIGURE_HEAD_RE.match(text or ""))


def _looks_like_reference_block(text: str) -> bool:
    """G2：这块是不是参考文献列表（首块或整页）。

    三判据任一条成立即算：

    1. 以 ``References`` / ``Bibliography`` 起头（参考文献首块）；
    2. 单行出现 ≥2 处「名 姓，」形态的姓名串——参考文献条目的标志，
       正文里人名不会这么密集地跟在逗号前面；
    3. 命中 ``_looks_like_ref_fragment`` 的缩写人名串。

    每一条都是在实测漏网样本上倒推出来的，不是先验猜测。
    """
    body = text or ""
    if _REFERENCE_SECTION_HEAD_RE.match(body):
        return True
    if len(_REF_ENTRY_LINE_RE.findall(body)) >= 2:
        return True
    return _looks_like_ref_fragment(body)


# --------------------------------------------------------------------------- 纯函数：议题词


def _concept_terms(idea: str) -> list[dict[str, Any]]:
    """从 idea 里抽出「议题概念」，每个概念带一组英文片段。

    词源是 ``literature._ZH_CONCEPT_MAP``（14 条中文→英文映射），
    按 idea 里的**首次出现位置**排序（不是按词长），最多 ``MAX_CONCEPTS`` 个。
    **不复用 ``literature._concept_terms``**：实测它对中文用的是
    ``[\\u4e00-\\u9fff]{4,}`` 贪心整段匹配，一条真实 idea 切出来是

        ['研究一种用引用图谱约束的检索增强生成方法', '缓解大模型幻觉并提升文献综述的可追溯性']

    两条 20 字的整句，拿去查英文正文逐字命中率 0。

    为什么要做概念合并：字典里 ``检索增强`` → ``(retrieval, rag)``、
    ``增强生成`` → ``(generation, rag)``，这两个中文词在「检索增强生成」里
    指的是同一件事，却各占一个名额。按位置排序后它们会紧挨着出现在前两位，
    再不做合并，后面的「幻觉」根本进不了前 4。

    合并规则：两个字典词只要**共享一个 frag**（例如都含 ``rag``）就是同一个概念。
    合并时 frags 取并集——所以合并后的「检索增强」会同时含 ``retrieval`` /
    ``rag`` / ``generation``。这是**有意的**：它代表「RAG 这件事」这一个概念，
    凡是提到其中任一写法的块都算谈到该概念，且只计一次。

    除了字典词，还会补上「英文专有名词」和「中文 token 内部的未登录短词」，
    这两类是为了让 idea 里出现 ``Mamba`` / ``FlashAttention`` 这类**字典覆盖不到**
    的关键词时，G3 仍然认得出来。

    每个概念都带 ``tier``：``"strong_anchor"``（具体模型/方法/明确命名的研究对象，
    单独可通过）、``"topic_concept"``（有研究意义但单独不够，需与其他主题概念
    共同命中）或 ``"generic"``（通用辅助概念，凑多少个都不能单独证明选题相关）。
    分类交给 ``_concept_class``，规则见那里的说明。
    """
    terms: list[dict[str, Any]] = []
    seen_zh: set[str] = set()

    # 1) 字典词：**按 idea 里首次出现的位置**排序，而不是按词长。
    #    按长度排的旧做法在真实 idea 上得到 ['检索增强','增强生成','引用','图谱']，
    #    截断到 4 个时把「幻觉」「文献综述」「可追溯性」全挤掉了——
    #    「检索增强/增强生成」本来就是同一个 RAG 的两种写法，占掉两个名额很浪费。
    #    按出现位置排则优先保留 idea 自己的叙述顺序。
    idea_text = idea or ""
    for zh in sorted(lit._ZH_CONCEPT_MAP, key=lambda w: idea_text.find(w)):
        if zh and zh in idea_text and zh not in seen_zh:
            seen_zh.add(zh)
            frags = [f for f in lit._ZH_CONCEPT_MAP[zh] if f]
            # 字典词也可能具体：``知识图谱`` 的片段是 ``knowledge graph``，
            # 减掉 graph 还剩 knowledge，够区分；而 ``图谱`` 减完就空了，只能算泛。
            tier, why = _concept_class(frags)
            terms.append({"zh": zh, "frags": frags, "kind": "mapped", "tier": tier, "why": why})

    # 2) 英文专有名词（Mamba / FlashAttention / BERT / GPT）
    #    **不能再只看「有没有大写字母」**：旧规则把 Attention、Transformer、RAG
    #    都当成了 proper，一个提到 attention 的段落就能靠 proper_noun 过闸，
    #    跟「单个通用概念不能通过」直接冲突。现在按 ``_PROPER_CAMEL_RE``
    #    要求驼峰或全大写，再过 ``_GENERIC_PROPER`` 黑名单。
    from app.writing import _tokens_of

    # 2.5) 补充主题词（可追溯 / 溯源 / 学术 …）：
    #      字典 14 条覆盖不到真实 idea 结尾的「可追溯性」，补一张小表。
    #      **与 }# 3) 并排放在所有碎词收集完之后**——碎词是中文原文，插进
    #      4 个名额的中段就会把后面的补充词挤出去；主题词必须排在它们前面。
    #      长键优先：``可追溯`` 里含 ``追溯``，先试长键才不会漏掉「可追溯性」。
    extra_terms: list[dict[str, Any]] = []
    for zh in sorted(_ZH_EXTRA_TOPIC, key=len, reverse=True):
        frags_en = _ZH_EXTRA_TOPIC[zh]
        if zh and zh in idea_text and zh not in seen_zh:
            seen_zh.add(zh)
            tier, why = _concept_class(list(frags_en))
            extra_terms.append(
                {"zh": zh, "frags": list(frags_en), "kind": "extra", "tier": tier, "why": why}
            )
    # 长键命中后，把被它包含的短键标成已用，避免同一件事占两个名额
    for zh in list(_ZH_EXTRA_TOPIC):
        if zh in seen_zh:
            continue
        if any(zh != other and zh in other for other in seen_zh):
            seen_zh.add(zh)

    for tok in _tokens_of(idea, limit=6):
        # 中文实义词块也会被切成「架构效率」这类片段，先按字符集筛掉，
        # 只留纯英文 token 走专有名词判定。
        if not re.fullmatch(r"[A-Za-z][A-Za-z\-]{2,}", tok):
            continue
        if not _looks_like_proper_noun(tok):
            continue
        if any(tok.lower() in t["frags"] for t in terms):
            continue
        # GPT 这类虽然形态像模型名，但含义过泛，只能算 topic_concept
        p_tier, p_why = _proper_tier(tok)
        terms.append(
            {"zh": tok, "frags": [tok.lower()], "kind": "proper", "tier": p_tier, "why": p_why}
        )

    # 3) 中文 token 内部的未登录短词（保守兜底，只在没有字典词时才用）
    if not terms:
        for tok in _tokens_of(idea, limit=3):
            if not re.search(r"[\u4e00-\u9fff]", tok):
                continue
            covered = re.sub(r"[^\u4e00-\u9fff]+", "", tok)
            for zh in seen_zh:
                covered = covered.replace(zh, "")
            for run in re.findall(r"[\u4e00-\u9fff]+", covered):
                for w in _zh_window(run):
                    if w not in seen_zh:
                        seen_zh.add(w)
                        # 兜底词是中文原文，拿去和英文正文比不可靠，
                        # 一律不算锚点；它们的唯一作用是保留「提到了这个词」的痕迹。
                        terms.append(
                            {"zh": w, "frags": [w], "kind": "fallback", "tier": "generic", "why": "fallback"}
                        )
                    if len(terms) >= MAX_CONCEPTS:
                        break

    # 补充主题词排在所有「中文原文碎词」之前（见上方 2.5 的说明）
    terms = terms + extra_terms

    # 概念身份编号：**同名不代表同概念**。``rag`` 同时列在「检索增强」和「增强生成」
    # 的别名里，但这两个中文词在 idea 里指同一件事，必须共享同一个 id 才能被
    # G3 正确地去重计数（见 _g3_verdict）。
    #
    # 合并时 tier 按 ``_TIER_RANK`` 取「更强的那一边」：strong > topic > generic。
    # 只要参与合并的任一个词是强锚点，合并后就是强锚点；
    # 反过来会让「知识图谱」被「图谱」这个泛词拉低。
    merged: list[dict[str, Any]] = []
    for t in terms:
        cid = next((m["concept_id"] for m in merged if set(m["frags"]) & set(t["frags"])), None)
        if cid is None:
            cid = len(merged) + 1
            merged.append({**t, "concept_id": cid})
        else:
            target = next(m for m in merged if m["concept_id"] == cid)
            target["frags"] = list(dict.fromkeys([*target["frags"], *t["frags"]]))
            if _TIER_RANK.get(t.get("tier", "generic"), 0) > _TIER_RANK.get(
                target.get("tier", "generic"), 0
            ):
                target["tier"] = t.get("tier") or "generic"
                target["why"] = t.get("why") or "merged"
    # 截断时给「有区分度」的概念让位，但**必须给泛词保底名额**。
    #
    # 真问题（2026-11-12 第二轮）：真实 idea 先抽出 4 个字典词（引用/图谱/检索增强/幻觉），
    # 补充主题词「可追溯」「文献综述」排在后面，直接被 MAX_CONCEPTS=4 截掉——
    # 正文讲得再贴切也只能凑出 1 个 topic_concept，永远 topic_only。
    #
    # 但反过来「只留主题词」也不行：citation + graph 这类**泛词组合的拒绝判定**
    # 依赖泛概念本身存在。泛词被排掉后，UDL 第 255 页会落到 reason=""，
    # 拒绝统计就从 generic_only 漂移到空字符串。
    #
    # 折中：主题词优先占位，但**至少保留 ``_MIN_GENERIC_SLOTS`` 个泛概念**。
    # 注意 ``_MIN_GENERIC_SLOTS`` 个「名额」不是「概念个数」：
    # 「文献综述」的 frags 是 (literature, survey) 两个词，可以合并两个名额，
    # 而占位只花 1 个——所以按「已占名额数」算，不能按概念个数算。
    _MIN_GENERIC_SLOTS = 2
    generic = [m for m in merged if _TIER_RANK.get(m.get("tier", "generic"), 0) < 1]
    generic_hold = min(len(generic), _MIN_GENERIC_SLOTS)
    room_for_topic = max(0, MAX_CONCEPTS - generic_hold)

    picked: list[dict[str, Any]] = []
    for m in merged:
        if _TIER_RANK.get(m.get("tier", "generic"), 0) < 1:
            continue
        if len(picked) >= room_for_topic:
            break
        picked.append(m)

    used = {id(m) for m in picked}
    # 补泛词保底：按原顺序取前 generic_hold 个
    for m in generic[:generic_hold]:
        if id(m) not in used:
            picked.append(m)
            used.add(id(m))
    # 还有空位再按原顺序补
    for m in merged:
        if len(picked) >= MAX_CONCEPTS:
            break
        if id(m) not in used:
            picked.append(m)
            used.add(id(m))
    # 输出顺序恢复成「字典词叙述顺序」：按原 merged 的下标排
    pos = {id(m): i for i, m in enumerate(merged)}
    picked.sort(key=lambda m: pos[id(m)])
    return picked[:MAX_CONCEPTS]


def _looks_like_proper_noun(token: str) -> bool:
    """判断一个英文 token 是不是「具体到某个模型/方法」的专有名词。

    旧规则是「含任意大写字母就算」，实测把 ``Attention``、``Transformer``、
    ``RAG`` 全判成了 proper——于是任何提到 attention 的段落都能靠
    ``proper_noun`` 过闸。新规则要求**形态**上就像专有名词：

    1. 驼峰：至少两个大写开头的音节（``FlashAttention``、``BertModel``）；
    2. 或全大写缩写：``BERT`` / ``GPT`` / ``RAG``；
    3. 且不在 ``_GENERIC_PROPER`` 黑名单里（``RAG`` 会被这里挡掉）。

    ``GPT`` 的处理（2026-11-12 第二轮）：GPT-2/3/4、GPTs、以及大量
    「基于 GPT 的 XX」都在用这个词，单写 ``GPT`` 不指向某篇具体工作。
    所以它**不是** strong_anchor，而是 topic_concept——能承认「这块在谈
    GPT」，但不能靠它一个词放行。判定入口在 ``_concept_terms``。

    为什么 ``Mamba`` 能过：它虽然只有一个大写字母，但不在黑名单里，
    且是 idea 作者显式写出的英文词——这是本条判据的**有意留出的口子**，
    与「黑名单里的通用技术词不能当锚点」并不矛盾。
    """
    tok = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z\-]{2,}", tok):
        return False
    if tok.lower() in _GENERIC_PROPER:
        return False
    if _PROPER_CAMEL_RE.fullmatch(tok):
        return True
    # 单个大写字母开头的普通词（Mamba 这类）：
    # 只要不是黑名单里的通用词，就认为作者是在指一个具体对象。
    return bool(tok[0].isupper() and not tok.isupper())


def _proper_tier(token: str) -> tuple[str, str]:
    """专有名词该给哪一档。返回 ``(tier, why)``。

    - 命中 ``_AMBIGUOUS_PROPER``（gpt / llm / ai …）→ ``topic_concept``：
      形态像模型名，但含义过泛、被反复复用，不能单独放行；
    - 其余（Mamba / FlashAttention / BERT / ResNet…）→ ``strong_anchor``。
    """
    low = (token or "").strip().lower()
    if low in _AMBIGUOUS_PROPER:
        return "topic_concept", f"ambiguous_proper:{low}"
    return "strong_anchor", "proper"


#: tier 的强弱次序，合并概念时取「更强的那一边」。
#: ``generic`` 最弱、``strong_anchor`` 最强。
_TIER_RANK = {"generic": 0, "topic_concept": 1, "strong_anchor": 2}


#: 允许跟在词根后面的词形后缀。白名单制：``hallucinat`` 后面只可能是
#: ion/ions/e/ed/ing，不可能是 ``hallucinatxyz``。
_ROOT_SUFFIXES = frozenset(
    {
        "",
        "s",
        "es",
        "e",
        "ed",
        "d",
        "ing",
        "ion",
        "ions",
        "ation",
        "ations",
        "ication",
        "ications",
        "y",
        "ied",
        "ies",
        "al",
        "ally",
        "able",
        "ment",
        "ments",
        "er",
        "ers",
        "or",
        "ors",
        "ity",
        "ivity",
        "ive",
        "ism",
        "ability",
        "abilities",
        "traceability",
    }
)


def _root_matches(root: str, word: str) -> bool:
    """词根是否命中这个词——**只按词边界 + 词形归一**，不做双向子串匹配。

    真问题（2026-11-12 第二轮）：旧实现是 ``root in word or word in root``，
    双向子串。``code`` 于是命中了 ``encode`` / ``decode`` / ``barcode``，
    ``math`` 命中 ``aftermath``。命中一个根本不相干的词就把块判成
    「谈了代码生成 / 数学推理」。

    归一规则：词根必须出现在词的**开头**，后面只能跟 ``_ROOT_SUFFIXES``
    白名单里的词形后缀。
    - ``hallucinat`` + ``ion`` → hallucination；+ ``e``/``ed``/``ing`` 也对；
    - ``verif`` + ``ication``/``y``/``ied`` → verification / verify；
    - ``knowledge`` 整词命中。

    多词词根（``question answering`` / ``long document`` / ``multi-agent``）
    逐词匹配，连字符在两边都归一成空格。
    """
    r = (root or "").lower().replace("-", " ").strip()
    w = (word or "").lower().replace("-", " ").strip()
    if not r or not w:
        return False
    r_words = r.split()
    w_words = w.split()
    if len(r_words) != len(w_words):
        return False
    for rw, ww in zip(r_words, w_words, strict=False):
        if rw == ww:
            continue
        # 词根至少 4 个字母才允许带后缀放宽，短词根要求整词相等
        if len(rw) < 4:
            return False
        if not ww.startswith(rw):
            return False
        # 只接受常见词形后缀，拒绝「词根 + 任意长尾巴」
        if ww[len(rw):] not in _ROOT_SUFFIXES:
            return False
    return True


def _concept_class(frags: list[str]) -> tuple[str, str]:
    """判定一个概念（由若干英文片段组成）属于哪一档。

    三档（2026-11-12 第二轮把 ``anchor`` 拆成 ``strong_anchor`` 与 ``topic_concept``）：

    - ``strong_anchor``：**具体到某个模型 / 方法 / 明确命名的研究对象**，
      单独命中即可放行（Mamba、FlashAttention、BERT、知识图谱里的 knowledge…）；
    - ``topic_concept``：有研究意义，但单独出现不足以证明论文围绕核心选题
      （**幻觉**降到这里：三篇补入论文全部只靠 hallucination 过关，
      而它们讲的是通用幻觉缓解，不是「引用图谱约束的 RAG」）；
    - ``generic``：通用辅助概念，永远不能单独放行。

    做法：把片段**减去泛词**，看还剩什么，再用**词边界匹配**去对词根表。

    - 剩空 → ``generic``（``引用`` → citation/cite/reference 全是泛词）；
    - 剩的东西命中强词根 → ``strong_anchor``；
    - 剩的东西命中主题词根 → ``topic_concept``；
    - 剩了东西但对不上任何词根 → ``generic``（保守：不认识的剩余词没有
      区分度证据，不能当锚点用）。

    ``code`` / ``math`` / ``reason`` **不在任何词根表里**：它们指向的是任务类型
    而不是研究对象，且短词根误匹配风险大（``code`` vs ``encode``）。

    返回 ``(tier, why)``，``why`` 用于审计时解释这一档是怎么来的。
    """
    residual: list[str] = []
    for f in frags:
        low = (f or "").lower().strip()
        if not low:
            continue
        # 整个片段命中泛词表就丢掉；否则再按空格切，逐个词看
        if low in _GENERIC_FRAGS:
            continue
        words = [w for w in low.replace("-", " ").split() if w and w not in _GENERIC_FRAGS]
        residual.extend(words)
    if not residual:
        return "generic", "all_generic"
    for word in residual:
        for root in _STRONG_ROOTS:
            if _root_matches(root, word):
                return "strong_anchor", f"strong_root:{root}"
    for word in residual:
        for root in _TOPIC_ROOTS:
            if _root_matches(root, word):
                return "topic_concept", f"topic_root:{root}"
    return "generic", "unrecognised_residual"


def _zh_window(run: str) -> list[str]:
    """把一段中文切成候选议题词：**长词优先**（5~2 字），再逐字跳过重来。

    为什么不直接取四字滑窗：实测 idea「探索 Mamba 这类状态空间模型处理长文档问答的能力边界」
    去掉字典词「长文档」「问答」后剩下「探索状态空间模型处理」，
    四字滑窗给的是「探索状态」「索状态空」这类跨词边界的碎片，
    只有「状态空间」才是真词。贪心长词优先至少能把真词排在前面。
    """
    out: list[str] = []
    i = 0
    while i < len(run):
        for size in (5, 4, 3, 2):
            w = run[i : i + size]
            if len(w) == size and w[0] not in _ZH_UNIGRAM_DROP and w[-1] not in _ZH_UNIGRAM_DROP:
                out.append(w)
                i += size
                break
        else:
            i += 1
    return out


def _topic_hits(text: str, concepts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """返回该块命中了哪些议题概念。

    **同一个概念只计一次**：concept 内部的 frags 是并列的别名
    （``检索增强`` → ``retrieval`` / ``rag``），任意命中即为该概念命中。
    ``rag`` 同时属于「检索增强」和「增强生成」两个概念的片段，但它们在
    ``_concept_terms`` 里已经合并成同一个 ``concept_id``，
    所以 G3 统计时不会因为一个英文词点亮两个概念而重复计数。

    每条命中带 ``tier``（``strong_anchor`` / ``topic_concept`` / ``generic``），
    供 ``_g3_verdict`` 分级判定。
    """
    low = (text or "").lower()
    out: list[dict[str, Any]] = []
    for idx, c in enumerate(concepts, start=1):
        frags = [f for f in (c.get("frags") or []) if f]
        hit_frag = next((f for f in frags if _frag_in(f, low)), None)
        if hit_frag is None:
            continue
        out.append(
            {
                "zh": c["zh"],
                "matched": hit_frag,
                "kind": c["kind"],
                "tier": c.get("tier", "generic"),
                "concept_id": c.get("concept_id", idx),
            }
        )
    return out


def _frag_in(frag: str, low_text: str) -> bool:
    """片段是否出现在（已转小写的）正文里。

    - 单片段（``hallucinat`` / ``mamba``）按子串匹配，这是原来的行为，保留。
    - **多词片段**（``knowledge graph`` / ``long document`` / ``question answering``）
      拆成词逐个匹配，要求**全部出现**——不要求相邻、不要求原顺序。

    为什么多词片段不能用整串子串匹配：PDF 正文里断行、连字符、单复数都会变，
    ``knowledge graph`` 常常写成 ``knowledge graphs`` 或中间夹换行；
    更常见的情况是正文只写 ``graph`` 而 idea 写「知识图谱」。
    实测这两类在整串匹配下**全部漏检**（``knowledge graph`` / ``long document`` /
    ``question answering`` 三个字典词都是多词片段，等于一直没生效过）。
    拆词要求全部出现，比「命中任意一词」严，不会把只提到 graph 的块
    误判成谈到了知识图谱。
    """
    if not frag:
        return False
    words = [w for w in frag.split() if w]
    if len(words) <= 1:
        return frag in low_text
    return all(_word_in(w, low_text) for w in words)


def _word_in(word: str, low_text: str) -> bool:
    """词边界匹配：``question`` 不能命中 ``questioning``。

    多词片段拆开后必须按词边界判，否则 ``question answering`` 会被
    ``questioning`` 这种词点亮；但允许单复数（``graphs``/``citations``），
    因为 PDF 正文里单复数变化很普遍。
    """
    return re.search(rf"(?<![a-z]){re.escape(word)}(?:s|es)?(?![a-z])", low_text) is not None


def _g3_verdict(text: str, concepts: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """G3：这块正文和 idea 的议题到底有没有关系。

    规则（2026-11-12 第二轮，**宁严不宽**，按优先级）：

    1. 命中 **≥2 个强锚点** → 通过，``reason="two_strong_anchors"``；
    2. 命中 **1 个强锚点** → 通过，``reason="strong_anchor"``；
    3. 强锚点 0 个，但命中 **≥1 个 topic_concept + ≥1 个不同且有意义的其他概念**
       → 通过，``reason="topic_concept_with_support"``；
    4. 其余 → 拒绝。``reason`` 视情况取 ``topic_only`` / ``generic_only`` / ``""``。

    **为什么要分三档**（两轮实测给的教训）：

    第一轮只数「命中几个不同概念」，Understanding Deep Learning 第 255 页
    靠 citation + graph 两个泛词凑成 two_concepts 被放行。

    第二轮把 hallucination 也当锚点，真实索引复测发现补进候选池的
    三篇（LLaVA / GPT-4 / Llama 3）**全部**只靠 hallucination 一个词过关，
    而它们讲的是通用幻觉缓解，不是「引用图谱约束的 RAG」。
    幻觉是每篇大模型论文都会提的词——证明「相关领域」可以，证明
    「围绕核心选题」不够。所以 hallucination 降为 topic_concept。

    **第 3 条「有支撑的 topic_concept」到底怎么算**：不能把任意两个
    topic_concept 自动当成相关（那等于退回第一轮的凑数）。这里要求：
    - 至少 1 个 topic_concept；
    - 另有至少 1 个**不同 concept_id** 的概念，且它不能是纯 generic——
      它必须是另一个 topic_concept 或 strong_anchor；
    - 两者概念 id 不同（同一概念的别名不算）。
    也就是说 ``幻觉 + 校验`` 这类**两个不同研究方向**的组合才算成型，
    而 ``幻觉 + citation`` 这种「主题词 + 万金油」不算。

    「不同概念」按 ``concept_id`` 判，**不按命中词判**：一个英文词可能同时是
    多个中文词的别名，但那些中文词若指同一件事，在 ``_concept_terms`` 里
    已经共享同一个 ``concept_id``。

    关于「同一 source 内多块交叉支持再放行」：本轮**不做**。它需要把门从单块
    判定改成两趟扫描，改动范围明显扩大，且会把「整篇论文到处提 RAG」误当作
    「多个概念」。代价是单概念 idea 的召回偏低，属于已知局限而不是 bug。

    关于 ``generic_partial``：**已删除**（2026-11-12）。它原本想表达
    「锚点只命中片段里的泛词部分」，但 ``_frag_in`` 对多词片段要求全部词出现，
    这种「部分命中」根本进不了 ``hits``，分支从未被触发过。
    """
    hits = _topic_hits(text, concepts)

    strong_ids: set[int] = set()
    topic_ids: set[int] = set()
    generic_ids: set[int] = set()
    for h in hits:
        cid = h["concept_id"]
        tier = h.get("tier", "generic")
        if tier == "strong_anchor":
            strong_ids.add(cid)
        elif tier == "topic_concept":
            topic_ids.add(cid)
        else:
            generic_ids.add(cid)

    if len(strong_ids) >= 2:
        reason = "two_strong_anchors"
        ok = True
    elif len(strong_ids) == 1:
        reason = "strong_anchor"
        ok = True
    elif topic_ids and len(topic_ids | strong_ids) >= 2:
        # ≥1 个 topic_concept，且另有不同 id 的 topic_concept —— 两个研究方向
        # 同时命中才算成型。这里 strong_ids 必为空（前面两个分支已处理），
        # 所以实际要求是 len(topic_ids) >= 2。
        reason = "topic_concept_with_support"
        ok = True
    elif topic_ids:
        # 只有一个 topic_concept（例：只提到 hallucination）
        reason = "topic_only"
        ok = False
    elif len(generic_ids) >= 2:
        # 只剩通用辅助概念，靠它们凑数不成立（citation + graph 就是这一类）
        reason = "generic_only"
        ok = False
    else:
        reason = ""
        ok = False

    return ok, {
        "hits": hits,
        "reason": reason,
        "matched": [h["matched"] for h in hits],
        "types": [h["kind"] for h in hits],
        "tiers": [h["tier"] for h in hits],
        "anchor_ids": sorted(strong_ids),
        "topic_ids": sorted(topic_ids),
        "generic_ids": sorted(generic_ids),
    }


# --------------------------------------------------------------------------- 纯函数：证据与 key


def _to_evidence(chunk: Any, score: float, channels: list[str], query: str) -> dict[str, Any]:
    """把 ``pipeline.Chunk`` 转成证据 dict。

    ``text`` 存**完整原文**，不在这里截断——截断只发生在 ``render_pool`` 渲染 prompt 时。
    ``page`` 为 ``None`` 时如实保留，渲染成「页码未定位」，**绝不写成第 0 页**。
    """
    return {
        "source_file": chunk.source,
        "chunk_id": int(chunk.chunk_id),
        "page": chunk.page,
        "text": chunk.text,
        "score": round(float(score), 5),
        # 检索分数，只用于同一篇内部排序；不解释成概率，也不当作相关性保证
        "score_type": "rerank" if "rerank" in channels else "rrf",
        "channels": list(channels),
        "query": query,
    }


def _next_key(pool: list[lit.Candidate]) -> int:
    """下一个可用的 / 编号。

    ``build_candidates`` 在 ``literature.py`` 里一次性把 key 重写成 C1..Cn，
    所以这里只要接在最大值后面就**不可能撞号**。
    """
    nums = [int(c.key[1:]) for c in pool if c.key.startswith("C") and c.key[1:].isdigit()]
    return (max(nums) + 1) if nums else 1


def plain_query(expr: str) -> str:
    """把 ``all:"a" AND all:"b"`` 这类 arXiv 布尔表达式还原成自然词组。

    正文检索（chroma + 重排模型）吃的是自然语言，喂布尔语法只会匹配失败。
    """
    text = re.sub(r'all:\s*"([^"]*)"', r"\1 ", expr or "")
    text = text.replace(" AND ", " ").replace(" OR ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _title_query(title: str) -> str:
    """用 catalog 里的真实标题构造一条正文检索 query。"""
    text = re.sub(r"[^\w\s\-]", " ", title or "")
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(text.split(" ")[:6])


# --------------------------------------------------------------------------- 主入口


def _new_tally() -> dict[str, int]:
    """Gate 台账。字段语义固定，按 retrieved → dup → no_catalog → G1 → G2 → G3 → passed
    的短路顺序各自计数（一块只会落在第一个拦下它的格子里）。

    ``g3_generic_only`` 是 2026-11-12 新加的**补充统计**：G3 的拒绝数仍然记在
    ``g3_topic`` 里（语义不变、旧统计可继续比），其中「只命中通用辅助概念、
    没有主题锚点」的那部分额外记一份，用来观察泛词凑数有多常见。
    """
    return {
        "retrieved": 0,
        "dup_skipped": 0,
        "no_catalog": 0,
        "g1_citation": 0,
        "g2_quality": 0,
        "g3_topic": 0,
        "g3_generic_only": 0,
        "passed": 0,
        "papers_seen": 0,
        "papers_in_pool": 0,
        "papers_promoted": 0,
        "papers_skipped_cap": 0,
    }


def promote_from_evidence(
    idea: str,
    english_queries: list[str],
    pool: list[lit.Candidate],
    total_limit: int = 32,
    zh_queries: list[str] | None = None,
) -> dict[str, Any]:
    """全库检索本库正文，把命中的论文补进候选池并绑定证据。**原地修改 ``pool``**。

    参数
    ----
    idea
        用户的研究 idea，用来抽议题概念（G3 判据）。
    english_queries
        英文 query（通常来自 ``writing._english_queries``，形如 ``all:"a" AND all:"b"``）。
        取前 ``GLOBAL_QUERY_LIMIT`` 条做全局检索。
    pool
        候选池。**必须是 ``build_candidates`` 的返回值**（此时 key 已是 C1..Cn）。
    total_limit
        原池上限，用来算补池预算 ``total_limit // MAX_PROMOTED_DIVISOR``。
    zh_queries
        中文 query。**只用来发现 source，不直接贡献证据**：中文在英文正文里几乎
        打不中，实测 ``检索增强生成方法`` 的 top12 里 ``rag`` 出现 0 次。
        中文命中的论文要再用「全局英文 query 或标题构造的英文 query」做
        source 限定检索，拿到合格英文块才能进池。

    返回
    ----
    ``{"status", "promoted", "errors", "discovered_without_evidence", "gates"}``。
    ``status`` ∈ ``ok`` / ``no_hit`` / ``no_promote`` / ``skipped``。
    """
    from app.rag import pipeline

    gates = _new_tally()
    errors: list[str] = []
    promoted: list[str] = []
    discovered_without_evidence: list[str] = []

    concepts = _concept_terms(idea)
    catalog = {str(r["file"]): r for r in lit.load_catalog() if r.get("file")}
    existing = {c.local_file for c in pool if c.local_file}
    budget = max(0, total_limit // MAX_PROMOTED_DIVISOR)

    if not english_queries and not zh_queries:
        return {
            "status": "skipped",
            "promoted": [],
            "errors": [],
            "discovered_without_evidence": [],
            "gates": gates,
        }

    # ---- 1) 三闸（固定顺序短路）+ 白名单 + 去重
    seen: set[tuple[str, int]] = set()
    by_file: dict[str, list[tuple[Any, float, list[str], str]]] = {}
    zh_discovered: list[str] = []

    def _collect(query: str, top_k: int, source: str | None) -> None:
        try:
            got = pipeline.retrieve_scored(query, top_k=top_k, source=source)
        except Exception as exc:  # noqa: BLE001 - 降级，不阻断主流程
            errors.append(f"{query}: {exc}")
            logger.warning("本地正文检索失败 query=%s source=%s: %s", query, source, exc)
            return
        for chunk, score, chans in got:
            gates["retrieved"] += 1
            key = (chunk.source, chunk.chunk_id)
            if key in seen:
                gates["dup_skipped"] += 1
                continue
            seen.add(key)
            if chunk.source not in catalog:
                gates["no_catalog"] += 1
                continue
            # G1 三判据并联
            if (
                "citation" in chans
                or pipeline._looks_like_citation(chunk.text)
                or _looks_like_ref_fragment(chunk.text)
            ):
                gates["g1_citation"] += 1
                continue
            # G2 文本质量
            if len(chunk.text) < MIN_TEXT_CHARS or _alpha_ratio(chunk.text) < MIN_ALPHA_RATIO:
                gates["g2_quality"] += 1
                continue
            if _newline_ratio(chunk.text) > MAX_NEWLINE_RATIO:
                gates["g2_quality"] += 1
                continue
            if _looks_like_table_fragment(chunk.text):
                gates["g2_quality"] += 1
                continue
            if _looks_like_reference_block(chunk.text):
                gates["g2_quality"] += 1
                continue
            # G3 议题相关
            ok, info = _g3_verdict(chunk.text, concepts)
            if not ok:
                gates["g3_topic"] += 1
                if info.get("reason") == "generic_only":
                    gates["g3_generic_only"] += 1
                continue
            gates["passed"] += 1
            by_file.setdefault(chunk.source, []).append((chunk, score, chans, query, info))

    # 1a) 全局英文检索
    for expr in english_queries[:GLOBAL_QUERY_LIMIT]:
        _collect(plain_query(expr), GLOBAL_TOPK, None)

    # 1b) 中文发现检索：只看 source，不贡献证据
    for zq in (zh_queries or [])[:1]:
        try:
            found = pipeline.retrieve_scored(zq, top_k=GLOBAL_TOPK)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{zq}: {exc}")
            logger.warning("中文发现检索失败 query=%s: %s", zq, exc)
            continue
        for chunk, _score, _chans in found:
            if chunk.source in catalog and chunk.source not in zh_discovered:
                zh_discovered.append(chunk.source)

    gates["papers_seen"] = len(by_file) + len([f for f in zh_discovered if f not in by_file])

    # ---- 2) 先给已在池内的本库论文绑证据（不占补池预算）
    ranked = sorted(by_file.items(), key=lambda kv: -max(x[1] for x in kv[1]))
    for fname, items in ranked:
        if fname in existing:
            gates["papers_in_pool"] += 1
            _bind(pool, fname, items)

    # ---- 3) 中文发现但英文全局没捞到的：补一次 source 限定英文检索
    for fname in [f for f in zh_discovered if f not in by_file]:
        got = _supplement_one(fname, catalog, english_queries, _collect, by_file)
        if not got:
            discovered_without_evidence.append(fname)
            continue
        if fname in existing:
            gates["papers_in_pool"] += 1
            _bind(pool, fname, got)
            continue
        if len(promoted) >= budget:
            gates["papers_skipped_cap"] += 1
            continue
        cand = _build_candidate(fname, catalog, got, _next_key(pool), category="zh_discovered")
        if cand is None:
            continue
        pool.append(cand)
        promoted.append(fname)
        gates["papers_promoted"] += 1

    # ---- 4) 英文全局命中的新论文：补候选
    for fname, items in ranked:
        if fname in existing:
            continue
        if len(promoted) >= budget:
            gates["papers_skipped_cap"] += 1
            continue
        cand = _build_candidate(fname, catalog, items, _next_key(pool), category="english_global")
        if cand is None:
            continue
        pool.append(cand)
        promoted.append(fname)
        gates["papers_promoted"] += 1

    if not by_file and not zh_discovered:
        status = "no_hit"
    elif not promoted and not gates["papers_in_pool"]:
        status = "no_promote"
    else:
        status = "ok"
    return {
        "status": status,
        "promoted": promoted,
        "errors": errors,
        "discovered_without_evidence": discovered_without_evidence,
        "gates": gates,
    }


def _supplement_one(
    fname: str,
    catalog: dict[str, Any],
    english_queries: list[str],
    collect: Any,
    by_file: dict[str, Any],
) -> list[Any]:
    """中文发现、但全局英文检索没捞到的论文：补一次 source 限定的英文检索。

    优先用全局英文 query；再补一条 catalog 真实标题构造的 query。
    **只有拿到过闸的英文块才算数**——只发现不取证的一律记 discovered_without_evidence。
    """
    queries = [plain_query(e) for e in english_queries[:GLOBAL_QUERY_LIMIT]]
    title_q = _title_query(str((catalog.get(fname) or {}).get("title") or ""))
    if title_q:
        queries.append(title_q)
    queries = [q for q in queries if q]
    if not queries:
        return []

    before = len(by_file.get(fname) or [])
    for q in queries:
        collect(q, PER_PAPER_TOPK, fname)
        if len(by_file.get(fname) or []) - before >= PER_PAPER_TOPK:
            break
    got = by_file.get(fname) or []
    return got if len(got) > before else []


def _build_candidate(
    fname: str,
    catalog: dict[str, Any],
    items: list[Any],
    key_number: int,
    category: str = "",
) -> lit.Candidate | None:
    """用 catalog 的真实元数据建一个本库候选，并挂上证据。"""
    row = catalog.get(fname)
    if not row:
        return None
    items = sorted(items, key=lambda x: -x[1])[:MAX_EVIDENCE_PER_PAPER]
    authors = lit.load_local_authors().get(fname, [])
    abstract = _local_abstract(fname)
    year = row.get("year")
    first = items[0] if items else None
    return lit.Candidate(
        key=f"C{key_number}",
        title=str(row.get("title") or fname),
        authors=list(authors),
        year=int(year) if isinstance(year, int) or str(year).isdigit() else None,
        source=lit.SOURCE_LOCAL,
        arxiv_id=str(row.get("arxiv_id") or ""),
        venue=str(row.get("category") or ""),
        abstract=abstract,
        local_file=fname,
        score=float(first[1]) if first else 0.0,
        evidence=[_to_evidence(chunk, score, chans, q) for chunk, score, chans, q, _info in items],
        entry_reason={
            "kind": "evidence_promoted",
            "category": category,
            "query": first[3] if first else "",
            "g3": first[4] if first else {},
        },
    )


def _bind(pool: list[lit.Candidate], fname: str, items: list[Any]) -> None:
    """给**已在池里**的本库候选绑证据（不新建、不改 key/标题等任何字段）。"""
    for c in pool:
        if c.local_file == fname:
            items = sorted(items, key=lambda x: -x[1])[:MAX_EVIDENCE_PER_PAPER]
            c.evidence = [
                _to_evidence(chunk, score, chans, q) for chunk, score, chans, q, _info in items
            ]
            if not c.entry_reason:
                c.entry_reason = {"kind": "retrieval"}
            return


def _local_abstract(fname: str) -> str:
    """从 ``data/meta/structures.json`` 取摘要。catalog.json 里没有摘要字段。"""
    try:
        from app.papers import load_structures

        meta = load_structures().get(fname) or {}
        return str(meta.get("abstract") or "")
    except Exception as exc:  # noqa: BLE001 - 摘要只是锦上添花，取不到不影响证据
        logger.warning("读取本地摘要失败 %s: %s", fname, exc)
        return ""