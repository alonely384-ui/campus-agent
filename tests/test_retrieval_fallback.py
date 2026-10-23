"""检索容错测试：三路召回各自失败时，不能把整轮问答打崩。

覆盖：
1. 稠密失败 → 哈希接管权重，仍需返回结果；
2. 全通道为空 → 返回 []，不抛异常；
3. 权重接管的数值可验证（dense 缺席时 hash 拿 2.0 的权重）。
"""

import pytest

from app.rag import pipeline


def test_fuse_dense_missing_hash_takes_weight():
    """稠密缺席时，哈希应拿到 dense 的权重：同一条命中的分数 = 2.0/(60+1)。"""
    order, fused, channels = pipeline._fuse_ids([], ["a", "b"], [], top_k=2)
    assert order == ["a", "b"]
    assert fused["a"] == pytest.approx(2.0 / 61)
    assert channels["a"] == ["hash"]


def test_fuse_all_three_channels():
    order, fused, channels = pipeline._fuse_ids(["a"], ["b"], ["c"], top_k=3)
    assert set(order) == {"a", "b", "c"}
    # a 只有 dense：2.0/61；c 只有 bm25：1.0/61
    assert fused["a"] > fused["c"]
    assert channels["a"] == ["dense"]


def test_fuse_empty_returns_empty():
    order, fused, channels = pipeline._fuse_ids([], [], [], top_k=4)
    assert order == []
    assert fused == {}
    assert channels == {}


def test_retrieve_scored_dense_failure_still_returns(monkeypatch, tmp_path):
    """稠密模型不可用时（例如离线/模型损坏），哈希 + BM25 仍要返回结果。"""
    import chromadb

    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))

    class _Stub:
        def __init__(self, vec):
            self._vec = vec

        def name(self):
            return "stub"

        def is_legacy(self):
            return False

        def __call__(self, input):  # noqa: A002 —— Chroma 的协议参数名
            return [self._vec for _ in input]

    client = chromadb.PersistentClient(path=str(tmp_path))
    docs = client.get_or_create_collection(name="docs", embedding_function=_Stub([0.1] * pipeline.DIM))
    docs.upsert(
        ids=["p7:1"],
        documents=["We trained on 8 NVIDIA P100 GPUs"],
        metadatas=[{"source": "x.pdf", "page": 7, "chunk_id": 1}],
        embeddings=[[0.1] * pipeline.DIM],
    )

    def _boom():
        raise RuntimeError("onnx 模型加载失败")

    monkeypatch.setattr(pipeline, "_dense", _boom)
    monkeypatch.setattr(pipeline, "_load_bm25", lambda: None)
    monkeypatch.setattr(pipeline, "_IDF", {})

    out = pipeline.retrieve_scored("GPUs used for training", top_k=1)
    assert out, "稠密失败后不应返回空"
    chunk, _score, channels = out[0]
    assert chunk.source == "x.pdf"
    assert "hash" in channels
