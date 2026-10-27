"""1.4.2 / 1.4.3 补齐部分的离线测试：多用户反馈、协同过滤、点击事件、对比式理由、调度器。

全部不联网：网络函数一律 monkeypatch。
"""

import asyncio
from datetime import datetime

import pytest

from app import recommend as rec
from app import scheduler


@pytest.fixture()
def meta(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "FEEDBACK_FILE", tmp_path / "feedback.json")
    monkeypatch.setattr(rec, "EVENTS_FILE", tmp_path / "events.json")
    monkeypatch.setattr(rec, "RECOMMEND_FILE", tmp_path / "recommendations.json")
    return tmp_path


def test_feedback_is_bucketed_per_user(meta):
    rec.save_feedback("2401.00001", "up", "alice")
    rec.save_feedback("2401.00002", "down", "bob")
    assert rec.load_feedback("alice") == {"2401.00001": "up"}
    assert rec.load_feedback("bob") == {"2401.00002": "down"}
    # 单用户看不到别人的投票
    assert rec.load_feedback("carol") == {}
    assert set(rec.load_all_feedback()) == {"alice", "bob"}


def test_feedback_legacy_flat_file_is_migrated(meta):
    meta.joinpath("feedback.json").write_text('{"2401.00009": "up"}', encoding="utf-8")
    assert rec.load_feedback() == {"2401.00009": "up"}
    assert rec.load_feedback(rec.DEFAULT_USER) == {"2401.00009": "up"}


def test_collaborative_scores_promotes_items_from_like_minded_user(meta):
    rec.save_feedback("a", "up", "alice")
    rec.save_feedback("b", "up", "alice")
    rec.save_feedback("a", "up", "bob")
    rec.save_feedback("b", "up", "bob")
    rec.save_feedback("c", "up", "bob")
    # 我和 bob 都赞了 a、b → 他赞的 c 应该被我看到；alice 与我共同点少，权重低
    scores = rec.collaborative_scores("alice")
    assert "c" in scores
    assert 0.0 < scores["c"] <= 1.0


def test_collaborative_scores_cold_start_uses_popularity(meta):
    rec.save_feedback("x", "up", "bob")
    rec.save_feedback("x", "up", "carol")
    rec.save_feedback("y", "up", "carol")
    scores = rec.collaborative_scores("newbie")
    assert scores["x"] == 1.0
    assert scores["y"] == pytest.approx(0.5)


def test_record_event_counts(meta):
    assert rec.record_event("2401.00001", "view") == {"view": 1, "download": 0}
    assert rec.record_event("2401.00001", "view") == {"view": 2, "download": 0}
    assert rec.record_event("2401.00001", "download") == {"view": 2, "download": 1}
    with pytest.raises(ValueError):
        rec.record_event("", "view")
    with pytest.raises(ValueError):
        rec.record_event("2401.00001", "click")


def test_event_scores_normalise_with_download_weight(meta):
    for _ in range(3):
        rec.record_event("k1", "download")  # 3*2=6 分 → 封顶 1.0
    rec.record_event("k2", "view")  # 1 分 → 0.2
    scores = rec.event_scores()
    assert scores["k1"] == 1.0
    assert scores["k2"] == pytest.approx(0.2)


def test_score_candidates_adds_collab_and_event_bonus(monkeypatch):
    monkeypatch.setattr(rec, "embed", lambda text: [1.0, 0.0])
    lib = [("Layer Normalization", [1.0, 0.0])]
    item = {
        "key": "k1",
        "arxiv_id": "2601.00001",
        "title": "A study",
        "abstract": "",
        "cited_by": ["x", "y", "z"],
        "year": 2026,
    }
    out = rec.score_candidates(
        [dict(item)],
        lib,
        feedback={},
        current_year=2026,
        collab={"k1": 1.0},
        events={"k1": {"view": 5, "download": 0}},
    )
    row = out[0]
    assert row["base"] == pytest.approx(0.35 * 1.0 + 0.35 * 1.0 + 0.15)
    assert row["collab"] == pytest.approx(rec.WEIGHT_COLLAB)
    assert row["event"] == pytest.approx(rec.WEIGHT_EVENT)
    assert row["score"] == pytest.approx(row["base"] + rec.WEIGHT_COLLAB + rec.WEIGHT_EVENT)


