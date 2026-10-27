"""app/recommend.py 的离线测试：打分、反馈重排、理由解析与接口壳。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import online as on
from app import recommend as rec
from app.main import app

client = TestClient(app)


@pytest.fixture()
def meta(tmp_path, monkeypatch):
    """反馈文件与推荐缓存都指到 tmp，别动真实数据。"""
    monkeypatch.setattr(rec, "FEEDBACK_FILE", tmp_path / "feedback.json")
    monkeypatch.setattr(rec, "RECOMMEND_FILE", tmp_path / "recommendations.json")
    monkeypatch.setattr(on, "CACHE_DIR", tmp_path / "online")
    # 路由里是 `from app.recommend import RECOMMEND_FILE`，绑定的是导入时的 Path 对象，
    # 改 rec.RECOMMEND_FILE 影响不到它，得单独改 app.main 里的名字。
    monkeypatch.setattr("app.main.RECOMMEND_FILE", tmp_path / "recommendations.json")
    return tmp_path


def test_score_candidates_keeps_relevant_and_drops_noise(monkeypatch):
    monkeypatch.setattr(
        rec, "embed", lambda text: [1.0, 0.0] if "norm" in text.lower() else [0.0, 1.0]
    )
    lib = [("Layer Normalization", [1.0, 0.0])]
    candidates = [
        {
            "key": "a",
            "arxiv_id": "2601.00001",
            "title": "Layer norm variants",
            "abstract": "",
            "cited_by": [],
            "year": 2026,
        },
        {
            "key": "b",
            "arxiv_id": "",
            "title": "Fuel cell membranes",
            "abstract": "",
            "cited_by": ["x"],
            "year": 2026,
        },
    ]
    out = rec.score_candidates(candidates, lib, feedback={}, current_year=2026)
    assert [item["key"] for item in out] == ["a"]
    assert out[0]["topic"] == 1.0
    assert out[0]["topic_of"] == "Layer Normalization"


def test_score_candidates_applies_weights_and_votes(monkeypatch):
    monkeypatch.setattr(rec, "embed", lambda text: [1.0, 0.0])
    lib = [("Layer Normalization", [1.0, 0.0])]
    item = {
        "key": "k1",
        "arxiv_id": "2601.00001",
        "title": "A study",
        "abstract": "code at https://github.com/openai/gpt-3",
        "cited_by": ["x", "y", "z"],
        "year": 2026,
    }
    out = rec.score_candidates([dict(item)], lib, feedback={"k1": "down"}, current_year=2026)
    assert len(out) == 1
    row = out[0]
    assert row["overlap"] == 1.0
    assert row["topic"] == 1.0
    assert row["github"] == "https://github.com/openai/gpt-3"
    # 0.35*overlap + 0.35*topic + 0.15*code + 0.15*fresh（当年 → fresh=1）
    assert row["base"] == 1.0
    assert row["score"] == pytest.approx(1.0 + rec.VOTE_DOWN)


def test_rerank_with_feedback_promotes_upvoted():
    items = [{"key": "a", "base": 0.5}, {"key": "b", "base": 0.45}]
    out = rec.rerank_with_feedback(items, {"a": "down", "b": "up"})
    assert [item["key"] for item in out] == ["b", "a"]
    assert out[0]["score"] == pytest.approx(0.45 + rec.VOTE_UP)
    assert out[1]["score"] == pytest.approx(0.5 + rec.VOTE_DOWN)


def test_feedback_roundtrip(meta):
    assert rec.load_feedback() == {}
    assert rec.save_feedback("2401.00001", "up") == {"2401.00001": "up"}
    assert rec.load_feedback() == {"2401.00001": "up"}
    assert rec.save_feedback("2401.00001", "") == {}
    with pytest.raises(ValueError):
        rec.save_feedback("2401.00001", "maybe")
    with pytest.raises(ValueError):
        rec.save_feedback("  ", "up")


def test_parse_reasons_tolerates_noise():
    answer = '好的，结果如下：\n```json\n[{"arxiv_id": "2601.00001", "reason": "在 LayerNorm 基础上改进"},\n {"arxiv_id": "", "reason": "没有编号"}]\n```'
    assert rec._parse_reasons(answer) == {"2601.00001": "在 LayerNorm 基础上改进"}
    assert rec._parse_reasons("没有 JSON") == {}
    assert rec._parse_reasons('[{"arxiv_id": "x", "reason": "y"') == {}


def test_write_reasons_falls_back_to_template(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(rec, "chat", boom)
    items = [
        {
            "key": "k",
            "arxiv_id": "",
            "title": "Some title",
            "cited_by": ["Layer Normalization"],
            "topic": 0.31,
            "github": "",
        }
    ]
    out = asyncio.run(rec.write_reasons(items, [{"title": "Layer Normalization"}]))
    assert "引用了库内 1 篇论文" in out[0]["reason"]
    assert "0.31" in out[0]["reason"]


def test_title_key_normalises():
    assert rec._title_key("Attention Is All You Need!") == "attentionisallyouneed"


def test_recommendations_endpoint_without_cache_is_offline(meta):
    resp = client.get("/recommendations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["offline"] is True
    assert body["items"] == []


def test_feedback_endpoint_requires_known_vote(meta):
    resp = client.post("/recommendations/2401.00001/feedback", json={"vote": "maybe"})
    assert resp.status_code == 400
    resp = client.post("/recommendations/2401.00001/feedback", json={"vote": "up"})
    assert resp.status_code == 200
    assert resp.json()["feedback"] == {"2401.00001": "up"}
