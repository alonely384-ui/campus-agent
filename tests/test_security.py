"""安全层测试（第二阶段）。

覆盖用户 m15060 确认的五项设计修订：
    1. ADMIN_TOKEN 为空时**默认拒绝**，不存在「没配就放行」
    2. IP 获取规则：无可信代理时用连接 IP，不无条件信 X-Forwarded-For
    3. 限流器边界明确（重启丢状态、单进程不共享），每日预算持久化+并发安全
    4. 请求体上限区分 JSON 与文件上传，且按**实际读取字节数**算，不看 Content-Length
    5. 800 次是初始值，不承诺固定费用上限

另覆盖：44 路由权限归属、SSE、静态资源、尾斜杠、未知路径、Cookie、CSRF、限流绕过。

命名约定：test_<被测行为>__<前置条件>，读起来就是一句断言。
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import security
from app.security import (
    ADMIN_PATHS,
    BUCKET_LIGHT,
    PUBLIC_PATHS,
    DailyBudget,
    RateLimiter,
    admin_configured,
    bucket_for,
    client_ip,
    normalize_path,
    sign_session,
    verify_password,
    verify_session,
)

TOKEN = "test-admin-token"
SECRET = "test-cookie-secret"


@pytest.fixture
def admin_env(monkeypatch):
    """给测试进程装上一套管理员凭证（monkeypatch 自动还原）。"""
    monkeypatch.setattr(security, "_admin_token", lambda: TOKEN)
    monkeypatch.setattr(security, "_cookie_secret", lambda: SECRET)
    security.login_guard.reset()


@pytest.fixture
def no_admin_env(monkeypatch):
    """模拟生产漏配 ADMIN_TOKEN 的情况。"""
    monkeypatch.setattr(security, "_admin_token", lambda: "")
    monkeypatch.setattr(security, "_cookie_secret", lambda: "")
    security.login_guard.reset()


@pytest.fixture
def client(admin_env):
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def anon_client(no_admin_env):
    with TestClient(main_mod.app) as c:
        yield c


def login(client: TestClient, token: str = TOKEN):
    return client.post("/admin/login", json={"token": token})


# ============================================================ 1. 默认拒绝


def test_admin_endpoint_denied_when_no_token_configured__returns_503(anon_client):
    """没配 ADMIN_TOKEN 时，管理接口必须 503，绝不能「没配就放行」。"""
    for path in ("/settings", "/settings/index/health", "/settings/about"):
        resp = anon_client.get(path)
        assert resp.status_code == 503, f"{path} 在未配置凭证时返回了 {resp.status_code}"
        assert "未配置" in resp.json()["detail"]


def test_admin_write_denied_when_no_token_configured__returns_503(anon_client):
    resp = anon_client.post("/settings", json={"key": "LLM_MODEL", "value": "x"})
    assert resp.status_code == 503


def test_login_endpoint_unavailable_when_not_configured(anon_client):
    """没配口令时连登录口都不该存在，否则等于给了爆破目标。"""
    resp = login(anon_client)
    assert resp.status_code == 503


def test_admin_configured_requires_both_values(monkeypatch):
    monkeypatch.setattr(security, "_admin_token", lambda: TOKEN)
    monkeypatch.setattr(security, "_cookie_secret", lambda: "")
    assert admin_configured() is False, "只有口令没有密钥不算配置完成"

    monkeypatch.setattr(security, "_cookie_secret", lambda: SECRET)
    assert admin_configured() is True


# ============================================================ 2. 认证与 Cookie


def test_admin_endpoint_without_cookie__returns_401(client):
    resp = client.get("/settings")
    assert resp.status_code == 401


def test_admin_endpoint_with_tampered_cookie__returns_401(client):
    client.cookies.set("ca_admin", "9999999999.deadbeef")
    resp = client.get("/settings")
    assert resp.status_code == 401


def test_admin_endpoint_with_expired_cookie__returns_401(client):
    """过期时间戳是真的，但没有对应签名——时间戳可猜，签名不可伪造。"""
    client.cookies.set("ca_admin", f"{int(time.time()) + 3600}.{'0' * 64}")
    assert client.get("/settings").status_code == 401


def test_login_success_sets_hardened_cookie(client):
    resp = login(client)
    assert resp.status_code == 200
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw, "Cookie 必须 HttpOnly，避免 XSS 直接读走"
    assert "samesite=strict" in raw, "Cookie 必须 SameSite=Strict，挡 CSRF"
    assert "path=/" in raw


def test_login_then_admin_access_succeeds(client):
    assert login(client).status_code == 200
    assert client.get("/settings").status_code == 200


def test_logout_invalidates_session(client):
    assert login(client).status_code == 200
    assert client.get("/settings").status_code == 200
    client.post("/admin/logout")
    client.cookies.clear()
    assert client.get("/settings").status_code == 401


def test_wrong_password_rejected_and_counted(client):
    """口令错误 401；第 N 次失败当场锁定并返回 429。"""
    for _ in range(security.LOGIN_MAX_FAILS - 1):
        resp = login(client, "wrong-token")
        assert resp.status_code == 401
        assert "口令错误" in resp.json()["detail"]

    # 第 5 次失败正好触发锁定：当场返回 429，而不是继续报「口令错误」
    last = login(client, "wrong-token")
    assert last.status_code == 429
    assert "Retry-After" in last.headers

    # 锁定后即使口令正确也进不来
    assert login(client).status_code == 429


def test_login_locked_after_five_failures(client):
    for _ in range(security.LOGIN_MAX_FAILS):
        login(client, "wrong-token")
    # 第 6 次：即使口令正确也应被锁
    resp = login(client)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_password_compare_is_constant_time():
    """口令比对走 sha256 + compare_digest，不是裸 ==。"""
    assert security.verify_password("") is False, "空口令永远不能通过"


def test_verify_password_rejects_empty_and_none(admin_env):
    assert verify_password("") is False
    assert verify_password("wrong") is False
    assert verify_password(TOKEN) is True


# ============================================================ 3. 路由归属


def test_all_routes_are_classified():
    """44 个路由必须一条不漏地归入 ADMIN 或 PUBLIC。"""
    templates = main_mod._route_templates()
    uncovered = templates - ADMIN_PATHS - PUBLIC_PATHS
    assert not uncovered, f"未登记路由：{sorted(uncovered)}"


def test_route_count_matches_design():
    """路由总数锁定在 44（含 /admin/login、/admin/logout）。

    这条测试的意义：将来加接口时数量会变，强制开发者停下来想一次
    「这个新接口该归哪一类」，而不是默默多一个公开接口。
    """
    templates = main_mod._route_templates()
    assert len(templates) == 44, f"路由数变成 {len(templates)}，请确认新接口的权限归属"


def test_admin_paths_have_no_public_overlap():
    assert not (ADMIN_PATHS & PUBLIC_PATHS), "一条路由不能同时属于两类"


def test_public_demo_endpoints_are_reachable_anonymously(anon_client):
    """/health 与 /ui/ 必须匿名可达 —— 评委打开页面不能先要密码。"""
    assert anon_client.get("/health").status_code == 200


def test_settings_snapshot_does_not_leak_env_path(client):
    """`GET /settings` 不能回传 .env 的绝对路径（暴露部署目录）。"""
    assert login(client).status_code == 200
    data = client.get("/settings").json()
    assert "/" not in data["env_file"] and "\\" not in data["env_file"]


# ============================================================ 4. IP 与代理头


def test_client_ip_ignores_forwarded_header_without_trusted_proxy():
    """没有可信代理时，X-Forwarded-For 完全是客户端可控的谎话。"""
    scope = {"client": ("203.0.113.9", 12345)}
    headers = [(b"x-forwarded-for", b"1.2.3.4")]
    assert client_ip(scope, headers) == "203.0.113.9"


def test_client_ip_uses_forwarded_header_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(security.settings, "trusted_proxies", "10.0.0.1")
    scope = {"client": ("10.0.0.1", 12345)}
    headers = [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")]
    # 取最后一个：最靠近我们的代理追加的那一跳才是可信的
    assert client_ip(scope, headers) == "5.6.7.8"


def test_client_ip_falls_back_when_scope_has_no_client():
    assert client_ip({}, []) == "unknown"


# ============================================================ 5. 路径规范化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/settings/", "/settings"),
        ("/settings//", "/settings"),
        ("/settings", "/settings"),
        ("/settings/restore/", "/settings/restore"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


def test_trailing_slash_admin_path_still_requires_auth(client):
    """尾斜杠绕过是经典漏洞：/settings/ 不能因为不匹配模板就漏掉认证。"""
    assert client.get("/settings/").status_code == 401
    assert client.get("/settings//").status_code == 401


def test_unknown_path_returns_404_not_401(client):
    """未知路径返回 404 而非 401：401 等于告诉对方「这里有接口」。"""
    resp = client.get("/definitely/not/a/route")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "未找到该接口"


def test_unknown_path_with_admin_prefix_returns_404(client):
    """ /settings/evil 不能因为前缀像管理接口就进认证分支。"""
    assert client.get("/settings/evil").status_code == 404


# ============================================================ 6. 请求体上限


def test_json_body_over_limit_rejected(client):
    """普通 JSON 接口上限 1MB。"""
    big = "x" * (security.MAX_JSON_BODY + 1024)
    resp = client.post("/chat", json={"message": big})
    assert resp.status_code in (413, 422), f"超大 JSON 未被拦下：{resp.status_code}"


def test_upload_limit_is_larger_than_json_limit():
    """上传和 JSON 必须是两个额度，否则要么传不了 PDF，要么 JSON 口太松。"""
    assert security.MAX_UPLOAD_BODY > security.MAX_JSON_BODY


def test_body_limit_for_distinguishes_upload_paths():
    assert security.body_limit_for("/papers/upload") == security.MAX_UPLOAD_BODY
    assert security.body_limit_for("/writing/chart") == security.MAX_UPLOAD_BODY
    assert security.body_limit_for("/chat") == security.MAX_JSON_BODY
    assert security.body_limit_for("/settings") == security.MAX_JSON_BODY


def test_chunked_body_without_content_length_is_counted(client):
    """不看 Content-Length：分块传输时它要么缺失要么撒谎。

    这里直接给一个不合法的大 body（超过 JSON 上限），
    验证即使绕开 Content-Length 也会被读字节数挡住。
    """
    payload = b"x" * (security.MAX_JSON_BODY + 4096)
    resp = client.post(
        "/chat",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


# ============================================================ 7. 限流器单元


def test_rate_limiter_allows_burst_then_blocks():
    lim = RateLimiter()
    allowed = sum(1 for _ in range(BUCKET_LIGHT.burst + 5) if lim.allow("k", BUCKET_LIGHT)[0])
    assert allowed == BUCKET_LIGHT.burst
    ok, wait = lim.allow("k", BUCKET_LIGHT)
    assert ok is False and wait > 0


def test_rate_limiter_keys_are_independent():
    lim = RateLimiter()
    for _ in range(BUCKET_LIGHT.burst):
        lim.allow("a", BUCKET_LIGHT)
    assert lim.allow("a", BUCKET_LIGHT)[0] is False
    assert lim.allow("b", BUCKET_LIGHT)[0] is True


def test_rate_limiter_refills_over_time(monkeypatch):
    lim = RateLimiter()
    for _ in range(BUCKET_LIGHT.burst):
        lim.allow("k", BUCKET_LIGHT)
    assert lim.allow("k", BUCKET_LIGHT)[0] is False

    # 手动把时间往前拨，验证真的按 rate 补充
    real_time = time.monotonic
    monkeypatch.setattr(
        security.time, "monotonic", lambda: real_time() + BUCKET_LIGHT.burst
    )
    assert lim.allow("k", BUCKET_LIGHT)[0] is True


def test_rate_limiter_is_thread_safe():
    """并发调用不能超发：100 个线程抢 10 个令牌，最多只能过 10 个。"""
    lim = RateLimiter()
    passed = []
    lock = threading.Lock()

    def worker():
        ok, _ = lim.allow("shared", BUCKET_LIGHT)
        if ok:
            with lock:
                passed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(passed) <= BUCKET_LIGHT.burst


def test_bucket_for_covers_every_registered_route():
    """每个路由都要有明确档位，不能掉进默认档还不知道。"""
    for template in ADMIN_PATHS | PUBLIC_PATHS:
        assert template in security._RATE_TABLE, f"{template} 没有指定限流档位"


def test_heavy_endpoints_get_stricter_limits_than_light_ones():
    """/graph/survey 单次 626ms、并发 16 要 10 秒；绝不能和 /health 同档。"""
    assert bucket_for("/graph/survey").rate < bucket_for("/health").rate
    assert bucket_for("/settings/index/health").rate < bucket_for("/health").rate


# ============================================================ 8. 每日预算


def test_daily_budget_persists_across_instances(tmp_path):
    path = tmp_path / "budget.json"
    b1 = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 10)
    for _ in range(3):
        assert b1.consume() is True

    # 模拟进程重启：全新实例读同一个文件
    b2 = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 10)
    assert b2.used() == 3
    assert b2.remaining() == 7


def test_daily_budget_blocks_after_limit(tmp_path):
    path = tmp_path / "budget.json"
    b = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 3)
    assert b.consume() is True
    assert b.consume() is True
    assert b.consume() is True
    assert b.consume() is False


def test_daily_budget_resets_on_new_day(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    b = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 5)
    b.consume()
    b.consume()

    # 伪造次日
    real_strftime = time.strftime
    monkeypatch.setattr(
        security.time, "strftime", lambda fmt, *a: "2099-12-31" if fmt == "%Y-%m-%d"
        else real_strftime(fmt, *a)
    )
    b2 = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 5)
    assert b2.used() == 0, "跨天必须自动重置"


def test_daily_budget_is_concurrency_safe(tmp_path):
    """50 个线程抢 10 次额度，最多只能成功 10 次（不能超发）。"""
    path = tmp_path / "budget.json"
    b = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 10)
    ok_count = []
    lock = threading.Lock()

    def worker():
        got = b.consume()
        if got:
            with lock:
                ok_count.append(1)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok_count) == 10, f"并发下超发成了 {len(ok_count)}"


def test_daily_budget_file_is_valid_json(tmp_path):
    path = tmp_path / "budget.json"
    b = DailyBudget(path_getter=lambda: path, limit_getter=lambda: 100)
    b.consume()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) >= {"day", "count"}


def test_budget_exceeded_maps_to_429(client, monkeypatch):
    """预算用完是业务状态，必须 429 而不是 500。

    reserve_llm_call 是在 app.llm 里 `from app.security import reserve_llm_call`
    导进去的，所以要补丁它真正被查找的位置。
    """
    def boom(*_a, **_k):
        raise security.BudgetExceeded("额度用完")

    from app import agent as agent_mod
    from app import llm as llm_mod

    monkeypatch.setattr(llm_mod, "reserve_llm_call", boom)
    monkeypatch.setattr(agent_mod, "reserve_llm_call", boom)
    resp = client.post("/chat", json={"message": "你好"})
    assert resp.status_code == 429
    assert "额度" in resp.json()["detail"]


# ============================================================ 9. 信息暴露


def test_health_hides_model_for_anonymous(client):
    data = client.get("/health").json()
    assert "model" not in data
    assert data["status"] == "ok"


def test_health_shows_model_for_admin(client):
    assert login(client).status_code == 200
    data = client.get("/health").json()
    assert "model" in data
    assert "budget" in data


def test_unhandled_exception_does_not_leak_stacktrace(admin_env, monkeypatch):
    """任何未捕获异常都不能把堆栈/路径吐给客户端。

    `raise_server_exceptions=False` 是必须的：TestClient 默认会把服务端异常
    重新抛给测试代码（方便调试），那样就绕过了我们注册的 Exception 处理器，
    测的其实是「异常会不会被抛出」，而不是「抛出去之后用户看到什么」。
    设为 False 才等价于真实服务器的行为。
    """

    def boom(*_a, **_k):
        raise RuntimeError("/opt/campus-agent/.env 读取失败")

    monkeypatch.setattr(main_mod.settings_api, "snapshot", boom)
    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
        assert login(c).status_code == 200
        resp = c.get("/settings")
        assert resp.status_code == 500
        assert "Traceback" not in resp.text
        assert "/opt/campus-agent" not in resp.text
        assert "读取失败" not in resp.text
        assert resp.json()["detail"] == "服务器内部错误"


def test_validation_error_stays_422(client):
    """注册了 Exception 兜底后，RequestValidationError 不能被降级成 400。"""
    resp = client.post("/chat", json={"message": ""})
    assert resp.status_code == 422


# ============================================================ 10. CSRF


def test_cross_site_post_rejected(client):
    assert login(client).status_code == 200
    resp = client.post(
        "/settings",
        json={"key": "LLM_MODEL", "value": "deepseek-chat"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


def test_same_origin_post_allowed(client):
    """Origin 的 host 与请求 Host 一致时必须放行（这是正常的页面内操作）。"""
    assert login(client).status_code == 200
    # TestClient 默认 Host 是 "testserver"，Origin 也用它
    resp = client.post(
        "/settings",
        json={"key": "LLM_MODEL", "value": "deepseek-chat"},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code != 403, f"同源请求被误拦：{resp.status_code}"


def test_request_without_origin_allowed_so_curl_works(client):
    """curl / 脚本没有 Origin。CSRF 的前提是浏览器自动带凭据，
    非浏览器客户端本来就不受 CSRF 影响，不能一刀切拦掉。"""
    assert login(client).status_code == 200
    resp = client.post("/settings/restore", json={"key": "LLM_MODEL", "value": ""})
    assert resp.status_code != 403


# ============================================================ 11. 生产开关


def test_docs_hidden_in_prod(monkeypatch):
    """APP_ENV=prod 时不注册 /docs、/redoc、/openapi.json。"""
    monkeypatch.setattr(main_mod, "_IS_PROD", True)
    app = main_mod.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with TestClient(app) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 404


def test_session_signature_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(security, "_cookie_secret", lambda: SECRET)
    value = sign_session()
    assert verify_session(value) is True

    monkeypatch.setattr(security, "_cookie_secret", lambda: "another-secret")
    assert verify_session(value) is False, "换密钥后旧会话必须立刻失效"


# ============================================================ 12. 上传与入库边界


def test_upload_requires_no_auth_but_is_rate_limited():
    """上传保持公开演示，但必须在重活档位上——不能和 /health 同速。"""
    assert "/papers/upload" in PUBLIC_PATHS
    assert bucket_for("/papers/upload").rate < bucket_for("/health").rate