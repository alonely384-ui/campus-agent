"""事件循环阻塞回归测试（第三阶段报告 1.9 节 P0-1）。

**这个文件要防的是什么**
`app/main.py` 的路由几乎都是 `async def`，但内部的同步 IO（读 0.8 MB 的
`structures.json`、PyMuPDF 打开 PDF、glob 扫目录）原先直接在事件循环线程上跑。
后果是：一个 `/paper/x.pdf/structure` 请求能让**同进程的所有其他请求**排到它后面，
第三阶段实测 `/health` 从 18.1 ms 涨到 2263.8 ms（放大 595 倍）。

原有的 540 项测试全部没覆盖这个缺陷，因为它们都是「发一个请求 → 断言响应」，
串行执行时同步阻塞和异步执行的表现完全一样。**必须靠"阻塞时另一个请求还能不能跑"来区分。**

**为什么不用真实磁盘耗时做判据**
真实耗时取决于机器、缓存、PDF 大小，慢机器上会假阳性，快机器上会假阴性。
这里全部用可控的慢函数替换掉被调用的同步函数：一旦路由把它放到线程池，
事件循环就是空闲的，第二个请求立刻能跑完；一旦路由还在事件循环上直接调它，
第二个请求会被拖慢整整 `SLOW` 秒——两者差两个数量级，判定稳定。

**并发是怎么造出来的**
`TestClient` 是同步的，单线程发不了并发请求。所以这里用 httpx 的 ASGITransport
+ `asyncio.gather` 直接驱动 ASGI 应用，不占端口、不启 uvicorn。
"""

from __future__ import annotations

import ast
import asyncio
import time
import types
from pathlib import Path

import httpx
import pytest

from app import main as main_mod

# 慢函数模拟的阻塞时长。取 0.3 s：足够和「事件循环被占用」拉开差距（断言阈值 0.15 s），
# 又不会让整个文件跑太久。
SLOW = 0.3
# 慢请求正在跑时，另一个请求必须在这么久之内跑完。事件循环没被占用时实测是几毫秒。
FAST_BUDGET = SLOW / 2

# 心跳间隔。0.01 s 下 SLOW=0.3 s 的理论心跳数是 ~30 拍，
# 断言阈值取理论值的 1/3（10 拍）——修复后实测 ~30，未修复实测 ~3，两侧留足余量。
TICK = 0.01

# 被测路由里用到的、但不在本文件内的类型。Path 在用例里既要造路径又要接管 glob。
_FAKE_ROOT = Path("data/papers")


def _slow_sleep(seconds: float = SLOW) -> None:
    """模拟同步阻塞的磁盘/CPU 工作。`time.sleep` 真的会占住调用它的线程。"""
    time.sleep(seconds)


