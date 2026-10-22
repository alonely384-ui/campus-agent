"""RAG 接入 Agent 的测试：检索结果要真的进入 messages，且带来源页码。"""

from types import SimpleNamespace

import pytest

import app.agent as agent_module
from app.agent import _rag_context, _rag_context_with_evidence, run_agent
from app.rag.pipeline import Chunk


async def test_rag_context_has_source_and_page(monkeypatch):
    def fake_retrieve(question, top_k=4, source=None):
        assert "GPUs" in question
        return [
            (
                Chunk(text="8 NVIDIA P100 GPUs", source="1706.03762v7.pdf", page=7),
                0.057,
                ["dense", "bm25"],
            )
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    context = await _rag_context("How many GPUs were used for training?")

    assert "1706.03762v7.pdf 第7页" in context
    assert "8 NVIDIA P100 GPUs" in context
    # 没检索到时必须拒答，这句提示要在
    assert "论文库中没有找到相关信息" in context


async def test_evidence_card_fields(monkeypatch):
    """证据卡片要有：排名、页码、融合分、命中通道、原文。"""

    def fake_retrieve(question, top_k=4, source=None):
        return [
            (Chunk(text="8 NVIDIA P100 GPUs", source="p.pdf", page=7), 0.057, ["dense"]),
            (Chunk(text="28.4 BLEU", source="p.pdf", page=8), 0.055, ["bm25"]),
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    _context, evidence = await _rag_context_with_evidence("训练用了多少 GPU？")

    assert len(evidence) == 2
    first = evidence[0].to_dict()
    assert first["rank"] == 1
    assert first["page"] == 7
    assert first["score_type"] == "rrf"
    assert first["channels"] == ["dense"]
    assert "NVIDIA P100" in first["text"]


async def test_rag_context_survives_retrieve_error(monkeypatch):
    def boom(question, top_k=4, source=None):
        raise RuntimeError("chroma 没建好")

    monkeypatch.setattr(agent_module, "retrieve_scored", boom)
    assert await _rag_context("任何问题") == ""


async def test_rag_context_empty_when_no_hits(monkeypatch):
    monkeypatch.setattr(agent_module, "retrieve_scored", lambda q, top_k=4, source=None: [])
    assert await _rag_context("库里没有的问题") == ""


@pytest.mark.asyncio
async def test_run_agent_passes_context_to_model(monkeypatch):
    captured: list[list[dict]] = []

    def fake_retrieve(question, top_k=4, source=None):
        return [
            (Chunk(text="We trained on 8 NVIDIA P100 GPUs", source="p.pdf", page=7), 0.06, ["dense"])
        ]

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs["messages"])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="答案 [来源: p.pdf 第7页]", tool_calls=None)
                    )
                ]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    monkeypatch.setattr(agent_module, "get_client", lambda: fake_client)

    result = await run_agent("训练用了多少 GPU？")

    assert result.answer.startswith("答案")
    messages = captured[0]
    # 第 0 条是系统人设，第 1 条必须是检索到的参考资料
    assert messages[0]["role"] == "system"
    assert "p.pdf 第7页" in messages[1]["content"]
    assert messages[-1]["role"] == "user"


@pytest.fixture(autouse=True)
def _no_translate(monkeypatch):
    """单测里不要真的调 LLM 做查询翻译：直接原样返回。"""

    async def _fake(q):
        return q

    monkeypatch.setattr(agent_module, "_search_query", _fake)