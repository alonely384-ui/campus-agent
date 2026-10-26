"""按「主旨」给论文分类。

为什么不直接用 arXiv 学科分类：arXiv 的 primary_category 是按投稿版块给的，
"cs.CL 自然语言处理" 里既有架构论文（Attention）、也有训练技巧（LoRA）、
还有对齐（InstructGPT）。用户问"这些论文都是什么类型的"时，要的是"这些论文在干什么"，
所以这里按标题主旨重新归类，arXiv 分类继续保留在 `arxiv_category` 字段里备查。

规则是「有序匹配，先命中先得」：越具体的类别放越前面。
新增论文时，如果规则里没有它的关键词，会落到「基础模型与预训练」——这是最宽的一类；
跑 `scripts/reclassify_topics.py` 能看到每篇被分到哪、为什么，方便人工修正。
"""

from __future__ import annotations

# 类别名保持在 8 个字以内，方便在上下文里排版
TOPICS = [
    "教材与理论",
    "多模态模型",
    "训练效率与微调",
    "对齐与推理",
    "能力评测与规模",
    "模型架构",
    "基础模型与预训练",
]

# (类别, 标题关键词) —— 顺序即优先级
_TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("教材与理论", ("understanding deep learning",)),
    (
        "多模态模型",
        (
            "image is worth",
            "visual models",
            "visual instruction",
            "speech recognition",
            "multimodal",
            "vision transformer",
        ),
    ),
    (
        "训练效率与微调",
        (
            "zero: memory",
            "lora:",
            "qlora",
            "flashattention",
            "switch transformers",
            "efficient finetuning",
        ),
    ),
    (
        "对齐与推理",
        (
            "proximal policy optimization",
            "direct preference optimization",
            "follow instructions with human feedback",
            "chain-of-thought",
            "deepseek-r1",
            "reasoning capability",
        ),
    ),
    (
        "能力评测与规模",
        (
            "emergent abilities",
            "scaling laws",
            "evaluating large language models",
            "trained on code",
        ),
    ),
    (
        "模型架构",
        (
            "attention is all you need",
            "layer normalization",
            "rwkv",
            "transformer architecture",
        ),
    ),
]

# 规则兜不住的单篇（扫描件标题是文件名，或标题是缩写）
_FILE_OVERRIDES = {
    "1706.03762v1_img.pdf": "模型架构",
    "bdfaa68d8984.pdf": "模型架构",
}


def assign_topic(file: str, title: str = "") -> str:
    """返回该篇的主旨类别；规则都没命中时归到最宽的一类。"""
    if file in _FILE_OVERRIDES:
        return _FILE_OVERRIDES[file]
    text = f"{file} {title}".lower()
    for topic, keywords in _TOPIC_RULES:
        if any(k in text for k in keywords):
            return topic
    return "基础模型与预训练"


def assign_all(items: list[dict]) -> list[dict]:
    """给 catalog 的每一条补 `topic`；原 `category`（arXiv 分类）改名保留为 `arxiv_category`。"""
    for item in items:
        if "arxiv_category" not in item:
            item["arxiv_category"] = item.get("category") or ""
        item["topic"] = assign_topic(str(item.get("file") or ""), str(item.get("title") or ""))
        # 问答侧读 `category`，所以把主旨写回 category；arXiv 分类留在 arxiv_category
        item["category"] = item["topic"]
    return items