class _NoSlowDown:
    """把一组被测函数临时换成慢函数，退出时还原。

    `monkeypatch` 本身就负责还原，这里只是把「换成什么」写在一处，
    免得每个用例里各写一遍 lambda 被 ruff 的 B023 拦住（循环/闭包绑定问题）。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, object]) -> None:
        self._mp = monkeypatch
        self._mapping = mapping

    def __enter__(self) -> _NoSlowDown:
        for name, fake in self._mapping.items():
            self._mp.setattr(main_mod, name, fake)
        return self

    def __exit__(self, *exc: object) -> None:
        return None


async def _raw_asgi_get(
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> tuple[float, int]:
    """**直接**驱动 ASGI 应用，不经过任何传输层。

    这一点是这套用例能不能成立的关键，踩过两次坑：

    1. `TestClient`（httpx 的同步包装）会把整个应用放进自己的工作线程跑，
       事件循环根本不在主线程上，同步阻塞看不出来。
    2. `httpx.ASGITransport` **同样不是 in-process 的**：它内部用 AnyIO 的
       `to_thread` 把整个 ASGI 应用派发到工作线程执行（本项目实测：
       原始未修复代码 + 0.3 s 阻塞函数，`/health` 仍只花 4 ms）。

    只有自己拼 scope、自己 await `app(scope, receive, send)`，应用才真的跑在
    当前事件循环线程上——这时「同步函数占住线程」才会表现为「事件循环停摆」。
    """

    body_sent = False

    async def receive() -> dict:
        # 只投递一次请求体。流式响应（SSE）在发送阶段会反复 await receive() 等断开信号，
        # 如果每次都回 http.request，Starlette 的 BaseHTTPMiddleware 会判成
        # "Unexpected message received: http.request" 并抛异常。第一次之后挂起即可。
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover - 正常路径到不了这里

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": list(headers or []),
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    }

    started = time.perf_counter()
    await main_mod.app(scope, receive, send)
    elapsed = time.perf_counter() - started
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 0)
    return elapsed, status


async def _heartbeats_during(
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> tuple[int, float, int]:
    """在请求执行期间数「事件循环还能不能转」。

    判定原理：起一个每 `TICK` 秒自增一次的协程当心跳。如果被测路由把同步函数
    直接跑在事件循环线程上，心跳在这段时间里**一次都跳不了**；如果函数被
    `run_in_threadpool` 送到线程池，事件循环空闲，心跳会按预期频率照跳。

    相比「并发打两个请求比一下耗时」，心跳法的好处是不依赖第二个请求自身的
    调度开销，也不受传输层影响——实测在未修复代码上稳定复现（心跳 3 次 vs 期望 32），
    在修复后代码上稳定通过。

    `headers` 用来带上管理员 Cookie（`/health` 的预算分支只在登录后才会跑）。

    返回 `(心跳次数, 请求耗时秒, 状态码)`。
    """
    ticks = 0
    running = True

    async def heartbeat() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(TICK)

    beat = asyncio.create_task(heartbeat())
    try:
        # 先让心跳跑几拍，进入稳定节奏再开始计时
        await asyncio.sleep(TICK * 3)
        before = ticks
        elapsed, status = await _raw_asgi_get(path, headers, query_string)
        during = ticks - before
    finally:
        running = False
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass
    return during, elapsed, status


def _assert_loop_free(monkeypatch_tick_result: tuple[int, float, int], path: str, slow_name: str) -> None:
    """断言：慢函数执行期间事件循环仍在转（心跳次数接近理论值）。"""
    during, elapsed, _status = monkeypatch_tick_result
    expected = elapsed / TICK
    # 慢到足够看出差异才有意义；太短的话心跳本来就跳不了几拍，断言没有信息量。
    assert elapsed >= SLOW * 0.9, (
        f"{path} 只花了 {elapsed * 1000:.0f} ms，慢函数可能没被真正调到，"
        f"本用例失去意义（期望至少 {SLOW * 0.9 * 1000:.0f} ms）"
    )
    assert during >= expected / 3, (
        f"{slow_name} 执行期间 {path} 耗时 {elapsed * 1000:.0f} ms，"
        f"事件循环只跳了 {during} 拍（理论 ~{expected:.0f} 拍）—— "
        f"说明同步调用仍在事件循环线程上跑，没有被 run_in_threadpool 送走"
    )


# ---------------------------------------------------------------------------
# 1. 逐条验证「慢函数不再占用事件循环」
#    每一条都对应报告 1.9 节里的一处阻塞点，且都直接把被测同步函数换成慢函数。
# ---------------------------------------------------------------------------


def test_structure_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/paper/{f}/structure` —— reader_meta 每次重读解析 0.8 MB JSON，报告里最重的一处。"""

    def slow_reader_meta(filename: str) -> dict:
        _slow_sleep()
        return {"filename": filename, "title": "慢函数", "sections": []}

    with _NoSlowDown(monkeypatch, {"reader_meta": slow_reader_meta}):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/paper/any.pdf/structure"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "reader_meta")


