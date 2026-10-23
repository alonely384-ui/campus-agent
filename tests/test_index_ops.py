"""增量索引测试：上传入索引、删除清索引、章节缓存同步。

全部指向 tmp_path，不碰真实 data/chroma 与 125MB 论文库。
稠密模型不下载：注入一个"零向量"假模型，让 Chroma 能正常写入，
检索相关断言只看 Chroma 记录与 BM25 倒排表。
"""

import json

import pytest

from app import papers as papers_mod
from app.rag import pipeline
from app.rag.pipeline import Chunk, add_chunks, index_paper, remove_source


class _StubEmbedding:
    """最小可用的稠密 embedding 函数：固定 384 维，避免测试时下载 ONNX 模型。"""

    def __call__(self, input):  # noqa: A002 —— Chroma 的协议参数名就是 input
        return [[0.01 * (i + 1)] * 384 for i in range(len(input))]

    def name(self) -> str:
        return "stub-384"

    def is_legacy(self) -> bool:
        # Chroma 1.5 会调用这个方法判断旧配置格式；返回 False 表示用新格式
        return False


@pytest.fixture
def index_tmp(tmp_path, monkeypatch):
    """把索引目录、IDF/BM25 模块缓存、稠密模型都指到临时状态。"""
    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path / "chroma"))
    monkeypatch.setattr(pipeline, "_IDF", {})
    monkeypatch.setattr(pipeline, "_BM25", None)
    stub = _StubEmbedding()
    monkeypatch.setattr(pipeline, "_dense", lambda: stub)
    return tmp_path


def _chunks(source: str = "a.pdf") -> list[Chunk]:
    return [
        Chunk(
            text="We trained our models on one machine with 8 NVIDIA P100 GPUs. " * 3,
            source=source,
            page=7,
            chunk_id=7000,
        ),
        Chunk(
            text="The Adam optimizer uses beta1 = 0.9 and beta2 = 0.999 in our setup. " * 3,
            source=source,
            page=7,
            chunk_id=7001,
        ),
    ]


def test_add_chunks_writes_three_indexes(index_tmp):
    """入库后：Chroma 两份集合有记录，BM25 倒排表里能查到这篇。"""
    import chromadb

    added = add_chunks(_chunks())
    assert added == 2

    client = chromadb.PersistentClient(path=str(index_tmp / "chroma"))
    assert client.get_collection("docs").count() == 2
    assert client.get_collection("docs_dense").count() == 2

    bm = json.loads((index_tmp / "chroma" / "bm25.json").read_text(encoding="utf-8"))
    assert [d["source"] for d in bm["docs"]] == ["a.pdf", "a.pdf"]
    # 词形还原后 "trained" -> "train"，倒排表里应该有这个特征
    assert any(term.startswith("w:train") for term in bm["postings"])

    idf = json.loads((index_tmp / "chroma" / "idf.json").read_text(encoding="utf-8"))
    assert idf["w:gpu"] >= 1.0


def test_add_chunks_appends_without_rebuilding(index_tmp):
    """第二次入库：旧块保留、新块追加，doc_id 连续（BM25 长度数组不能错位）。"""
    add_chunks(_chunks("a.pdf"))
    add_chunks(_chunks("b.pdf"))

    bm = json.loads((index_tmp / "chroma" / "bm25.json").read_text(encoding="utf-8"))
    assert len(bm["docs"]) == 4
    assert len(bm["lens"]) == 4
    # postings 里的 doc_id 必须落在 [0, len(docs)) 内
    for entries in bm["postings"].values():
        for doc_id, _tf in entries:
            assert 0 <= doc_id < len(bm["docs"])


