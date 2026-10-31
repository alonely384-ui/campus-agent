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
    # 兜底模板也不能把内部相似度（0.31）抄给用户看
    assert "0.31" not in out[0]["reason"]
    assert "相似度" not in out[0]["reason"]


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


def test_topic_profile_blends_best_and_top3():
    # 候选 [1,0]：与 A 完全一致、与 B 余弦 0.8、与 C 余弦 0
    lib = [("A", [1.0, 0.0]), ("B", [0.8, 0.6]), ("C", [0.0, 1.0])]
    topic, best, top3 = rec.topic_profile([1.0, 0.0], lib)
    assert best == "A"
    # 0.55*max + 0.45*均值(top3)
    assert topic == pytest.approx(round(0.55 * 1.0 + 0.45 * (1.0 + 0.8 + 0.0) / 3, 4))
    assert [row["title"] for row in top3] == ["A", "B", "C"]


def test_recent_candidates_gate_on_topic_only(monkeypatch):
    monkeypatch.setattr(
        rec, "embed", lambda text: [1.0, 0.0] if "fresh" in text.lower() else [0.15, 0.99]
    )
    lib = [("Layer Normalization", [1.0, 0.0])]
    keep = {
        "key": "r1",
        "arxiv_id": "2610.00001",
        "title": "A fresh paper on normalization",
        "abstract": "",
        "cited_by": [],
        "source": "recent",
        "year": 2026,
    }
    drop = dict(keep, key="r2", arxiv_id="2610.00002", title="Biology matters", source="recent")
    out = rec.score_candidates([dict(keep), dict(drop)], lib, current_year=2026)
    assert [item["key"] for item in out] == ["r1"]
    assert out[0]["source"] == "recent"
    assert out[0]["score"] == pytest.approx(
        rec.WEIGHT_TOPIC * out[0]["topic"] + rec.WEIGHT_FRESH * 1.0, abs=1e-6
    )


def test_collect_recent_skips_library_duplicates(monkeypatch):
    monkeypatch.setattr(
        rec,
        "library_rows",
        lambda: [{"paper_id": "p1", "filename": "a.pdf", "arxiv_id": "2401.00001",
                  "title": "Attention Is All You Need", "abstract": ""}],
    )
    monkeypatch.setattr(
        rec,
        "recent_papers",
        lambda *args, **kwargs: [
            {"arxiv_id": "2401.00001", "title": "Attention Is All You Need", "posted": "", "url": ""},
            {"arxiv_id": "2610.00003", "title": "Brand new thing", "posted": "", "url": ""},
        ],
    )
    monkeypatch.setattr(
        rec,
        "paper_meta",
        lambda arxiv_id: {"title": "Brand new thing", "abstract": "摘要", "year": 2026},
    )
    # lib_vecs=None：跳过标题粗排，保证两条都进池子（重复的那条要在这里被剔掉）
    out, errors = rec.collect_recent(max_items=4, lib_vecs=None)
    assert errors == []
    assert [item["arxiv_id"] for item in out] == ["2610.00003"]
    assert out[0]["source"] == "recent"
    assert out[0]["year"] == 2026


# ------------------------------------------------------------------ 理由里的内部度量


@pytest.mark.parametrize(
    "raw, must_not_contain",
    [
        ("与库内LoRA方向相近但相似度低于0.30，本文重新审视LoRA初始化，属参数高效微调方向。", "相似度"),
        ("与库内《Attention Is All You Need》方向相近但相似度低于0.30，本文提出Transformer++改进。", "0.30"),
        ("在库内《X》的基础上（主题相似度 0.47）又引用了库内 3 篇论文，区别是换了问题设定", "0.47"),
        ("与库内《Y》方向不同（相似度 0.21），可作为该方向的旁证参考", "相似度"),
    ],
)
def test_sanitize_reason_strips_internal_metric(raw, must_not_contain):
    """模型会把「相似度低于0.30」这种内部阈值抄进给用户看的理由里，必须清掉。"""
    out = rec.sanitize_reason(raw)
    assert out, "清洗后不该整句丢掉"
    assert must_not_contain not in out
    assert "相关度" not in out
    assert "相近但" not in out, "删数字后留下的半截转折要收拾掉"


def test_sanitize_reason_keeps_clean_text_untouched():
    raw = "在《Scaling Laws for Neural Language Models》的基础上，拓展到强化学习训练场景。"
    assert rec.sanitize_reason(raw) == raw


def test_sanitize_reason_drops_text_that_is_only_a_number():
    """清洗后掉太多字说明整句是围着数字写的，返回空串让调用方走兜底模板。"""
    assert rec.sanitize_reason("相似度 0.47") == ""
