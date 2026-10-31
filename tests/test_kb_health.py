"""知识库健康检查与幽灵索引清理的测试（设置中心 D 模块）。

这里不碰真实 Chroma：用 monkeypatch 把三路读取换成假数据，
专注于「识别准不准、护栏有没有生效、失败会不会静默」这三件事。
"""

from __future__ import annotations

import pytest

from app import kb_health

# ---------------------------------------------------------------- 假数据


def _patch_sources(monkeypatch, docs, dense, bm25, disk):
    """把 health_check 用到的四个读取器全部换成假实现。"""
    monkeypatch.setattr(kb_health, "_chroma_sources", lambda name: dict(docs if name == "docs" else dense))
    monkeypatch.setattr(kb_health, "_bm25_sources", lambda: dict(bm25))
    monkeypatch.setattr(kb_health, "_disk_sources", lambda: set(disk))
    monkeypatch.setattr(kb_health.papers, "list_papers", lambda: [{"file": n} for n in disk])


HEALTHY = (
    {"a.pdf": 10, "b.pdf": 20},
    {"a.pdf": 10, "b.pdf": 20},
    {"a.pdf": 10, "b.pdf": 20},
    {"a.pdf", "b.pdf"},
)


# ---------------------------------------------------------------- 健康检查


def test_healthy_library_is_consistent(monkeypatch):
    _patch_sources(monkeypatch, *HEALTHY)
    report = kb_health.health_check()
    assert report.consistent is True
    assert report.ghost_sources == []
    assert report.notes == []


def test_ghost_source_is_detected_across_all_three_paths(monkeypatch):
    """只在索引里有、磁盘上没有的 source 才算幽灵。"""
    _patch_sources(
        monkeypatch,
        {"a.pdf": 10, "ghost.pdf": 184},
        {"a.pdf": 10, "ghost.pdf": 184},
        {"a.pdf": 10, "ghost.pdf": 180},
        {"a.pdf"},
    )
    report = kb_health.health_check()

    assert report.consistent is False
    assert [g.source for g in report.ghost_sources] == ["ghost.pdf"]
    ghost = report.ghost_sources[0]
    assert bool(ghost.is_ghost) is True
    assert ghost.docs_chunks == 184
    assert ghost.dense_chunks == 184
    assert ghost.bm25_chunks == 180
    assert report.ghost_chunks_docs == 184
    assert report.ghost_chunks_bm25 == 180