def test_score_candidates_without_bonus_keeps_old_formula(monkeypatch):
    monkeypatch.setattr(rec, "embed", lambda text: [1.0, 0.0])
    lib = [("Layer Normalization", [1.0, 0.0])]
    item = {
        "key": "k1",
        "arxiv_id": "2601.00001",
        "title": "A study",
        "abstract": "",
        "cited_by": ["x", "y", "z"],
        "year": 2026,
    }
    out = rec.score_candidates([dict(item)], lib, feedback={}, current_year=2026)
    assert out[0]["score"] == pytest.approx(out[0]["base"])


def test_rerank_keeps_bonus_when_voting():
    items = [
        {"key": "a", "base": 0.5, "collab": 0.1, "event": 0.05},
        {"key": "b", "base": 0.45, "collab": 0.0, "event": 0.0},
    ]
    out = rec.rerank_with_feedback(items, {"b": "up"})
    # b: 0.45 + 0.15 + 0 + 0 = 0.60 > a: 0.5 + 0.1 + 0.05 = 0.65 ？不，0.65 更大
    assert [row["key"] for row in out] == ["a", "b"]
    assert out[0]["score"] == pytest.approx(0.65)
    assert out[1]["score"] == pytest.approx(0.60)


def test_rerank_keeps_downloadable_items_first():
    """没 arXiv 号的条目即使分更高也排在能下载的后面（和生成时的口径一致）。"""
    items = [
        {"key": "noarxiv", "arxiv_id": "", "base": 0.90, "collab": 0.0, "event": 0.0},
        {"key": "arxiv", "arxiv_id": "2401.00001", "base": 0.40, "collab": 0.0, "event": 0.0},
    ]
    out = rec.rerank_with_feedback(items, {})
    assert [row["key"] for row in out] == ["arxiv", "noarxiv"]


def test_reason_template_uses_comparison_wording(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(rec, "chat", boom)
    items = [
        {
            "key": "k",
            "arxiv_id": "2601.00001",
            "title": "Some title",
            "cited_by": ["Attention Is All You Need"],
            "topic": 0.31,
            "topic_of": "Layer Normalization",
            "github": "https://github.com/a/b",
        }
    ]
    out = asyncio.run(rec.write_reasons(items, [{"title": "Layer Normalization", "abstract": "x"}]))
    assert "在库内《Layer Normalization》的基础上" in out[0]["reason"]
    assert "开源代码" in out[0]["reason"]


def test_reason_prompt_asks_for_contrast():
    assert "基础上" in rec._REASON_PROMPT
    assert "区别" in rec._REASON_PROMPT


def test_fill_github_adds_code_weight(monkeypatch):
    monkeypatch.setattr(rec, "github_repo", lambda title: "https://github.com/owner/repo")
    items = [{"key": "k", "title": "Some paper title", "github": "", "base": 0.4, "score": 0.4}]
    rec._fill_github(items)
    assert items[0]["github"] == "https://github.com/owner/repo"
    assert items[0]["base"] == pytest.approx(0.4 + rec.WEIGHT_CODE)


def test_fill_github_skips_items_with_repo(monkeypatch):
    called = []

    def fake(title):
        called.append(title)
        return "https://github.com/x/y"

    monkeypatch.setattr(rec, "github_repo", fake)
    items = [{"key": "k", "title": "t", "github": "https://github.com/a/b", "base": 0.4}]
    rec._fill_github(items)
    assert called == []
    assert items[0]["github"] == "https://github.com/a/b"


def test_scheduler_next_run_daily():
    now = datetime(2026, 10, 28, 9, 30)
    nxt = scheduler.next_run_time("daily", now=now)
    assert nxt.hour == scheduler._hour()
    assert nxt > now
    assert (nxt - now).days in (0, 1)


def test_scheduler_next_run_weekly_is_monday():
    now = datetime(2026, 10, 28, 9, 30)  # 周三
    nxt = scheduler.next_run_time("weekly", now=now)
    assert nxt.weekday() == 0
    assert nxt > now


def test_scheduler_status_and_off_mode(monkeypatch):
    monkeypatch.setenv(scheduler.SCHEDULE_ENV, "off")
    info = scheduler.start()
    assert info["mode"] == "off"
    assert info["running"] is False
    status = scheduler.status()
    assert status["mode"] == "off"
    assert status["hour"] == scheduler._hour()
