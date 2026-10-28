"""1.4.2 / 1.4.3 的离线测试：单用户反馈、主题引用打分、对比式理由、调度器。

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


def test_score_candidates_uses_four_signals_only(monkeypatch):
    """主题引用简单显示：分数只由四个主信号算出，没有协同/事件附加分。"""
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
    row = out[0]
    assert row["base"] == pytest.approx(0.40 * 1.0 + 0.35 * 1.0 + 0.0 + 0.10)
    assert "collab" not in row
    assert "event" not in row
    assert "events" not in row
    assert row["score"] == pytest.approx(row["base"])


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


def test_rerank_adds_vote_to_base():
    items = [
        {"key": "a", "base": 0.50},
        {"key": "b", "base": 0.45},
    ]
    out = rec.rerank_with_feedback(items, {"b": "up"})
    # b: 0.45 + 0.15 = 0.60 > a: 0.50
    assert [row["key"] for row in out] == ["b", "a"]
    assert out[0]["score"] == pytest.approx(0.60)
    assert out[1]["score"] == pytest.approx(0.50)


def test_rerank_keeps_downloadable_items_first():
    """没 arXiv 号的条目即使分更高也排在能下载的后面（和生成时的口径一致）。"""
    items = [
        {"key": "noarxiv", "arxiv_id": "", "base": 0.90},
        {"key": "arxiv", "arxiv_id": "2401.00001", "base": 0.40},
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


def test_select_mixed_reserves_quota_for_recent():
    """纯按分数排时新上架永远挤不进榜，所以选榜时要给它们保底名额。"""
    cited = [
        {"key": f"c{i}", "source": "cited", "score": 0.9 - i * 0.01, "arxiv_id": f"2401.0000{i}"}
        for i in range(8)
    ]
    recent = [
        {"key": "r1", "source": "recent", "score": 0.30, "arxiv_id": "2402.00001"},
        {"key": "r2", "source": "recent", "score": 0.28, "arxiv_id": "2402.00002"},
    ]

    picked = rec._select_mixed(cited + recent, limit=5, quota=2)

    assert len(picked) == 5
    assert sum(1 for item in picked if item["source"] == "recent") == 2
    # 选完仍按分数排，引用扩展在前
    assert picked[0]["key"] == "c0"


def test_select_mixed_quota_never_exceeds_limit():
    recent = [{"key": f"r{i}", "source": "recent", "score": 0.3, "arxiv_id": ""} for i in range(5)]

    picked = rec._select_mixed(recent, limit=2, quota=3)

    assert len(picked) == 2


def test_cited_noise_below_topic_floor_is_dropped(monkeypatch):
    """只靠"被同一篇引用过"、主题完全不搭的候选（实测里的机器人论文）要挡掉。"""
    monkeypatch.setattr(
        rec, "embed", lambda text: [1.0, 0.0] if "layer" in text.lower() else [0.0, 1.0]
    )
    lib = [("Layer Normalization", [1.0, 0.0])]
    noise = {
        "arxiv_id": "2401.99999",
        "title": "Robot arm control",
        "abstract": "",
        "cited_by": ["a", "b", "c"],
        "source": "cited",
    }
    good = {
        "arxiv_id": "2401.88888",
        "title": "Layer normalization variants",
        "abstract": "",
        "cited_by": ["a"],
        "source": "cited",
    }

    ranked = rec.score_candidates([noise, good], lib, {})

    assert [item["arxiv_id"] for item in ranked] == ["2401.88888"]


def test_looks_like_paper_rejects_non_pdf(tmp_path):
    from app.main import _looks_like_paper

    bad = tmp_path / "downloaded.pdf"
    bad.write_text("<html><body>arXiv error</body></html>", encoding="utf-8")

    assert _looks_like_paper(bad, "Attention Is All You Need") is False


def test_looks_like_paper_accepts_real_pdf(tmp_path):
    import fitz

    from app.main import _looks_like_paper

    doc = fitz.open()
    for _ in range(5):
        doc.new_page()
    doc[0].insert_text((60, 90), "Attention Is All You Need")
    path = tmp_path / "ok.pdf"
    doc.save(str(path))
    doc.close()

    assert _looks_like_paper(path, "Attention Is All You Need") is True
    # 标题完全对不上的 PDF（比如别人的附件）也要拒
    assert _looks_like_paper(path, "Layer Normalization") is False