def test_source_on_disk_is_never_ghost(monkeypatch):
    """磁盘上还有这个 PDF，索引里块再多也不能算幽灵。"""
    _patch_sources(monkeypatch, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf"})
    report = kb_health.health_check()
    assert not any(g.is_ghost for g in report.ghost_sources)


def test_partial_ghost_only_in_one_path_is_still_ghost(monkeypatch):
    """三路各自独立删除，只有一路残留也要能抓出来。"""
    _patch_sources(monkeypatch, {"a.pdf": 10, "ghost.pdf": 5}, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf"})
    report = kb_health.health_check()
    assert [g.source for g in report.ghost_sources] == ["ghost.pdf"]
    assert report.ghost_sources[0].docs_chunks == 5
    assert report.ghost_sources[0].dense_chunks == 0


def test_missing_from_index_is_reported(monkeypatch):
    """磁盘有但索引没有，是另一种问题，要单独报，不能混进幽灵。"""
    _patch_sources(monkeypatch, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf", "new.pdf"})
    report = kb_health.health_check()
    assert report.missing_from_index == ["new.pdf"]
    assert report.ghost_sources == []
    assert any("索引里没有" in n for n in report.notes)


def test_three_path_total_mismatch_is_noted(monkeypatch):
    _patch_sources(monkeypatch, {"a.pdf": 10}, {"a.pdf": 10}, {"a.pdf": 9}, {"a.pdf"})
    report = kb_health.health_check()
    assert report.consistent is False
    assert any("BM25" in n and "不一致" in n for n in report.notes)


# ---------------------------------------------------------------- 清理护栏


def test_cleanup_refuses_to_delete_valid_paper(monkeypatch):
    """把有效论文传进清理接口，必须 skipped，绝不能调用删除。"""
    _patch_sources(
        monkeypatch,
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf"},
    )

    called: list[str] = []
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "remove_source", lambda src: called.append(src) or 5)

    result = kb_health.cleanup_ghosts(["a.pdf"])
    assert result.skipped == ["a.pdf"]
    assert result.cleaned == []
    assert called == [], "有效论文被删了！"


def test_cleanup_removes_only_ghosts(monkeypatch):
    _patch_sources(
        monkeypatch,
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf": 10, "ghost.pdf": 5},
        {"a.pdf"},
    )

    called: list[str] = []
    from app.rag import pipeline

    monkeypatch.setattr(pipeline, "remove_source", lambda src: called.append(src) or 5)

    result = kb_health.cleanup_ghosts()
    assert result.cleaned == ["ghost.pdf"]
    assert result.removed_chunks == 5
    assert called == ["ghost.pdf"]


def test_cleanup_records_error_and_continues(monkeypatch):
    """一个 source 删失败，不能伪装成功，也不能中断其他 source 的清理。"""
    _patch_sources(
        monkeypatch,
        {"ghost1.pdf": 3, "ghost2.pdf": 4},
        {"ghost1.pdf": 3, "ghost2.pdf": 4},
        {"ghost1.pdf": 3, "ghost2.pdf": 4},
        set(),
    )

    from app.rag import pipeline

    def fake_remove(src: str) -> int:
        if src == "ghost1.pdf":
            raise RuntimeError("删除 docs_dense 失败")
        return 4

    monkeypatch.setattr(pipeline, "remove_source", fake_remove)

    result = kb_health.cleanup_ghosts()
    assert result.cleaned == ["ghost2.pdf"], "失败的 source 不能算清理成功"
    assert len(result.errors) == 1
    assert "ghost1.pdf" in result.errors[0]
    assert "删除 docs_dense 失败" in result.errors[0]


def test_cleanup_aftermath_is_rechecked(monkeypatch):
    """清理后三路必须真的干净，verify_consistency 要能证明这一点。"""
    state = {
        "docs": {"a.pdf": 10, "ghost.pdf": 5},
        "dense": {"a.pdf": 10, "ghost.pdf": 5},
        "bm25": {"a.pdf": 10, "ghost.pdf": 5},
    }
    monkeypatch.setattr(kb_health, "_chroma_sources", lambda name: dict(state["docs" if name == "docs" else "dense"]))
    monkeypatch.setattr(kb_health, "_bm25_sources", lambda: dict(state["bm25"]))
    monkeypatch.setattr(kb_health, "_disk_sources", lambda: {"a.pdf"})
    monkeypatch.setattr(kb_health.papers, "list_papers", lambda: [{"file": "a.pdf"}])

    before = kb_health.health_check()
    assert before.consistent is False

    from app.rag import pipeline

    def fake_remove(src: str) -> int:
        n = state["docs"].pop(src, 0)
        state["dense"].pop(src, None)
        state["bm25"].pop(src, None)
        return n

    monkeypatch.setattr(pipeline, "remove_source", fake_remove)

    result = kb_health.cleanup_ghosts(["ghost.pdf"])
    assert result.cleaned == ["ghost.pdf"]
    assert result.errors == []

    after = kb_health.verify_consistency()
    assert after["consistent"] is True, f"清理后仍不一致: {after}"
    assert after["ghost_sources"] == []
    assert after["docs_total"] == after["dense_total"] == after["bm25_total"]


def test_cleanup_with_empty_ghost_list_is_noop(monkeypatch):
    _patch_sources(monkeypatch, *HEALTHY)
    from app.rag import pipeline

    def blow_up(src):
        raise AssertionError("没有幽灵时不应调用删除")

    monkeypatch.setattr(pipeline, "remove_source", blow_up)
    result = kb_health.cleanup_ghosts()
    assert result.cleaned == [] and result.errors == []


def test_dense_delete_failure_is_not_silent(monkeypatch):
    """remove_source 里 docs_dense 删除失败必须抛出来，不能 pass 掉。"""
    import chromadb

    from app.rag import pipeline

    class FakeCol:
        """docs 允许删，docs_dense 故意失败 —— 精确模拟「只清掉一路」。"""

        def __init__(self, name: str) -> None:
            self.name = name

        def get(self, **kw):
            return {"ids": ["x1", "x2"]}

        def delete(self, **kw):
            if self.name == "docs_dense":
                raise RuntimeError("dense delete failed")
            return None

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_or_create_collection(self, name, embedding_function=None):
            return FakeCol(name)

    monkeypatch.setattr(chromadb, "PersistentClient", FakeClient)
    monkeypatch.setattr(pipeline, "_load_bm25", lambda: {"docs": [], "postings": {}, "lens": []})

    # 只断言「异常被抛出且信息里点名 docs_dense」，中文部分不做正则匹配（控制台编码会干扰）
    with pytest.raises(RuntimeError) as info:
        pipeline.remove_source("ghost.pdf")
    assert "docs_dense" in str(info.value)


def test_bm25_save_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    """BM25 索引必须原子落盘：崩在写一半时，磁盘上仍是完整的旧索引。

    直接 open(..., "w") 会先把文件截断成 0 字节，写完之前进程挂掉
    就只剩半截 JSON，下次 _load_bm25 解析失败返回 None，BM25 整路静默消失。
    """
    import json

    from app.rag import pipeline

    target = tmp_path / "bm25.json"
    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))

    old = {"docs": ["keep-1"], "postings": {"a": [[0, 1]]}, "lens": [3]}
    target.write_text(json.dumps(old), encoding="utf-8")

    # 让 json.dump 中途炸掉，模拟磁盘满 / 进程被杀
    import json as json_mod

    real_dump = json_mod.dump

    def boom(obj, fp, **kw):
        real_dump(obj, fp, **kw)
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.json, "dump", boom)
    with pytest.raises(OSError):
        pipeline._save_bm25({"docs": [], "postings": {}, "lens": []})
    monkeypatch.setattr(pipeline.json, "dump", real_dump)

    # 旧文件必须原封不动，且不能留下半截临时文件
    assert json.loads(target.read_text(encoding="utf-8")) == old
    assert not target.with_suffix(".json.tmp").exists()