def test_remove_source_clears_chunks_and_bm25(index_tmp):
    """删除一篇：Chroma 里该来源清零，BM25 里也不再出现，另一篇不受影响。"""
    import chromadb

    add_chunks(_chunks("a.pdf"))
    add_chunks(_chunks("b.pdf"))

    removed = remove_source("a.pdf")
    assert removed == 2

    client = chromadb.PersistentClient(path=str(index_tmp / "chroma"))
    docs = client.get_collection("docs")
    assert len(docs.get(where={"source": "a.pdf"}, include=[])["ids"]) == 0
    assert len(docs.get(where={"source": "b.pdf"}, include=[])["ids"]) == 2

    bm = json.loads((index_tmp / "chroma" / "bm25.json").read_text(encoding="utf-8"))
    assert {d["source"] for d in bm["docs"]} == {"b.pdf"}
    for entries in bm["postings"].values():
        for doc_id, _tf in entries:
            assert 0 <= doc_id < len(bm["docs"])


def test_remove_unknown_source_is_noop(index_tmp):
    add_chunks(_chunks("a.pdf"))
    assert remove_source("not-there.pdf") == 0


def test_index_paper_chunk_id_scheme(index_tmp, tmp_path, monkeypatch):
    """上传入库的 chunk_id 必须是 页码*1000+页内序号，和全量建库一致。"""
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")

    page_text = "Attention Is All You Need " * 80  # 足够长，能切成多块
    monkeypatch.setattr(
        pipeline,
        "load_pdf",
        lambda path: [
            Chunk(text=page_text, source="fake.pdf", page=3, chunk_id=0),
            Chunk(text=page_text, source="fake.pdf", page=5, chunk_id=1),
        ],
    )

    indexed = index_paper(str(fake_pdf), "fake.pdf")
    assert indexed > 2

    import chromadb

    client = chromadb.PersistentClient(path=str(index_tmp / "chroma"))
    metas = client.get_collection("docs").get(where={"source": "fake.pdf"}, include=["metadatas"])["metadatas"]
    chunk_ids = sorted(m["chunk_id"] for m in metas)
    assert all(m["page"] in (3, 5) for m in metas)
    assert all(cid // 1000 in (3, 5) for cid in chunk_ids)


def test_delete_structure_removes_entry(tmp_path, monkeypatch):
    """删论文时 structures.json 里那条也要消失，否则三处数据不一致。"""
    structures = tmp_path / "structures.json"
    structures.write_text(
        json.dumps({"a.pdf": {"title": "A"}, "b.pdf": {"title": "B"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(papers_mod, "STRUCTURES_FILE", structures)

    assert papers_mod.delete_structure("a.pdf") is True
    assert papers_mod.delete_structure("a.pdf") is False
    left = json.loads(structures.read_text(encoding="utf-8"))
    assert left == {"b.pdf": {"title": "B"}}


def test_upload_and_delete_endpoints_are_wired(tmp_path, monkeypatch):
    """接口接线测试：上传要调 index_paper，删除要调 remove_source。

    这里把两个函数换成记录器，验证的是"接口有没有把它们接上"，
    真正的索引行为由上面的单元测试保证。
    """
    from fastapi.testclient import TestClient

    from app import main as main_mod

    monkeypatch.setattr(papers_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(papers_mod, "INDEX_FILE", tmp_path / "papers" / "index.json")
    monkeypatch.setattr(papers_mod, "TEXT_DIR", tmp_path / "papers" / "text")
    monkeypatch.setattr(papers_mod, "STRUCTURES_FILE", tmp_path / "papers" / "structures.json")

    calls: dict[str, object] = {}

    def fake_index(path: str, source: str) -> int:
        calls["index"] = (path, source)
        return 7

    def fake_remove(source: str) -> int:
        calls["remove"] = source
        return 7

    monkeypatch.setattr(main_mod, "index_paper", fake_index)
    monkeypatch.setattr(main_mod, "remove_source", fake_remove)

    client = TestClient(main_mod.app)
    resp = client.post(
        "/papers/upload",
        files={"file": ("x.pdf", b"%PDF-1.4 fake body", "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["indexed"] == 7
    assert calls["index"][1] == "x.pdf"

    paper_id = body["paper"]["paper_id"]
    deleted = client.delete(f"/papers/{paper_id}")
    assert deleted.status_code == 200
    assert deleted.json()["removed_chunks"] == 7
    assert calls["remove"] == "x.pdf"
