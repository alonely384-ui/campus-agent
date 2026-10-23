"""重排（Cross-Encoder）的单元测试。

原则：测试不依赖 23MB 的模型文件，也不依赖真实 Chroma 库。
- 排序逻辑：把 `score_pairs` 换成一个假函数，验证"按分数降序 + 打上 rerank 标记"；
- 不可用回退：把模型目录指到空目录，验证抛出 RerankUnavailable（调用方据此回退）；
- 证据卡片：通道里带 rerank 时，score_type 必须是 rerank 而不是 rrf。
"""

import pytest

from app.agent import Evidence
from app.rag import rerank
from app.rag.pipeline import Chunk


def test_rerank_orders_by_cross_encoder_score(monkeypatch):
    items = [
        (Chunk(text="无关段落", source="a.pdf"), 0.03, ["dense"]),
        (Chunk(text="答案段落", source="a.pdf"), 0.02, ["bm25"]),
    ]
    # 关键：分数与融合分相反 —— 重排必须信模型分，不能信原来的 RRF 顺序
    monkeypatch.setattr(rerank, "score_pairs", lambda q, ps: [0.1, 0.9])

    out = rerank.rerank("问题", items, top_k=2)

    assert [chunk.text for chunk, _s, _ch in out] == ["答案段落", "无关段落"]
    assert out[0][2][-1] == "rerank"
    assert out[0][1] == pytest.approx(0.9)


def test_rerank_empty_input_returns_empty():
    assert rerank.rerank("问题", [], top_k=4) == []


def test_missing_model_raises_rerank_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(rerank, "_MODEL_DIR", tmp_path / "not-there")
    monkeypatch.setattr(rerank, "_session", None)
    monkeypatch.setattr(rerank, "_load_failed", False)

    with pytest.raises(rerank.RerankUnavailable):
        rerank.score_pairs("问题", ["段落"])


def test_evidence_marks_rerank_score_type():
    reranked = Evidence(
        rank=1, source="a.pdf", page=7, score=-4.07, channels=["dense", "rerank"], text="t"
    )
    plain = Evidence(
        rank=1, source="a.pdf", page=7, score=0.05, channels=["dense", "bm25"], text="t"
    )

    assert reranked.to_dict()["score_type"] == "rerank"
    assert plain.to_dict()["score_type"] == "rrf"