def test_bm25_save_success_replaces_atomically(tmp_path, monkeypatch):
    """正常保存后磁盘上是新索引，且不留临时文件。"""
    import json

    from app.rag import pipeline

    target = tmp_path / "bm25.json"
    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))
    target.write_text('{"docs": ["old"]}', encoding="utf-8")

    new = {"docs": ["new"], "postings": {}, "lens": [1]}
    pipeline._save_bm25(new)

    assert json.loads(target.read_text(encoding="utf-8")) == new
    assert not (tmp_path / "bm25.json.tmp").exists()


def test_remove_source_deletes_dense_last(monkeypatch, tmp_path):
    """docs_dense 删除放最后：失败时已改动的面最小，且异常照常抛出。

    注意 chroma_dir 必须重定向到 tmp_path：这条路径会走 _save_bm25，
    不重定向就会把真实的 data/chroma/bm25.json 覆盖成测试用的假索引
    （实测把 26596 块的真实 BM25 索引写成了 56 字节的空索引）。
    """
    import chromadb

    from app.rag import pipeline

    monkeypatch.setattr(pipeline.settings, "chroma_dir", str(tmp_path))
    order: list[str] = []

    class FakeCol:
        def __init__(self, name: str) -> None:
            self.name = name

        def get(self, **kw):
            return {"ids": ["x1"]}

        def delete(self, **kw):
            order.append(self.name)
            if self.name == "docs_dense":
                raise RuntimeError("dense delete failed")
            return None

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_or_create_collection(self, name, embedding_function=None):
            return FakeCol(name)

    monkeypatch.setattr(chromadb, "PersistentClient", FakeClient)
    monkeypatch.setattr(
        pipeline,
        "_load_bm25",
        lambda: {"docs": [{"id": "x1", "source": "ghost.pdf"}], "postings": {}, "lens": [2], "avg_len": 2.0},
    )

    with pytest.raises(RuntimeError):
        pipeline.remove_source("ghost.pdf")

    assert order == ["docs", "docs_dense"], f"docs_dense 必须排在最后，实际顺序 {order}"
    # 只写了 tmp_path，真实索引没被动过
    assert (tmp_path / "bm25.json").exists()