def test_layout_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/paper/{f}/layout` —— detect_layout 逐页开 PDF，全库冷跑 22.7 秒。"""

    def slow_detect_layout(path) -> dict:  # noqa: ANN001 - 形参类型由被替换的函数决定
        _slow_sleep()
        return {"layout": "double", "pages": []}

    # safe_paper_path 也要放行，否则会先 404
    with _NoSlowDown(
        monkeypatch,
        {"safe_paper_path": lambda name: _fake_path(name), "detect_layout": slow_detect_layout},
    ):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/paper/any.pdf/layout"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "detect_layout")


def test_papers_list_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /papers` —— list_papers 读索引 + build_topics 读 structures.json。"""

    def slow_list_papers() -> list:
        _slow_sleep()
        return []

    with _NoSlowDown(monkeypatch, {"list_papers": slow_list_papers}):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/papers"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "list_papers")


def test_papers_topics_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /papers/topics` —— 列表 + Louvain 分组。"""

    def slow_list_papers() -> list:
        _slow_sleep()
        return []

    with _NoSlowDown(monkeypatch, {"list_papers": slow_list_papers}):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/papers/topics"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "list_papers")


def test_papers_files_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /papers/files` —— glob 扫描 data/papers。"""
    def slow_glob(self, pattern: str, **kw):  # noqa: ANN001, ANN201 - 模拟 Path.glob
        _slow_sleep()
        return iter(())

    with monkeypatch.context() as mp:
        mp.setattr(Path, "glob", slow_glob)
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/papers/files"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "PAPERS_DIR.glob")


def test_reader_papers_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /reader/papers` —— list_reader_papers 逐个读 PDF 元信息。"""

    def slow_list_reader_papers() -> list:
        _slow_sleep()
        return []

    with _NoSlowDown(monkeypatch, {"list_reader_papers": slow_list_reader_papers}):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/reader/papers"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "list_reader_papers")


def test_translation_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/paper/{f}/translation` —— load_translation 同步读译文缓存。"""

    def slow_load_translation(filename: str) -> dict:
        _slow_sleep()
        return {"pages": {}}

    with _NoSlowDown(
        monkeypatch,
        {
            "safe_paper_path": lambda name: _fake_path(name),
            "load_translation": slow_load_translation,
            "references_page_of": lambda name: None,
        },
    ):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/paper/any.pdf/translation"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "load_translation")


def test_figure_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/paper/{f}/figure/{i}` —— load_structures 读 JSON + PyMuPDF 渲染。"""

    def slow_load_structures() -> dict:
        _slow_sleep()
        return {}

    with _NoSlowDown(
        monkeypatch,
        {"safe_paper_path": lambda name: _fake_path(name), "load_structures": slow_load_structures},
    ):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/paper/any.pdf/figure/0"))

    # figure 会因为 figures 为空返回 404，这是对的——重点是被拦在那之前的那次慢读
    assert status == 404
    _assert_loop_free((ticks, elapsed, status), "/health", "load_structures")


def test_translate_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/paper/{f}/translate` —— 路由本身要 fitz 开 PDF 数页数 + 读 structures.json 找参考文献页。

    这是一个 SSE 流式接口：路由前半段（安全校验 + 算页码）跑在事件循环上，
    真正的逐页翻译在 `event_generator` 里异步产出。本用例只关心**前半段**，
    所以把生成器换成空实现——引擎缺 API Key 时它必然抛 `_StreamFailed`，
    那与「会不会阻塞事件循环」无关，掺进来只会让用例变脆。

    注意 `pages=1` 走的是显式页码分支，按设计**不会**去 fitz 数页数
    （`app/main.py:1133` 只看 all/空/`*`），所以这里只剩 `references_page_of` 一处待验。
    """

    def slow_references_page_of(filename: str) -> None:
        _slow_sleep()
        return None

    def empty_translate(*args: object, **kwargs: object) -> object:
        async def gen():
            if False:  # pragma: no cover - 只为让它成为异步生成器
                yield ""
        return gen()

    with _NoSlowDown(
        monkeypatch,
        {
            "safe_paper_path": lambda name: _fake_path(name),
            "references_page_of": slow_references_page_of,
            "settings": _fake_settings_with_key(),
            "translate_pages": empty_translate,
        },
    ):
        ticks, elapsed, status = asyncio.run(_heartbeats_during("/paper/any.pdf/translate", query_string=b"pages=1"))

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/paper/any.pdf/translate", "references_page_of")


def test_health_budget_status_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` 自己也在修的范围里：管理员登录时它会碰磁盘读预算文件。"""

    def slow_status() -> dict:
        _slow_sleep()
        return {"used": 0, "limit": 800}

    with _NoSlowDown(
        monkeypatch,
        {"daily_budget": _fake_budget(slow_status), "verify_session": lambda value: True},
    ):
        # 预算分支只在「带有效管理员 Cookie」时才会走（app/main.py:630），
        # 所以这里必须真的带上 Cookie，否则 /health 早早返回，慢函数根本不会被调到。
        ticks, elapsed, status = asyncio.run(
            _heartbeats_during("/health", headers=[(b"cookie", b"ca_admin=whatever")])
        )

    assert status == 200
    _assert_loop_free((ticks, elapsed, status), "/health", "daily_budget.status")


