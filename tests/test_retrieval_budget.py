"""检索预算与意图锚的守卫测试。

背景（2026-11-16 实测，见 app/agent.py 里 _INTENT_TERMS 与 PER_PAPER_TOP_K 的注释）：
官方 2.2「多篇论文问答」的四道题里有三道要"查具体数字"（参数量、上下文窗口长度）。
改之前，问「GPT-3 的最大上下文窗口长度是多少 token」被译成
"GPT-3 maximum context window token length"，目标块（含 nctx = 2048）排在 top5，
而多篇对比每篇只取 3 块 → 根本进不了证据池，系统只能拒答。

两组改动缺一不可：
  1. 意图锚：把 maximum/length 这类原文里几乎不出现的词挤开，补上 nctx / context window；
     实测 top5 → top1。
  2. 预算：把 PER_PAPER_TOP_K 从 3 提到 5；GPT-1 的 "sequences of 512 tokens"
     在 top5 才出现（前三名全是公式块）。

这些测试锁住"行为"，不锁死具体数字来源，避免以后调参时悄悄把可答变成不可答。
"""

from __future__ import annotations

import app.agent as agent_module


def test_context_window_question_gets_nctx_anchor():
    """问上下文窗口时，锚点必须同时覆盖三篇的原文写法。

    2026-11-16 修正：最初只补 "context window nctx"，实测 GPT-2 目标块掉到第 8、
    GPT-1 直接 top8 之外（GPT-2 原文写 "context size"，GPT-1 只在训练设置里写
    "sequences of 512 tokens"）。改成 "context size nctx 512 1024" 后三篇都进 top3。
    所以这里同时断言 context size / nctx / 512 —— 缺任何一个都会让某篇召回不到。
    """
    got = agent_module._intent_terms("GPT-3 的最大上下文窗口长度是多少 token？")
    assert "context size" in got
    assert "nctx" in got
    assert "512" in got
    assert "1024" in got


def test_parameter_question_gets_parameter_anchor():
    got = agent_module._intent_terms("GPT-1 模型的参数量是多少？")
    assert "parameters" in got


def test_heads_question_does_not_drag_in_parameter_terms():
    """问头数时不该补 parameters/model size——那会把规格表之外的内容顶上来。"""
    got = agent_module._intent_terms("注意力头数 h 是多少？")
    assert "attention heads" in got
    assert "parameters" not in got


def test_batch_size_question_is_not_treated_as_model_size():
    """「batch size」里的 size 不能触发模型规模锚（早先版本会误触发）。"""
    got = agent_module._intent_terms("训练用的 batch size 是多少？")
    assert "batch size" in got
    assert "model size" not in got


def test_layernorm_question_is_not_treated_as_layer_count():
    """LayerNorm 里含 "layer"，但问的是归一化不是层数。"""
    got = agent_module._intent_terms("LayerNorm 对应哪条参考文献")
    assert "references citation" in got
    assert "n_layers" not in got


def test_plain_question_gets_no_anchor():
    assert agent_module._intent_terms("这篇论文的 BLEU 是多少？") == ""


def test_merge_terms_appends_anchor_without_dropping_original():
    """补词只能追加，不能改写原查询——原查询里的实体名是分篇检索的定位依据。"""
    merged = agent_module._merge_terms("GPT-3 maximum context window token length", "GPT-3 的最大上下文窗口长度是多少 token？")
    assert merged.startswith("GPT-3 maximum context window token length")
    assert "nctx" in merged


def test_per_paper_top_k_covers_the_observed_ranking():
    """每篇证据预算必须覆盖实测排名第 5 的那块（GPT-1 的 512 tokens 片段）。"""
    assert agent_module.PER_PAPER_TOP_K >= 5


def test_multi_max_evidence_still_caps_the_prompt():
    """预算放宽了，但喂给模型的总条数仍要有上限，否则长文档会撑爆上下文。"""
    assert agent_module.MULTI_MAX_EVIDENCE <= 12
    assert agent_module.MULTI_MAX_EVIDENCE >= agent_module.RETRIEVE_TOP_K