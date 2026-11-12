"""按「研究领域 → 具体方向」两级给论文分类（规则打分，不训练模型）。

为什么要重做
------------
旧实现 `assign_topic()` 只有一条链式规则，规则都没命中就归入「基础模型与预训练」。
本库 40 条里 16 条是这么进来的（Mamba、BERT、T5、GPT-2、LLaMA 全在其中），
那个最大类别其实是规则遗漏的兜底桶，不是内容分类。

现在的口径
----------
1. 两级：一级 `primary_topic` 是研究领域，二级 `secondary_topics` 是更细的方向。
   列表里的名字都是从本库 40 篇论文里归纳出来的，不是照搬通用分类法。
2. 证据分强弱：标题命中强（2 分），摘要命中弱（1 分），多个独立关键词同时命中再加分。
   arXiv 学科分类只作辅助 +0.5 分，**单独命中不足以定分类**——它按投稿版块给，
   同一版块里混着架构、训练技巧和对齐论文。
3. 没有可信证据就标 `pending`，**不再兜底到具体类别**。宁可空着，不猜。
4. 同一篇可以有多个标签：主分类在 `primary_topic`，其余在 `secondary_topics`。
5. 输出的是规则分到「可信度等级」的映射（高/中/低），不是概率——没有做过概率校准，
   所以字段名用 `confidence`（等级字符串），不写成 0~1 的小数。

对外兼容
--------
`assign_all()` 仍然写回 `category` 与 `topic` 两个旧字段（问答侧 `app/agent.py`
和写作侧 `app/literature.py` 都直接读 `category`），另外追加新字段。
`TOPICS` 也保留，但内容收敛为一级领域名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 分类体系

# 一级研究领域（顺序即排序优先级，报告与前端都按这个顺序展示）
FIELD_MODELS = "基础模型与预训练"
FIELD_ARCH = "模型架构"
FIELD_ALIGN = "对齐与推理"
FIELD_EFFI = "训练效率与微调"
FIELD_MULTI = "多模态模型"
FIELD_EVAL = "能力评测与规模"
FIELD_THEORY = "教材与理论"
FIELD_PENDING = "待归类"

#: 一级领域名，供前端下拉与统计使用。`待归类` 永远排最后。
TOPICS: list[str] = [
    FIELD_MODELS,
    FIELD_ARCH,
    FIELD_ALIGN,
    FIELD_EFFI,
    FIELD_MULTI,
    FIELD_EVAL,
    FIELD_THEORY,
    FIELD_PENDING,
]

#: 二级方向 → 所属领域。二级名同样是按本库内容归纳的。
SUBTOPIC_FIELD: dict[str, str] = {
    "自回归语言模型": FIELD_MODELS,
    "双向预训练模型": FIELD_MODELS,
    "文本到文本模型": FIELD_MODELS,
    "多语言与开源基座": FIELD_MODELS,
    "Transformer": FIELD_ARCH,
    "状态空间与线性时间模型": FIELD_ARCH,
    "高效注意力": FIELD_ARCH,
    "归一化方法": FIELD_ARCH,
    "指令微调": FIELD_ALIGN,
    "人类反馈对齐": FIELD_ALIGN,
    "推理增强": FIELD_ALIGN,
    "参数高效微调": FIELD_EFFI,
    "量化与模型压缩": FIELD_EFFI,
    "训练优化与并行": FIELD_EFFI,
    "视觉语言模型": FIELD_MULTI,
    "多模态对齐": FIELD_MULTI,
    "语音与多模态": FIELD_MULTI,
    "基准测试": FIELD_EVAL,
    "Scaling Law": FIELD_EVAL,
    "教材与综述": FIELD_THEORY,
}

#: 二级方向的定义。`title`/`abstract` 是关键词（小写、子串匹配），`requires_any`
#: 是「至少命中其中一个才算数」的门槛词，用来避免只因为出现一个常见词就归类。
_RULES: list[dict[str, Any]] = [
    {
        "subtopic": "自回归语言模型",
        # 型号名只是例子，真正起作用的是「xx language model / foundation models / 模型家族」
        # 这类描述性说法——新一批同类型论文不必逐条加型号也能命中。
        "title": [
            "few-shot learners",
            "language understanding by generative pre-training",
            "gpt-4",
            "gpt2",
            "open and efficient foundation language models",
            "mistral 7b",
            "llama 3",
            "qwen2.5",
            "collection of pretrained and fine-tuned",
            "herd of models",
            "technical report",
            "large language models",
            "trained on code",
        ],
        # DeepSeek-V3 摘要里是 "Mixture-of-Experts (MoE) language model"，中间夹括号，
        # 所以这里必须写全带括号的形式，不能简写成 "mixture-of-experts language model"。
        "abstract": [
            "autoregressive language model",
            "few-shot learners",
            "mixture-of-experts (moe) language model",
            "billion-parameter language model",
            "large language model",
            "float16/bfloat16",
            "hugging face transformers",
            "open-access models",
        ],
        "requires_any": [
            "few-shot",
            "gpt",
            "generative pre-training",
            "autoregressive",
            "language model",
            "llm",
        ],
    },
    {
        "subtopic": "双向预训练模型",
        "title": ["bidirectional transformers"],
        "abstract": ["bidirectional encoder", "masked language model"],
        "requires_any": ["bert", "bidirectional"],
    },
    {
        "subtopic": "文本到文本模型",
        "title": ["text-to-text transformer", "unified text-to-text"],
        "abstract": ["text-to-text"],
        "requires_any": ["text-to-text", "t5"],
    },
    {
        "subtopic": "多语言与开源基座",
        "title": ["bloom", "glm-130b", "deepseek-v3"],
        "abstract": ["bilingual", "multilingual", "open-access", "open-access multilingual"],
        "requires_any": ["multilingual", "bilingual", "open-access", "glm-130b", "bloom"],
    },
    {
        "subtopic": "Transformer",
        "title": ["attention is all you need", "transformer"],
        "abstract": ["sequence transduction", "self-attention"],
        "requires_any": ["transformer", "self-attention", "attention is all you need"],
    },
    {
        "subtopic": "状态空间与线性时间模型",
        "title": ["mamba", "state spaces", "rwkv", "recurrent neural network"],
        "abstract": ["state space", "linear-time", "linear attention"],
        "requires_any": ["state space", "mamba", "rwkv", "recurrent"],
    },
    {
        "subtopic": "高效注意力",
        "title": ["flashattention", "memory-efficient exact attention", "flash attention"],
        "abstract": ["io-aware", "memory-efficient exact attention", "tiling and recomputation"],
        "requires_any": ["flashattention", "flash attention", "io-aware"],
    },
    {
        "subtopic": "归一化方法",
        "title": ["layer normalization", "layernorm"],
        "abstract": ["layer normalization", "layer normalization"],
        "requires_any": ["layer normalization", "layernorm"],
    },
    {
        "subtopic": "指令微调",
        "title": ["follow instructions", "instruction tuning", "visual instruction tuning"],
        "abstract": ["instruction tuning", "instruction-following"],
        "requires_any": ["instruction tuning", "instruction-following", "follow instructions"],
    },
    {
        "subtopic": "人类反馈对齐",
        "title": [
            "human feedback",
            "proximal policy optimization",
            "direct preference optimization",
            "fine-tuned chat models",
        ],
        "abstract": ["human feedback", "preference optimization", "reward model", "rlhf", "fine-tuned chat"],
        "requires_any": [
            "human feedback",
            "preference optimization",
            "proximal policy optimization",
            "rlhf",
            "reward model",
            "chat models",
        ],
    },
    {
        "subtopic": "推理增强",
        "title": ["chain-of-thought", "incentivizing reasoning", "reasoning capability"],
        "abstract": ["chain of thought", "reinforcement learning", "reasoning"],
        "requires_any": ["chain-of-thought", "chain of thought", "reasoning"],
    },
    {
        "subtopic": "参数高效微调",
        "title": ["lora", "low-rank adaptation", "finetuning", "instruction pre-training"],
        "abstract": ["low-rank", "parameter-efficient", "finetuning", "fine-tuning", "instruction pre-training"],
        "requires_any": ["lora", "low-rank", "parameter-efficient", "finetuning", "pre-training"],
    },
    {
        "subtopic": "量化与模型压缩",
        "title": ["qlora", "quantized"],
        "abstract": ["quantization", "4-bit", "quantized"],
        "requires_any": ["quantiz", "4-bit"],
    },
    {
        "subtopic": "训练优化与并行",
        "title": ["zero: memory", "switch transformers", "mixture of experts"],
        "abstract": ["mixture-of-experts", "memory optimizations", "sparsity", "parallelism"],
        "requires_any": ["mixture-of-experts", "mixture of experts", "zero", "parallelism"],
    },
    {
        "subtopic": "视觉语言模型",
        "title": ["image is worth", "visual models", "vision transformer"],
        "abstract": ["image recognition", "visual models", "contrastive pretraining"],
        "requires_any": ["image", "vision", "visual"],
    },
    {
        "subtopic": "多模态对齐",
        "title": ["clip", "natural language supervision"],
        "abstract": ["natural language supervision", "zero-shot transfer"],
        "requires_any": ["natural language supervision", "zero-shot"],
    },
    {
        "subtopic": "语音与多模态",
        "title": ["speech recognition", "whisper"],
        "abstract": ["speech processing", "speech recognition", "audio"],
        "requires_any": ["speech", "audio"],
    },
    {
        "subtopic": "基准测试",
        "title": ["evaluating large language models", "trained on code"],
        "abstract": ["benchmark", "evaluate", "evaluation"],
        "requires_any": ["benchmark", "evaluat", "codex", "humaneval"],
    },
    {
        "subtopic": "Scaling Law",
        "title": ["scaling laws", "emergent abilities"],
        "abstract": ["scaling laws", "power-law", "emergent abilities"],
        "requires_any": ["scaling law", "power-law", "emergent"],
    },
    {
        "subtopic": "教材与综述",
        "title": ["understanding deep learning"],
        "abstract": ["textbook", "this book"],
        "requires_any": ["understanding deep learning", "textbook"],
    },
]

# arXiv 一级分类（cs.LG / cs.CL / cs.CV / cs.AI / stat.ML）→ 线索：只加分，不定分类
_ARXIV_HINT: dict[str, list[str]] = {
    "cs.CV": ["视觉语言模型", "多模态对齐"],
    "cs.CL": ["自回归语言模型", "文本到文本模型"],
    "eess.AS": ["语音与多模态"],
}

# 门槛：加权得分（标题 2 分 / 摘要 1 分 / arXiv 0.5 分）
_MIN_SCORE = 2.0          # 标题命中时要求的分数（一条标题命中即 2.0）
_MIN_ABSTRACT_ONLY_SCORE = 1.25  # 只有摘要命中时：至少要 2 个摘要关键词（1.0+0.25）才算
_ARXIV_ALONE_MAX = 0.5    # arXiv 线索的上限，永远够不着 _MIN_ABSTRACT_ONLY_SCORE


@dataclass
class Classification:
    """一条论文的分类结果。字段名对齐验收报告要求。"""

    primary_topic: str
    subtopic: str
    status: str
    """`classified` / `pending` / `manual`。"""
    confidence: str
    """`high` / `medium` / `low`。等级不是概率。"""
    score: float
    reason: str
    source: str
    evidence: list[str] = field(default_factory=list)
    secondary_topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_topic": self.primary_topic,
            "subtopic": self.subtopic,
            "secondary_topics": list(self.secondary_topics),
            "classification_reason": self.reason,
            "classification_source": self.source,
            "confidence": self.confidence,
            "classification_score": round(self.score, 3),
            "classification_status": self.status,
            "classification_evidence": list(self.evidence),
        }


def _hit(text: str, phrase: str) -> bool:
    """短语在文本里是否出现。英文按词边界匹配，中文按子串。

    - 英文关键词允许**复数与词尾变形**：写 `language model` 也能命中 `language models`。
      论文摘要里复数比单数常见，不这么做会出现「明明写了 language models 却判不中」。
    - 缩写（`llm`/`llms`）按全大写形式也认一次。
    """
    if not text or not phrase:
        return False
    if re.search(r"[\u4e00-\u9fff]", phrase):
        return phrase in text
    escaped = re.escape(phrase)
    pattern = rf"(?<![a-z]){escaped}(?:s|es)?(?![a-z])"
    if re.search(pattern, text):
        return True
    # 纯字母的短缩写：再试一次全大写（LLM / LLMs / MoE 这类写法）
    if phrase.isalpha() and len(phrase) <= 5 and phrase.lower() == phrase:
        return re.search(rf"\b{re.escape(phrase.upper())}(?:S|ES)?\b", text.upper()) is not None
    return False


def _score_subtopic(
    rule: dict[str, Any], title: str, abstract: str, arxiv_hints: list[str]
) -> tuple[float, list[str], set[str]]:
    """给一条规则打分，返回 (分数, 证据短语, 命中的词条类型集合)。"""
    score = 0.0
    evidence: list[str] = []
    kinds: set[str] = set()
    gate = [p for p in rule.get("requires_any") or () if _hit(f"{title} {abstract}", p)]
    if not gate:
        return 0.0, [], set()

    title_hits = [p for p in rule.get("title") or () if _hit(title, p)]
    abstract_hits = [p for p in rule.get("abstract") or () if _hit(abstract, p)]
    if title_hits:
        # 标题命中给主分；多个不同关键词再加 0.5/个（上限 1.0），避免单条规则堆词刷分
        score += 2.0 + min(1.0, 0.5 * (len(title_hits) - 1))
        evidence.extend(f"标题含「{p}」" for p in title_hits[:3])
        kinds.add("title")
    if abstract_hits:
        score += 1.0 + min(0.5, 0.25 * (len(abstract_hits) - 1))
        evidence.extend(f"摘要含「{p}」" for p in abstract_hits[:2])
        kinds.add("abstract")
    if rule["subtopic"] in arxiv_hints:
        score += _ARXIV_ALONE_MAX
        evidence.append(f"arXiv 分类线索指向「{rule['subtopic']}」")
        kinds.add("arxiv")
    # 标题和摘要同时命中：两条独立证据，提一档可信度
    if {"title", "abstract"} <= kinds:
        score += 0.5
        evidence.append("标题与摘要同时命中")
    return score, evidence, kinds


def classify(
    filename: str = "",
    title: str = "",
    abstract: str = "",
    arxiv_category: str = "",
    manual: str = "",
) -> Classification:
    """给一篇论文分类。

    manual：人工指定的方向名（来自 `MANUAL_OVERRIDES`），命中则直接采信并标 `manual`。
    任何一项证据都不够时返回 `pending`，**不猜具体类别**。
    """
    if manual and manual in SUBTOPIC_FIELD:
        return Classification(
            primary_topic=SUBTOPIC_FIELD[manual],
            subtopic=manual,
            status="manual",
            confidence="high",
            score=_MIN_SCORE,
            reason="人工核对后指定",
            source="manual",
            evidence=["人工标注"],
        )

    t = (title or "").lower()
    a = (abstract or "").lower()
    hints = _ARXIV_HINT.get((arxiv_category or "").strip(), [])

    scored: list[tuple[float, str, list[str], set[str]]] = []
    for rule in _RULES:
        s, ev, kinds = _score_subtopic(rule, t, a, hints)
        # 证据强度分档：
        # - 标题命中（title_hit）：**可靠依据**，分数 ≥ _MIN_SCORE 即可判 classified；
        # - 只有摘要命中（abstract_only）：**弱依据**，要求更高分且至多算 medium 可信度。
        # 这样「Mistral 7B」这类只有型号名的标题不会因为缺摘要而永远 pending，
        # 也不会让摘要里随便一个常见词就定成具体方向。
        if "title" in kinds and s >= _MIN_SCORE:
            scored.append((s, rule["subtopic"], ev, kinds))
        elif "title" not in kinds and s >= _MIN_ABSTRACT_ONLY_SCORE:
            scored.append((s, rule["subtopic"], ev, kinds))
    if not scored:
        vague = "标题与摘要都没有足够具体的分类证据"
        has_any = bool(t.strip() or a.strip())
        return Classification(
            primary_topic=FIELD_PENDING,
            subtopic=FIELD_PENDING,
            status="pending",
            confidence="low",
            score=0.0,
            reason=vague + ("（标题或摘要缺失，无法判断）" if not has_any else ""),
            source="rule" if has_any else "none",
        )

    scored.sort(key=lambda x: (-x[0], x[1]))
    top_score, top_sub, top_ev, top_kinds = scored[0]
    # 第二名和第一名分数很接近时，把第二名也列进 secondary_topics，交给人工判断
    secondary = [x[1] for x in scored[1:3] if top_score - x[0] <= 1.0]
    if "title" in top_kinds:
        level = "high" if top_score >= 2.5 else "medium"
    else:
        # 只有摘要证据：绝对不给 high，可信度封顶 medium
        level = "medium"

    return Classification(
        primary_topic=SUBTOPIC_FIELD[top_sub],
        subtopic=top_sub,
        status="classified",
        confidence=level,
        score=top_score,
        reason="；".join(top_ev),
        source="rule",
        evidence=top_ev,
        secondary_topics=[f"{SUBTOPIC_FIELD[s]} / {s}" for s in secondary],
    )


# 人工兜底：规则确实判不了的单篇（扫描件标题是文件名、或论文本身跨类）。
# 每一条都要在验收报告里写明理由，并且 status 会标成 manual 供复查。
MANUAL_OVERRIDES: dict[str, str] = {
    # 图片版 Attention：文本层为空、摘要为空，但文件名与 v1/v7 同源，可确认
    "1706.03762v1_img.pdf": "Transformer",
}


def assign_all(items: list[dict]) -> list[dict]:
    """给 catalog 的每一条补分类字段（兼容旧字段）。

    写回的键：
    - `primary_topic` / `subtopic` / `secondary_topics` / `classification_*`：新字段
    - `category` / `topic`：**旧字段，值等于 `primary_topic`**，供问答与写作侧继续读取
    - `arxiv_category`：原样保留；缺失时从旧的 `category` 兜一次，但只在它明显是
      arXiv 学科名（形如 `cs.XX`/`stat.XX`）时才这么干，否则置空——老数据里
      `category` 已经是主旨分类，不能反过来污染 `arxiv_category`。
    """
    for item in items:
        # 键名两种都见过：catalog.json 用 `file`，调用方（app/main.py、测试）常传 `filename`。
        # 只认 `file` 会让 MANUAL_OVERRIDES 永远不生效——人工兜底被静默吞掉，
        # 该篇退化成 pending，状态分布也对不上（实测少 1 条 manual、多 1 条 pending）。
        filename = str(item.get("file") or item.get("filename") or "")
        arxiv_cat = str(item.get("arxiv_category") or "").strip()
        if not arxiv_cat:
            legacy = str(item.get("category") or "").strip()
            arxiv_cat = legacy if re.fullmatch(r"[a-z]{2,4}\.[A-Z]{2}", legacy) else ""
            item["arxiv_category"] = arxiv_cat
        res = classify(
            filename=filename,
            title=str(item.get("title") or ""),
            abstract=str(item.get("abstract") or ""),
            arxiv_category=arxiv_cat,
            manual=MANUAL_OVERRIDES.get(filename, ""),
        )
        item.update(res.to_dict())
        item["topic"] = res.primary_topic
        item["category"] = res.primary_topic
    return items