def test_admin_login_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/admin/login` —— 常量时间口令比较 + HMAC 签名，故意慢，也不能占事件循环。"""

    def slow_verify_password(token: str) -> bool:
        _slow_sleep()
        return True

    def slow_sign_session() -> str:
        _slow_sleep()
        return "signed-value"

    with _NoSlowDown(
        monkeypatch,
        {
            "admin_configured": lambda: True,
            "verify_password": slow_verify_password,
            "sign_session": slow_sign_session,
        },
    ):
        elapsed_list: list[float] = []
        slow_elapsed, slow_status = asyncio.run(_login_pair(elapsed_list))

    assert slow_status == 200
    assert slow_elapsed >= SLOW * 0.9
    assert elapsed_list and elapsed_list[0] < FAST_BUDGET, (
        f"/admin/login 执行期间 /health 花了 {elapsed_list[0] * 1000:.0f} ms"
    )


# ---------------------------------------------------------------------------
# 2. 结构断言：这些路由的处理函数里，同步调用必须出现在 run_in_threadpool 里
#    上面那些用例靠「换慢函数」验证行为；这一节靠「读源码」防止以后被人改回去，
#    两者互补——行为测漏了，结构测能兜底。
# ---------------------------------------------------------------------------

# 路由函数名 → 必须被送进线程池的同步函数名
_THREADPOOL_EXPECTATIONS = {
    "papers_list": ["list_papers"],
    "papers_topics": ["list_papers"],
    "papers_delete": ["get_paper", "delete_paper"],
    "reader_papers": ["list_reader_papers"],
    "paper_structure": ["reader_meta"],
    "paper_layout": ["detect_layout", "safe_paper_path"],
    "paper_figure": ["safe_paper_path"],
    "paper_translation": ["safe_paper_path"],
    "paper_translate": ["references_page_of"],
}


def test_blocking_routes_dispatch_off_the_event_loop() -> None:
    """源码级检查：已知会阻塞的函数必须是 `await run_in_threadpool(...)` 的实参。"""
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(main_mod))
    tree = ast.parse(source)

    handlers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert set(_THREADPOOL_EXPECTATIONS) <= set(handlers), (
        "有路由函数改名了，请同步更新 _THREADPOOL_EXPECTATIONS"
    )

    missing: list[str] = []
    for func_name, expected in _THREADPOOL_EXPECTATIONS.items():
        node = handlers[func_name]
        dispatched: set[str] = set()
        # 收集本函数体内所有 `run_in_threadpool(<name>, ...)` 的第一个实参
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "run_in_threadpool"
                and call.args
                and isinstance(call.args[0], ast.Name)
            ):
                dispatched.add(call.args[0].id)
        for name in expected:
            # 允许通过包装函数转交（例如 self._purge_index），这里只查直接调用
            if name in dispatched:
                continue
            # 再查一层：函数体里有没有直接把 name(...) 当语句调用（同步裸调）
            bare = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == name
                and not _is_awaited(c, node)
                for c in ast.walk(node)
            )
            if bare:
                missing.append(f"{func_name}() 直接同步调用了 {name}()")

    assert not missing, "以下调用没走线程池，会阻塞事件循环：\n  " + "\n  ".join(missing)


def _is_awaited(call: ast.Call, func_node: ast.AST) -> bool:
    """判断这个调用是不是 `await run_in_threadpool(...)` 的实参（即已被送走）。"""
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_in_threadpool"
        ):
            if any(arg is call for arg in node.args):
                return True
    return False


# ---------------------------------------------------------------------------
# 3. 改完之后行为不能变：响应内容、权限、限流、体积限制
# ---------------------------------------------------------------------------


def test_response_payloads_unchanged() -> None:
    """`/papers`、`/papers/topics`、`/papers/files`、`/reader/papers` 的顶层结构没变。"""
    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)

    papers = client.get("/papers")
    assert papers.status_code == 200
    body = papers.json()
    assert set(body) == {"total", "papers"}
    # total 必须等于列表长度，说明线程池里组装的 payload 没被截断
    assert body["total"] == len(body["papers"])

    topics = client.get("/papers/topics")
    assert topics.status_code == 200
    tbody = topics.json()
    assert set(tbody) == {"total", "page_size", "topics"}
    assert isinstance(tbody["page_size"], int)
    assert tbody["total"] == sum(t["count"] for t in tbody["topics"])

    files = client.get("/papers/files")
    assert files.status_code == 200
    files_body = files.json()
    assert set(files_body) == {"files"}
    assert files_body["files"] == sorted(files_body["files"])

    reader = client.get("/reader/papers")
    assert reader.status_code == 200
    rbody = reader.json()
    assert set(rbody) == {"total", "papers"}
    assert rbody["total"] == len(rbody["papers"])


def test_structure_and_layout_payloads_are_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构树与版式判定：线程池调用的返回值必须原样透传，不能被包一层。"""
    from fastapi.testclient import TestClient

    sentinel_meta = {"filename": "any.pdf", "title": "标题", "sections": [{"page": 3}]}
    sentinel_layout = {"layout": "double", "pages": [{"page": 1, "columns": 2}]}

    monkeypatch.setattr(main_mod, "reader_meta", lambda name: sentinel_meta)
    monkeypatch.setattr(main_mod, "safe_paper_path", lambda name: _fake_path(name))
    monkeypatch.setattr(main_mod, "detect_layout", lambda path: sentinel_layout)

    client = TestClient(main_mod.app)
    assert client.get("/paper/any.pdf/structure").json() == sentinel_meta
    assert client.get("/paper/any.pdf/layout").json() == sentinel_layout


def test_missing_paper_still_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` 返回值的 404 分支必须还在——线程池调用没把 None 吞成空 dict。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main_mod, "reader_meta", lambda name: None)
    client = TestClient(main_mod.app)
    resp = client.get("/paper/nope.pdf/structure")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "论文不存在"


