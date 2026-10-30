"""精华卡相关测试：渲染、缺失降级、注入上下文与证据。"""

from __future__ import annotations

import asyncio

from app import agent as agent_module
from app import cards


def _sample_card() -> dict:
    return {
        "title": "Direct Preference Optimization",
        "abstract": "We introduce DPO, a simple approach to fine-tune language models on human preferences.",
        "problem": "如何在不训练奖励模型的情况下对齐人类偏好",
        "method": "把 RLHF 目标改写成闭式偏好损失，直接用偏好数据微调",
        "contributions": ["提出 DPO", "无需显式奖励模型"],
        "results": ["在多个任务上优于 PPO 基线"],
        "limitations": ["摘要未提及"],
        "keywords": ["偏好优化", "RLHF"],
        "generated_by": "llm",
    }


def test_render_card_keeps_field_order():
    text = cards.render_card(_sample_card())
    for label in ("标题：", "摘要原文：", "研究问题：", "方法：", "贡献：", "结果：", "局限：", "关键词："):
        assert label in text
    assert text.index("研究问题：") < text.index("方法：") < text.index("贡献：")


def test_render_card_truncates():
    card = _sample_card()
    card["abstract"] = "x" * 2000
    assert len(cards.render_card(card, max_chars=200)) <= 200


def test_missing_card_returns_none():
    assert cards.get_card("不存在的文件.pdf") is None


def test_explain_question_gets_card_block(monkeypatch):
    monkeypatch.setattr(agent_module, "_search_query", _fake_query)
    monkeypatch.setattr(agent_module, "retrieve_scored", _fake_retrieve)
    monkeypatch.setattr(agent_module, "get_card", lambda source: _sample_card() if source == "dpo.pdf" else None)
    monkeypatch.setattr(agent_module, "paper_meta", lambda source: None)
    monkeypatch.setattr(agent_module, "find_paper_by_title", lambda question: None)

    context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("这篇论文的研究问题是什么", source="dpo.pdf")
    )
    assert "精华卡" in context
    assert "研究问题" in context
    assert any(ev.channels == ["card"] and ev.page == 1 for ev in evidence)


def test_content_question_skips_card(monkeypatch):
    monkeypatch.setattr(agent_module, "_search_query", _fake_query)
    monkeypatch.setattr(agent_module, "retrieve_scored", _fake_retrieve)
    monkeypatch.setattr(agent_module, "get_card", lambda source: _sample_card() if source == "dpo.pdf" else None)
    monkeypatch.setattr(agent_module, "paper_meta", lambda source: None)
    monkeypatch.setattr(agent_module, "find_paper_by_title", lambda question: None)

    context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("训练用了多少 GPU", source="dpo.pdf")
    )
    assert "精华卡" not in context
    assert all(ev.channels != ["card"] for ev in evidence)


async def _fake_query(question: str) -> str:
    return question


def test_category_question_injects_cards(monkeypatch):
    """「强化学习是哪三篇，他们的主旨是？」：按类别元数据取篇目，注 3 张卡。"""
    catalog = [
        {"file": "ppo.pdf", "title": "PPO", "category": "强化学习", "indexed": True},
        {"file": "dpo.pdf", "title": "DPO", "category": "强化学习", "indexed": True},
        {"file": "r1.pdf", "title": "DeepSeek-R1", "category": "强化学习", "indexed": True},
        {"file": "bert.pdf", "title": "BERT", "category": "自然语言处理", "indexed": True},
    ]
    monkeypatch.setattr(agent_module, "load_catalog", lambda: catalog)
    monkeypatch.setattr(agent_module, "_search_query", _fake_query)
    monkeypatch.setattr(agent_module, "retrieve_scored", _fake_retrieve)
    monkeypatch.setattr(agent_module, "get_card", lambda source: _sample_card())
    monkeypatch.setattr(agent_module, "paper_meta", lambda source: None)
    monkeypatch.setattr(agent_module, "find_paper_by_title", lambda question: None)

    context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("强化学习是哪三篇，他们的主旨是？")
    )
    cards = [ev for ev in evidence if ev.channels == ["card"]]
    assert {ev.source for ev in cards} == {"ppo.pdf", "dpo.pdf", "r1.pdf"}
    assert "精华卡" in context


class _FakeChunk:
    def __init__(self, text: str, source: str, page: int) -> None:
        self.text, self.source, self.page = text, source, page


def _fake_retrieve(question: str, top_k: int = 4, source: str | None = None, use_dense: bool = True):
    chunk = _FakeChunk("We used 8 NVIDIA V100 GPUs for training.", source or "dpo.pdf", 5)
    return [(chunk, 0.05, ["dense", "bm25"])]


def _catalog_named() -> list[dict]:
    return [
        {
            "file": "1707.06347v2.pdf",
            "title": "Proximal Policy Optimization Algorithms",
            "category": "强化学习",
            "indexed": True,
        },
        {
            "file": "2305.18290v3.pdf",
            "title": "Direct Preference Optimization",
            "category": "强化学习",
            "indexed": True,
        },
    ]


def _patch_named(monkeypatch):
    monkeypatch.setattr(agent_module, "load_catalog", _catalog_named)
    monkeypatch.setattr(agent_module, "_search_query", _fake_query)
    monkeypatch.setattr(agent_module, "retrieve_scored", _fake_retrieve)
    monkeypatch.setattr(agent_module, "get_card", lambda source: _sample_card())
    monkeypatch.setattr(agent_module, "paper_meta", lambda source: None)
    monkeypatch.setattr(agent_module, "find_paper_by_title", lambda question: None)


def test_named_file_question_injects_card(monkeypatch):
    """「总结一下 1707.06347v2.pdf」：按文件名点名就该出这篇的卡。"""
    _patch_named(monkeypatch)
    context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("总结一下 1707.06347v2.pdf")
    )
    cards = [ev for ev in evidence if ev.channels == ["card"]]
    assert {ev.source for ev in cards} == {"1707.06347v2.pdf"}
    assert "精华卡" in context


def test_abbreviation_question_injects_card(monkeypatch):
    """「PPO 那篇讲了什么」：缩写要展开成全称去对标题，不能锁到别的论文。"""
    _patch_named(monkeypatch)
    _context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("PPO 那篇讲了什么")
    )
    cards = [ev for ev in evidence if ev.channels == ["card"]]
    assert {ev.source for ev in cards} == {"1707.06347v2.pdf"}


def test_history_reference_picks_first_paper(monkeypatch):
    """「这三篇中第一篇的主旨」：篇名在上一轮回答里，按序号取第一篇的卡。"""
    _patch_named(monkeypatch)
    history = [
        {
            "role": "assistant",
            "content": "对齐与推理共 5 篇：1707.06347v2.pdf、2305.18290v3.pdf",
        }
    ]
    assert agent_module._history_files(history) == ["1707.06347v2.pdf", "2305.18290v3.pdf"]
    _context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("这三篇中第一篇的主旨是啥", history=history)
    )
    cards = [ev for ev in evidence if ev.channels == ["card"]]
    assert {ev.source for ev in cards} == {"1707.06347v2.pdf"}
