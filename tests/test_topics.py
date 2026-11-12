"""论文分类体系测试。

覆盖本次改造的关键承诺：
- 没有任何规则命中时**不得**落进具体类别，必须是 pending；
- 只看 arXiv 字段不足以确定具体方向；
- 核心样本（Mamba / BERT / T5 / GPT-2）归类正确；
- 多标签不破坏主分类兼容性；
- 新旧字段同时写回，旧调用方（agent/literature/graph）不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import topics


def _cls(**kwargs):
    base = {"filename": "", "title": "", "abstract": "", "arxiv_category": ""}
    base.update(kwargs)
    return topics.classify(**base)


# --- 兜底语义 -------------------------------------------------------------


def test_no_rule_hit_goes_pending_not_a_specific_field():
    """原本的缺陷：规则零命中会被塞进「基础模型与预训练」。现在必须是待归类。"""
    got = _cls(title="Some Unrelated Work On Something Else", abstract="Nothing matches here.")
    assert got.status == "pending"
    assert got.primary_topic == topics.FIELD_PENDING
    assert got.primary_topic not in (topics.FIELD_MODELS, topics.FIELD_ARCH, topics.FIELD_ALIGN)


def test_pending_paper_is_not_counted_in_any_real_field():
    got = _cls(title="Untitled Scan", abstract="")
    assert got.to_dict()["primary_topic"] == topics.FIELD_PENDING
    assert got.to_dict()["classification_status"] == "pending"
    assert got.to_dict()["confidence"] == "low"


def test_arxiv_category_alone_never_decides_a_subtopic():
    """arXiv 学科分类是辅助线索，不能单独定成具体研究方向。"""
    got = _cls(title="A Study Of Things", abstract="", arxiv_category="cs.CL")
    assert got.status == "pending"


# --- 核心样本 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "title", "abstract", "field", "subtopic"),
    [
        (
            "2312.00752.pdf",
            "Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
            "We propose Mamba, a selective state space model.",
            topics.FIELD_ARCH,
            "状态空间与线性时间模型",
        ),
        (
            "N19-1423.pdf",
            "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
            "We introduce a new language representation model called BERT, a deep bidirectional encoder.",
            topics.FIELD_MODELS,
            "双向预训练模型",
        ),
        (
            "1910.10683v4.pdf",
            "Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer",
            "We explore a unified text-to-text transformer, T5.",
            topics.FIELD_MODELS,
            "文本到文本模型",
        ),
        (
            "language_understanding_paper.pdf",
            "Improving Language Understanding by Generative Pre-Training",
            "We use a generative pre-training of a language model on a diverse corpus.",
            topics.FIELD_MODELS,
            "自回归语言模型",
        ),
    ],
)
def test_core_samples_classified_correctly(filename, title, abstract, field, subtopic):
    got = _cls(filename=filename, title=title, abstract=abstract)
    assert got.status == "classified"
    assert got.primary_topic == field
    assert got.subtopic == subtopic


def test_mamba_is_not_dumped_into_generic_pretraining():
    """Mamba 以前被规则遗漏丢进「基础模型与预训练」，这是明确的错误分类。"""
    got = _cls(
        title="Mamba: Linear-Time Sequence Modeling with Selective State Spaces",
        abstract="We propose Mamba, a selective state space model.",
    )
    assert got.primary_topic != topics.FIELD_MODELS


def test_manual_override_is_trusted_and_labelled():
    got = _cls(filename="1706.03762v1_img.pdf", title="", manual="Transformer")
    assert got.status == "manual"
    assert got.source == "manual"
    assert got.subtopic == "Transformer"


# --- 词边界与复数 ---------------------------------------------------------


def test_hit_requires_word_boundary():
    """`rag` 不能命中 `storage`，否则常见词会把论文乱分类。"""
    assert topics._hit("storage systems", "rag") is False
    assert topics._hit("we use a rag pipeline", "rag") is True


def test_hit_accepts_plural_forms():
    """摘要里复数比单数常见，写单数关键词也要能命中复数文本。"""
    assert topics._hit("large language models are", "language model") is True
    assert topics._hit("a large language model is", "language model") is True


def test_hit_accepts_uppercase_abbreviation():
    assert topics._hit("a series of large language models (LLMs)", "llm") is True


def test_hit_handles_chinese_by_substring():
    assert topics._hit("这是一篇关于引用图谱的论文", "引用图谱") is True
    assert topics._hit("这是一篇论文", "引用图谱") is False


# --- 多标签与兼容性 -------------------------------------------------------


def test_secondary_topics_do_not_replace_primary():
    got = _cls(
        title="LoRA: Low-Rank Adaptation of Large Language Models",
        abstract="LoRA is a parameter-efficient finetuning method for large language models.",
    )
    assert got.status == "classified"
    assert got.primary_topic  # 主分类必须有值
    assert got.primary_topic == topics.SUBTOPIC_FIELD[got.subtopic]
    for item in got.secondary_topics:
        assert "/" in item


def test_assign_all_writes_legacy_fields_equal_to_primary():
    """`category`/`topic` 是旧字段，问答与写作侧还在读，必须等于主分类。"""
    items = [
        {"file": "2312.00752.pdf", "title": "Mamba: Linear-Time Sequence Modeling with Selective State Spaces"},
        {"file": "unknown.pdf", "title": "Whatever"},
    ]
    out = topics.assign_all(items)
    for row in out:
        assert row["category"] == row["primary_topic"]
        assert row["topic"] == row["primary_topic"]
        assert row["classification_status"] in ("classified", "pending", "manual")


def test_assign_all_preserves_arxiv_category_that_is_a_real_category():
    items = [{"file": "a.pdf", "title": "Untitled", "arxiv_category": "cs.CL"}]
    out = topics.assign_all(items)
    assert out[0]["arxiv_category"] == "cs.CL"


def test_assign_all_does_not_pollute_arxiv_category_with_topic_name():
    """老数据里 `category` 是主旨分类名，不能当成 arXiv 学科名塞进 arxiv_category。"""
    items = [{"file": "a.pdf", "title": "Untitled", "category": "模型架构"}]
    out = topics.assign_all(items)
    assert out[0]["arxiv_category"] == ""


def test_manual_override_applies_with_legacy_file_key():
    """catalog.json 用 `file` 作文件名键，人工兜底必须生效。"""
    items = [{"file": "1706.03762v1_img.pdf", "title": "1706.03762v1_img"}]
    out = topics.assign_all(items)
    assert out[0]["classification_status"] == "manual"
    assert out[0]["primary_topic"] == "模型架构"
    assert out[0]["subtopic"] == "Transformer"
    assert out[0]["classification_source"] == "manual"


def test_manual_override_applies_with_filename_key():
    """调用方（app/main.py、测试）习惯传 `filename`，不能因为键名不同就静默失效。

    回归：旧实现只读 `item["file"]`，导致 MANUAL_OVERRIDES 永远不生效，
    该篇退化成 pending —— 前端会多显示 1 篇「待归类」。
    """
    items = [{"filename": "1706.03762v1_img.pdf", "title": "1706.03762v1_img"}]
    out = topics.assign_all(items)
    assert out[0]["classification_status"] == "manual"
    assert out[0]["subtopic"] == "Transformer"


def test_manual_override_does_not_leak_to_other_files():
    """人工兜底只对它点名的那一篇生效。"""
    items = [
        {"filename": "1706.03762v1_img.pdf", "title": "1706.03762v1_img"},
        {"filename": "other.pdf", "title": "Totally Unrelated"},
    ]
    out = topics.assign_all(items)
    assert out[1]["classification_status"] == "pending"
    assert out[1]["classification_source"] != "manual"


def test_classification_is_deterministic():
    rows = [
        {"file": "x1.pdf", "title": "LoRA: Low-Rank Adaptation of Large Language Models"},
        {"file": "x2.pdf", "title": "Layer Normalization"},
    ]
    first = [r["primary_topic"] for r in topics.assign_all([dict(r) for r in rows])]
    for _ in range(5):
        again = [r["primary_topic"] for r in topics.assign_all([dict(r) for r in rows])]
        assert again == first


# --- 真实语料回归 ---------------------------------------------------------


_CATALOG = Path(__file__).resolve().parent.parent / "data" / "catalog.json"


@pytest.mark.skipif(not _CATALOG.exists(), reason="catalog.json 不在本机")
def test_real_catalog_has_no_paper_silently_dumped_into_a_field():
    """真实语料上，没有任何一条是「零证据却被判成具体领域」。"""
    rows = json.loads(_CATALOG.read_text(encoding="utf-8"))
    out = topics.assign_all(rows)
    for row in out:
        if row["classification_status"] == "pending":
            assert row["primary_topic"] == topics.FIELD_PENDING
        else:
            assert row["primary_topic"] != topics.FIELD_PENDING
            assert row["classification_reason"], f"{row['file']} 没有给出分类依据"


@pytest.mark.skipif(not _CATALOG.exists(), reason="catalog.json 不在本机")
def test_real_catalog_pending_rate_is_low_but_nonzero():
    """允许存在待归类，但不应大面积待归类（要求覆盖率，也不为覆盖率降标准）。

    catalog.json 里**没有摘要**，摘要要从 structures.json 合并。只给标题时
    Mistral 7B / Qwen2.5 / Llama 3 这类只有型号名的标题无法判断，属于已知限制，
    所以这里的分母用「能拿到摘要的篇数」来算，并单独断言纯标题路径下仍会降级。
    """
    rows = json.loads(_CATALOG.read_text(encoding="utf-8"))
    out = topics.assign_all(rows)
    pending = [r for r in out if r["classification_status"] == "pending"]
    assert len(pending) <= 8, [r["file"] for r in pending]
    # 纯标题路径：型号名类标题应判 pending，而不是硬猜一个方向
    mistral = [r for r in out if r["file"] == "2310.06825v1.pdf"][0]
    assert mistral["classification_status"] == "pending"


def test_abstract_only_evidence_caps_confidence_at_medium():
    """只有摘要证据时可以判 classified，但可信度绝不能到 high。"""
    got = _cls(
        title="Megatron-Bridge 42B",  # 型号名不在规则词表里，只能靠摘要
        abstract="We introduce a 42-billion-parameter language model. "
        "It is a Mixture-of-Experts (MoE) language model variant.",
    )
    assert got.status == "classified"
    assert got.confidence == "medium"
    assert "摘要含" in got.reason
    assert "标题含" not in got.reason


def test_single_abstract_keyword_is_not_enough():
    """摘要里随便出现一个关键词不足以定方向（否则常见词会泛滥）。"""
    got = _cls(title="Some Manual", abstract="We use a language model for this.")
    assert got.status == "pending"