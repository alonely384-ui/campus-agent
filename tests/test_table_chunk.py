"""表格感知切块 + 页级兜底的单元测试。

这两个改动针对的是"答案在表格里"的题（如"BLEU 多少"）：
1. 表格行必须和表头在同一块里，否则数字不知道属于哪一列；
2. 数值类问题要把命中页整页补进上下文。
"""

from app import agent as agent_module
from app.rag import pipeline


def test_table_rows_keep_header():
    """表头 + 数据行必须在同一块，且每块都带表头。"""
    text = (
        "Model BLEU EN-DE BLEU EN-FR Training Cost (FLOPs)\n"
        "Transformer (base) 27.3 38.1 3.3 * 1018\n"
        "Transformer (big) 28.4 41.8 2.3 * 1019\n"
        "Our model achieves 28.4 BLEU on the WMT 2014 English-to-German task.\n"
    )
    chunks = pipeline.split_text(text, 120, 12)
    joined = "\n".join(chunks)
    assert "28.4" in joined
    # 含 28.4 的那一块必须同时含 BLEU 表头
    hit = [c for c in chunks if "28.4" in c]
    assert hit, "28.4 丢失"
    assert any("BLEU" in c for c in hit), "数字与表头被切散"


def test_table_chunks_never_exceed_size():
    text = (
        "Model BLEU EN-DE BLEU EN-FR Training Cost (FLOPs)\n"
        + "\n".join(f"Model v{i} {i}.1 {i}.2 1.{i} * 1019" for i in range(30))
        + "\n"
    )
    chunks = pipeline.split_text(text, 150, 15)
    assert chunks
    assert all(len(c) <= 150 for c in chunks)
    assert all("BLEU" in c for c in chunks), "每块都应重复表头"


def test_plain_text_unchanged():
    """普通段落不该被表格逻辑破坏，长度约束仍然成立。"""
    text = ("这是普通的中文段落，没有数字表格。" * 40) + "结束。"
    chunks = pipeline.split_text(text, 100, 10)
    assert chunks
    assert all(len(c) <= 100 for c in chunks)


def test_numeric_question_detection():
    assert agent_module._needs_page_text("训练用了多少块 GPU？")
    assert agent_module._needs_page_text("BLEU 是多少")
    assert not agent_module._needs_page_text("这篇论文讲了什么")


def test_page_text_fallback_injected(monkeypatch):
    """数值类问题要把命中页整页文本放进上下文。"""
    from app.rag.pipeline import Chunk

    monkeypatch.setattr(
        agent_module,
        "retrieve_scored",
        lambda q, top_k=4, source=None, use_dense=True: [
            (Chunk(text="Transformer (big) 28.4 41.8", source="x.pdf", page=8, chunk_id=1), 0.05, ["bm25"])
        ],
    )
    monkeypatch.setattr(agent_module, "page_text", lambda src, pg: "整页原文 含 28.4 BLEU")
    monkeypatch.setattr(agent_module, "paper_meta", lambda src: None)

    import asyncio

    context, evidence = asyncio.run(
        agent_module._rag_context_with_evidence("这篇论文的 BLEU 是多少？", source="x.pdf")
    )
    assert evidence
    assert "28.4" in context
    assert "【x.pdf 第8页 含数字的行" in context


def test_page_text_not_injected_for_general_question(monkeypatch):
    from app.rag.pipeline import Chunk

    monkeypatch.setattr(
        agent_module,
        "retrieve_scored",
        lambda q, top_k=4, source=None, use_dense=True: [
            (Chunk(text="Transformer 用自注意力替代循环结构", source="x.pdf", page=1, chunk_id=1), 0.05, ["bm25"])
        ],
    )
    monkeypatch.setattr(agent_module, "page_text", lambda src, pg: "整页原文")
    monkeypatch.setattr(agent_module, "paper_meta", lambda src: None)

    import asyncio

    context, _ = asyncio.run(agent_module._rag_context_with_evidence("这篇论文讲了什么？", source="x.pdf"))
    assert "整页原文" not in context
