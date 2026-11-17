"""推荐刷新的权限边界（2026-11-18，审计 P1-2 的回归测试）。

审计实测的问题：`POST /recommendations/refresh` 在 PUBLIC_PATHS 里，匿名请求
一次就真跑了 7.7 秒的 Semantic Scholar + arXiv 外部请求，任何访问者都能反复
触发——等于把我们的外部额度开关交给公网。

现在要求：
1. 匿名读缓存（`GET /recommendations`）必须照常 200；
2. 匿名强制刷新（`POST /recommendations/refresh`）必须 401，且**一次外部请求都不能发**；
3. 管理员登录后强制刷新可以正常跑；
4. "刷新"不复用普通页面访问——普通访问只读缓存，不触发外部 API。

第 2 条要证明的是"根本没调用"，而不只是"返回了 401"，所以这里给
`main.build_recommendations` 装一个会记账的桩：它被调用过就说明拦截位置错了
（拦在函数里面而没拦在路由上）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import recommend as rec
from app import security

TOKEN = "refresh-test-token"
SECRET = "refresh-test-secret"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把推荐缓存与反馈文件都指到 tmp，避免碰真实 data/meta。"""
    monkeypatch.setattr(rec, "RECOMMEND_FILE", tmp_path / "recommendations.json")
    monkeypatch.setattr(rec, "FEEDBACK_FILE", tmp_path / "feedback.json")
    monkeypatch.setattr(main_mod, "RECOMMEND_FILE", tmp_path / "recommendations.json")
    return tmp_path


@pytest.fixture()
def anon_client(sandbox, monkeypatch):
    """没登录的客户端：管理员凭证配好了，但请求不带 Cookie。"""
    monkeypatch.setattr(security, "_admin_token", lambda: TOKEN)
    monkeypatch.setattr(security, "_cookie_secret", lambda: SECRET)
    security.login_guard.reset()
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture()
def admin_client(anon_client):
    resp = anon_client.post("/admin/login", json={"token": TOKEN})
    assert resp.status_code == 200, resp.text
    return anon_client


CACHED = {
    "updated_at": "2026-11-18T00:00:00+08:00",
    "items": [{"key": "arxiv:1706.03762", "title": "Attention Is All You Need", "year": 2017}],
    "errors": [],
    "library_size": 38,
}


# ---------------------------------------------------------------- 1. 匿名读缓存


def test_anonymous_can_read_cached_recommendations(anon_client, monkeypatch):
    monkeypatch.setattr(main_mod, "cached_recommendations", lambda: dict(CACHED))

    resp = anon_client.get("/recommendations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["title"] == "Attention Is All You Need"
    assert body["library_size"] == 38


def test_anonymous_read_does_not_touch_external_apis(anon_client, monkeypatch):
    """读缓存这条路必须完全不触发外部抓取（它是每次打开页面都会走的路）。"""
    called: list[str] = []

    def boom(*args, **kwargs):
        called.append("build")
        raise AssertionError("普通访问不该触发外部抓取")

    monkeypatch.setattr(main_mod, "build_recommendations", boom)
    monkeypatch.setattr(main_mod, "cached_recommendations", lambda: dict(CACHED))

    assert anon_client.get("/recommendations").status_code == 200
    assert called == []


# ---------------------------------------------------------------- 2. 匿名强制刷新


def test_anonymous_refresh_returns_401(anon_client):
    resp = anon_client.post("/recommendations/refresh")
    assert resp.status_code == 401, resp.text
    assert "管理员" in resp.json()["detail"]


def test_anonymous_refresh_never_reaches_the_scraper(anon_client, monkeypatch):
    """关键断言：401 必须是"在路由之前就拦掉"，而不是跑完外部请求再拒。"""
    called: list[tuple] = []

    async def spy_build(*args, **kwargs):
        called.append((args, kwargs))
        return dict(CACHED)

    monkeypatch.setattr(main_mod, "build_recommendations", spy_build)

    assert anon_client.post("/recommendations/refresh").status_code == 401
    assert called == [], "匿名请求居然走到了抓取函数里，说明拦截位置不对"


def test_refresh_is_registered_as_admin_path():
    """路径归属也要钉住：它同时在 README 式的对账表里，不能只靠中间件兜底。"""
    assert "/recommendations/refresh" in security.ADMIN_PATHS
    assert "/recommendations/refresh" not in security.PUBLIC_PATHS


# ---------------------------------------------------------------- 3. 管理员刷新


def test_admin_refresh_runs(admin_client, monkeypatch):
    async def fake_build(*args, **kwargs):
        return dict(CACHED, items=[], refreshed=True)

    monkeypatch.setattr(main_mod, "build_recommendations", fake_build)

    resp = admin_client.post("/recommendations/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("refreshed") is True


def test_admin_refresh_still_falls_back_to_cache_on_network_error(admin_client, monkeypatch):
    """外部抓取失败时接口仍返回过期缓存（offline=true），不是 500。"""
    from app.online import OnlineError

    async def failing_build(*args, **kwargs):
        raise OnlineError("arXiv 503")

    monkeypatch.setattr(main_mod, "build_recommendations", failing_build)
    monkeypatch.setattr(main_mod, "cached_recommendations", lambda: dict(CACHED))

    resp = admin_client.post("/recommendations/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["offline"] is True
    assert "503" in body["detail"]


# ---------------------------------------------------------------- 4. 缓存有效时不抓外网

def test_valid_cache_short_circuits_external_fetch(sandbox, monkeypatch):
    """`cached_recommendations()` 有可用缓存时必须直接返回，不打外部接口。"""
    monkeypatch.setattr(rec, "read_cache_any_age", lambda key: dict(CACHED))

    payload = rec.cached_recommendations()
    assert payload["items"][0]["key"] == "arxiv:1706.03762"
    assert payload["offline"] is False
    # 缓存里没有 items 时才该轮到磁盘兜底；这里已经提前返回，文件都没被读
    assert not (sandbox / "recommendations.json").exists()


def test_empty_cache_still_returns_a_shell_not_an_error(sandbox, monkeypatch):
    """没有缓存时返回空壳（前端显示"还没刷新过"），而不是抛错。"""
    monkeypatch.setattr(rec, "read_cache_any_age", lambda key: None)

    payload = rec.cached_recommendations()
    assert payload["items"] == []
    assert payload["offline"] is True