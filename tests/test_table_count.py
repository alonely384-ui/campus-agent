"""数量类问题（"一共几个/几种"）走表格补页的逻辑测试。

背景：这类问题的答案经常是表格的行数，而单个切片进不了 top4（实测 p8 表 2.1 有 8 行，
向量/BM25 都把 p9 排前面）。所以单篇问答时会在全篇找"最像答案的表格页"补进上下文，
并把补进来的页也写进 evidence，保证前端证据卡片和回答里的页码一致。
"""

from __future__ import annotations

import pytest

from app.agent import (
    Evidence,
    _is_count_question,
    _rag_context_with_evidence,
    _table_pages,
)
from app.rag.pipeline import Chunk

# 第 1 页：正文段落，有"参数"字样但不是表格
PAGE_1 = """We train 8 different sizes of model, ranging over three orders of magnitude
from 125 million parameters to 175 billion parameters, with the last being the
model we call GPT-3. The exact sizes are listed in the table below.
"""

# 第 2 页：表格页 + "规模/结构"表题
PAGE_2 = """GPT-3 Small 125M 12 768 12 64 0.5M 6.0x 1e-4
GPT-3 Medium 350M 24 1024 16 64 1.0M 3.0x 1e-4
GPT-3 Large 760M 24 1536 16 96 2.5M 2.5x 1e-4
GPT-3 XL 1.3B 24 2048 24 128 3.2M 2.0x 1e-4
Table 2.1: Sizes, architectures, and learning hyper-parameters of the models.
"""

# 第 3 页：另一张大表，但没有"规模"表题（用来验证表题加权）
PAGE_3 = """GPT-3 Small 25.0 10.0 5.0 2.0 1.0 0.5
GPT-3 Medium 30.0 12.0 6.0 3.0 2.0 1.0
GPT-3 Large 35.0 14.0 7.0 4.0 3.0 1.5
GPT-3 XL 40.0 16.0 8.0 5.0 4.0 2.0
Table 3.3: Results on three Open-Domain QA tasks.
"""

PAGES = {1: PAGE_1, 2: PAGE_2, 3: PAGE_3, 4: ""}


def _page_text(source: str, page: int) -> str:
    return PAGES.get(page, "")


def test_is_count_question_detects_chinese_count_words() -> None:
    assert _is_count_question("GPT-3 一共训了几个不同规模的模型？")
    assert _is_count_question("这篇论文用了多少种数据集？")
    # 问具体数值（不是"几个/几种"）不算数量类
    assert not _is_count_question("训练用了多少块 GPU？")


def test_table_pages_ranks_size_table_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agent.page_text", _page_text)
    pages = _table_pages("x.pdf", "GPT-3 一共有几种模型规模？", max_pages=4)
    assert pages, "应当找到候选表格页"
    assert pages[0] == (2, 24), f"表题含 Sizes 的表应排第一，实际 {pages}"
    # 第 3 页表格也含 GPT-3 行，但没有"规模"表题，只能排后面
    assert 3 in [p for p, _ in pages]


def test_table_pages_returns_empty_without_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agent.page_text", _page_text)
    assert _table_pages("x.pdf", "它到底有多大？", max_pages=4) == []


@pytest.mark.asyncio
async def test_count_question_injects_table_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """数量类问题：上下文里要有候选表格，evidence 里也要有对应页。"""

    monkeypatch.setattr("app.agent.page_text", _page_text)
    chunk = Chunk(text="我们训练了不同规模的模型", source="x.pdf", page=1, chunk_id=1)

    def _fake_retrieve(question: str, top_k: int = 4, source: str | None = None):
        return [(chunk, 0.05, ["dense"])]

    monkeypatch.setattr("app.agent.retrieve_scored", _fake_retrieve)
    monkeypatch.setattr("app.agent.paper_meta", lambda source: None)

    context, evidence = await _rag_context_with_evidence(
        "GPT-3 一共有几种模型规模？", "x.pdf", None
    )

    assert "候选表格" in context
    assert "第2页" in context
    table_ev = [e for e in evidence if e.channels == ["table"]]
    assert table_ev, "补进来的表格页必须进 evidence"
    assert table_ev[0].page == 2
    assert isinstance(table_ev[0], Evidence)
    assert table_ev[0].score == 0.0