def test_delete_requires_admin_after_threadpool_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """权限依赖没被线程池改造影响：匿名 DELETE 仍然 401，而不是 404/500。"""
    from fastapi.testclient import TestClient

    from app import security

    monkeypatch.setattr(security, "_admin_token", lambda: "t0ken")
    monkeypatch.setattr(security, "_cookie_secret", lambda: "s3cret")
    client = TestClient(main_mod.app)
    resp = client.delete("/papers/deadbeef0000")
    assert resp.status_code == 401


def test_body_limit_still_enforced() -> None:
    """请求体按**实际字节数**拦截的逻辑没被本次改造带坏。

    用 `TestClient.post` 而不是手搓 ASGI scope：走的是真实通道，行为与线上一致。
    （手搓 scope 会掉进 Starlette 的路由解析路径，错误 detail 会被替换成
    "There was an error parsing the body"，见下面那条用例。）

    为什么打 `/chat` 而不是 `/papers/upload`：上传路径上限是 24 MB
    （`security.MAX_UPLOAD_BODY`），2 MB 根本不超，测出来的是 multipart 解析器的 400。
    `/chat` 走 1 MB 的 JSON 档，才能验到 413。
    为什么用原始 bytes 内容而不是 `json=`：这里要的是**实际字节数**，不是合法 JSON。
    """
    from fastapi.testclient import TestClient

    from app import security

    payload = b'{"message":"' + b"x" * (security.MAX_JSON_BODY + 4096) + b'"}'
    client = TestClient(main_mod.app)
    resp = client.post("/chat", content=payload, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert "超过" in resp.json()["detail"]


def test_body_limit_chunked_path_is_known_gap() -> None:
    """分块通道（没有 Content-Length）的超限请求目前返回 400，不是 413。

    **这是一条"记录现状"的用例，不是"期望的行为"**，写在这里是为了两件事：
    1. 让回归测试如实反映线上行为，不假装 413 已经是全通道生效；
    2. 一旦有人修好了它（把 `_BodyTooLarge` 在 `receive` 包装层就转成 413 响应），
       这条用例会红，逼着他同时改掉这条注释和 `docs/SECURITY-PHASE3-REVIEW.md` 里的记录。

    根因（已在本轮用最小复现确认，与本轮 P0-1 改造无关）：
    FastAPI 在 `get_request_handler` 里把 `request.body()` 的异常统一包成
    `HTTPException(400, "There was an error parsing the body")`，
    我们抛出的 `_BodyTooLarge` 在上传路径体解析阶段就已经被替换掉了，
    到不了注册的异常处理器。实测：带 Content-Length 走 `_safe_content_length` 分支得到 413，
    不带则走分块分支得到 400。
    """
    from app import security

    payload = b'{"message":"' + b"x" * (security.MAX_JSON_BODY + 4096) + b'"}'
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat",
        "raw_path": b"/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    asyncio.run(main_mod.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    # 现状：超限被拦住了（不会真的把 1MB+ 送进去），但状态码是 400 而不是 413。
    # 如果哪天变成 413，请更新本条用例与文档，说明缺口已补。
    assert status == 400, f"分块通道的超限返回 {status}，缺口可能已被修复，请更新本用例与文档"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _fake_path(name: str):
    """给 `safe_paper_path` 用的一个"存在的论文路径"。不需要真文件，只要求 .name 可用。"""
    return _FAKE_ROOT / name


def _fake_settings_with_key():
    """翻译路由要求 `llm_api_key` 非空，用一个只带该属性的哑对象顶替。"""
    return types.SimpleNamespace(llm_api_key="sk-test-not-a-real-key")


def _fake_budget(status_fn):
    return types.SimpleNamespace(status=status_fn)


async def _login_pair(bucket: list[float]) -> tuple[float, int]:
    """并发打 `/admin/login`（故意慢）与 `/health`，把 /health 的耗时写进 bucket。"""
    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

        async def do_login() -> tuple[float, int]:
            started = time.perf_counter()
            resp = await client.post("/admin/login", json={"token": "whatever"})
            return time.perf_counter() - started, resp.status_code

        login_task = asyncio.create_task(do_login())
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        health = await client.get("/health")
        bucket.append(time.perf_counter() - started)
        assert health.status_code == 200
        return await login_task