"""检索容错测试：三路召回各自失败时，不能把整轮问答打崩。

覆盖：
1. 稠密失败 → 哈希接管权重，仍需返回结果；
2. 全通道为空 → 返回 []，不抛异常；
3. 权重接管的数值可验证（dense 缺席时 hash 拿 2.0 的权重）。
"""

import pytest

from app.rag import pipeline


def test_fuse_dense_missing_hash_takes_weight():
    """稠密缺席时，哈希应拿到 dense 的权重。

    这里不能写死 2.0/61：RAG_W_DENSE 与 RAG_RRF_K 都是可在设置页改、
    并由 .env 填充的配置，开发机上 .env 有非默认值时这条断言就会误报。
    实际值一律从模块常量算，测的是「权重接管」这个行为本身。
    """
    order, fused, channels = pipeline._fuse_ids([], ["a", "b"], [], top_k=2)
    assert order == ["a", "b"]
    assert fused["a"] == pytest.approx(pipeline._W_DENSE / (pipeline._RRF_K + 1))
    assert channels["a"] == ["hash"]


def test_fuse_dense_missing_hash_takes_weight_with_defaults(monkeypatch):
    """把权重与 RRF 常数钉成文档里的默认值（2.0 / 60），验证数值口径没被改坏。"""
    monkeypatch.setattr(pipeline, "_W_DENSE", 2.0)
    monkeypatch.setattr(pipeline, "_RRF_K", 60)
    _, fused, _ = pipeline._fuse_ids([], ["a", "b"], [], top_k=2)
    assert fused["a"] == pytest.approx(2.0 / 61)


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


def _stub_docs(tmp_path):
    """建一个只有哈希通道的 docs 集合，返回 chromadb client。"""
    import chromadb

    monkeypatch_dir = str(tmp_path)

    class _Stub:
        def __init__(self, vec):
            self._vec = vec

        def name(self):
            return "stub"

        def is_legacy(self):
            return False

        def __call__(self, input):  # noqa: A002 —— Chroma 的协议参数名
            return [self._vec for _ in input]

    client = chromadb.PersistentClient(path=monkeypatch_dir)
    docs = client.get_or_create_collection(
        name="docs", embedding_function=_Stub([0.1] * pipeline.DIM)
    )
    docs.upsert(
        ids=["p7:1"],
        documents=["We trained on 8 NVIDIA P100 GPUs"],
        metadatas=[{"source": "x.pdf", "page": 7, "chunk_id": 1}],
        embeddings=[[0.1] * pipeline.DIM],
    )
    return client


def test_use_dense_false_skips_dense_channel(monkeypatch, tmp_path):
    """「智能搜索」关掉时传 use_dense=False：稠密那一路必须完全不碰。

    判定方式不是看结果——而是让 _dense() 一被调用就炸。如果还去查稠密，
    out 会因为异常被 try/except 兜住照常返回，但 called 会变成 True。
    """
    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))
    _stub_docs(tmp_path)

    called = {"dense": False}

    def _spy_dense():
        called["dense"] = True
        raise RuntimeError("不该被调到")

    monkeypatch.setattr(pipeline, "_dense", _spy_dense)
    monkeypatch.setattr(pipeline, "_load_bm25", lambda: None)
    monkeypatch.setattr(pipeline, "_IDF", {})

    out = pipeline.retrieve_scored("GPUs used for training", top_k=1, use_dense=False)
    assert called["dense"] is False, "use_dense=False 时不应加载稠密模型"
    assert out, "关掉稠密后哈希通道仍应返回结果"


def test_use_dense_true_still_queries_dense(monkeypatch, tmp_path):
    """默认值不能被改坏：不传 use_dense 时稠密那一路照旧要跑。"""
    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))
    _stub_docs(tmp_path)

    called = {"dense": False}

    def _spy_dense():
        called["dense"] = True
        raise RuntimeError("模拟稠密不可用")

    monkeypatch.setattr(pipeline, "_dense", _spy_dense)
    monkeypatch.setattr(pipeline, "_load_bm25", lambda: None)
    monkeypatch.setattr(pipeline, "_IDF", {})

    out = pipeline.retrieve_scored("GPUs used for training", top_k=1)
    assert called["dense"] is True, "默认必须仍然走稠密通道"
    assert out, "稠密失败后哈希通道仍应返回结果"
