"""对比类问题自动选篇 + 参考文献片段识别（A/B/C 三件事的回归测试）。"""

import pytest

from app import agent as agent_module
from app.rag import pipeline


def test_catalog_block_lists_members_of_each_category():
    block = agent_module._catalog_block(limit=4000)
    assert "【对齐与推理】5 篇" in block
    assert "1707.06347v2.pdf" in block  # PPO：对齐与推理名单里必须有它
    assert "Proximal Policy" in block


def test_plan_multi_sources_uses_category_and_count():
    files = agent_module._plan_multi_sources("三篇对比，对齐与推理方向的论文主要内容和侧重")
    assert len(files) == 3
    assert "1707.06347v2.pdf" in files
    catalog = {i["file"]: i["category"] for i in agent_module.load_catalog()}
    assert all(catalog.get(f) == "对齐与推理" for f in files)


def test_plan_multi_sources_respects_explicit_count():
    files = agent_module._plan_multi_sources("对比两篇对齐与推理的论文")
    assert len(files) == 2


def test_plan_multi_sources_returns_empty_without_clues():
    # 既没类别、也没点名论文：不瞎选，让模型提示用户去勾选
    assert agent_module._plan_multi_sources("三篇对比，主要内容和侧重") == []


def test_wants_compare_detection():
    assert agent_module._wants_compare("对比三篇的差异")
    assert not agent_module._wants_compare("多少个不同规模的模型")


def test_looks_like_citation():
    ref = (
        "F. Wu et al. Hierarchical reinforcement learning: A comprehensive survey. "
        "ACM Computing Surveys, 54(5):1-35, 2022. https://doi.org/10.1145/123"
    )
    assert pipeline._looks_like_citation(ref)
    body = (
        "We trained our models on one machine with 8 NVIDIA P100 GPUs. "
        "Each training batch contained approximately 0.25 million tokens."
    )
    assert not pipeline._looks_like_citation(body)
    # 长正文即使引用格式多，也不判成参考文献（避免误伤 Related Work）
    assert not pipeline._looks_like_citation(ref * 10)


@pytest.mark.asyncio
async def test_citation_chunk_gets_marker_in_context(monkeypatch):
    from app.rag.pipeline import Chunk

    fake_chunk = Chunk(
        text=(
            "F. Wu et al. Hierarchical reinforcement learning: A comprehensive survey. "
            "ACM Computing Surveys, 54(5):1-35, 2022. https://doi.org/10.1145/123"
        ),
        source="2407.21783v3.pdf",
        page=81,
    )

    async def _fake_query(_q):
        return _q

    monkeypatch.setattr(agent_module, "_search_query", _fake_query)
    monkeypatch.setattr(
        agent_module, "retrieve_scored", lambda q, top_k, source=None, use_dense=True: [(fake_chunk, 0.5, ["dense"])]
    )
    context, evidence = await agent_module._rag_context_with_evidence("推荐一篇强化学习的论文")
    assert "【参考文献列表片段" in context
    # 清单问题会额外插入一条 catalog 证据，所以按来源找而不是看第 0 条
    assert any(ev.source == "2407.21783v3.pdf" for ev in evidence)
