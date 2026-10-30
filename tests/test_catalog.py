"""全库清单（data/catalog.json）的测试。

为什么要有它：问"一共有多少篇论文"时，模型以前只能数检索片段里出现过几篇。
清单是"全库视角"，这两条测试保证：①清单块内容正确；②全库问题会真的注入清单并出现在证据里。
"""

from __future__ import annotations

import json

import pytest

import app.agent as agent_module
from app.agent import _catalog_block, _is_catalog_question, _rag_context_with_evidence
from app.rag.pipeline import Chunk


@pytest.fixture(autouse=True)
def _no_translate(monkeypatch):
    """单测不真的调 LLM 做查询翻译。"""

    async def _fake(q):
        return q

    monkeypatch.setattr(agent_module, "_search_query", _fake)


def _write_catalog(tmp_path, items):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _use_catalog(monkeypatch, tmp_path, items):
    monkeypatch.setattr(agent_module, "CATALOG_PATH", _write_catalog(tmp_path, items))
    monkeypatch.setattr(agent_module, "_CATALOG_CACHE", {"mtime": None, "items": []})


def test_catalog_question_detection():
    assert _is_catalog_question("这里一共有多少篇论文？")
    assert _is_catalog_question("这些论文都是什么类型的？")
    assert _is_catalog_question("论文库里都有哪些论文")
    # 普通事实题不能被误判成全库题
    assert not _is_catalog_question("训练用了多少块 GPU？")


def test_catalog_block_counts_and_groups(tmp_path, monkeypatch):
    items = [
        {"file": "a.pdf", "title": "A", "year": 2017, "category": "cs.CL", "indexed": True},
        {"file": "b.pdf", "title": "B", "year": 2017, "category": "cs.CL", "indexed": True},
        {"file": "c.pdf", "title": "C", "year": 2023, "category": "cs.CV", "indexed": False},
    ]
    _use_catalog(monkeypatch, tmp_path, items)

    block = _catalog_block()
    assert "共 3 篇" in block
    assert "已建立检索索引 2 篇" in block
    assert "cs.CL 2 篇" in block
    assert "2017年 2 篇" in block


async def test_catalog_question_injects_block_and_evidence(tmp_path, monkeypatch):
    items = [{"file": "a.pdf", "title": "A", "year": 2017, "category": "cs.CL", "indexed": True}]
    _use_catalog(monkeypatch, tmp_path, items)
    # 检索为空也要能回答"多少篇"：清单不依赖向量检索
    monkeypatch.setattr(agent_module, "retrieve_scored", lambda q, top_k=4, source=None, use_dense=True: [])

    context, evidence = await _rag_context_with_evidence("这里一共有多少篇论文？")

    assert "【论文库清单】" in context
    assert "共 1 篇" in context
    assert evidence and evidence[0].channels == ["catalog"]
    assert evidence[0].source == "论文库清单"


async def test_normal_question_gets_no_catalog_block(tmp_path, monkeypatch):
    items = [{"file": "a.pdf", "title": "A", "year": 2017, "category": "cs.CL", "indexed": True}]
    _use_catalog(monkeypatch, tmp_path, items)
    monkeypatch.setattr(
        agent_module,
        "retrieve_scored",
        lambda q, top_k=4, source=None, use_dense=True: [
            (Chunk(text="8 NVIDIA P100 GPUs", source="a.pdf", page=7), 0.05, ["dense"])
        ],
    )

    context, evidence = await _rag_context_with_evidence("训练用了多少块 GPU？")

    assert "【论文库清单】" not in context
    assert all(e.channels != ["catalog"] for e in evidence)
