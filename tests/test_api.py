"""接口冒烟测试：确认服务能启动、关键接口有响应。

为什么叫"冒烟测试"？
    源自硬件行业——新板子第一次通电，先看冒不冒烟。
    软件里指"最基本的功能还能不能用"，跑得快、不依赖外部服务。

这些测试故意**不调用真实的 LLM**（要花钱、还不稳定），
只验证"服务活着、路由没写错、参数校验生效"。
"""

from fastapi.testclient import TestClient

from app import main as main_mod
from app.main import app

client = TestClient(app)


def test_health_endpoint():
    """/health 必须永远能返回 200 —— 部署后靠它判断服务是否存活。

    注意 model 字段：安全整改后它**只对管理员返回**。
    探活方（Nginx / Docker）不该看到用的是哪家上游、哪个型号，
    那属于运维细节。所以这里断言的是「匿名访问拿不到 model」。
    """
    resp = client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "model" not in data, "匿名访问 /health 不应泄露模型名"


def test_version_comes_from_pyproject():
    """版本号只能有一个真源：pyproject.toml。

    app/main.py 的 _read_version() 先问 importlib.metadata（装过就返回包装版本），
    装不上才回落到直接读 pyproject.toml。两条路的结果必须一致——
    以前这里是硬编码 0.1.0，pyproject 里也写一份，改一处忘一处就会对不上。
    所以这里不写死期望值，而是**比对两个来源**。
    """
    import tomllib

    with (main_mod.BASE_DIR / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    assert main_mod.VERSION == declared, (
        f"/health 报的版本 {main_mod.VERSION} 与 pyproject.toml 的 {declared} 不一致"
    )
    assert main_mod.VERSION != "unknown", "版本号回落到了兜底值，说明两条读取路径都不通"


def test_openapi_schema_is_valid():
    """FastAPI 能生成接口文档，说明所有路由定义语法正确。

    dev 环境下文档开着；生产（APP_ENV=prod）会被关掉，
    那条行为由 test_security.py::test_docs_hidden_in_prod 单独验证。
    """
    resp = client.get("/openapi.json")
    assert resp.status_code == 200

    paths = resp.json()["paths"]
    # 这四个接口是第 3 周交付的核心，缺一个都算回归
    for expected in ["/health", "/chat", "/chat/stream", "/upload"]:
        assert expected in paths, f"接口 {expected} 不见了"


def test_chat_rejects_empty_message():
    """参数校验：空问题应该被挡在门外，而不是送到模型那里浪费钱。"""
    resp = client.post("/chat", json={"message": ""})
    assert resp.status_code == 422  # 422 = 参数不合法


def test_chat_rejects_missing_field():
    resp = client.post("/chat", json={})
    assert resp.status_code == 422


def test_upload_rejects_unsupported_extension():
    """只允许 pdf/md/txt，其他一律拒绝。"""
    resp = client.post(
        "/upload",
        files={"file": ("evil.exe", b"hello", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_accepts_txt(tmp_path):
    """正常文件应该能存下来。"""
    resp = client.post(
        "/upload",
        files={"file": ("note.txt", b"hello campus agent", "text/plain")},
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "note.txt"


def test_upload_strips_path_traversal():
    """安全测试：文件名里的路径必须被剥掉，不能写到别处去。

    如果哪天有人把 Path(filename).name 去掉，这个测试会立刻失败。
    """
    resp = client.post(
        "/upload",
        files={"file": ("../../../evil.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 200
    # 返回值里不能出现 .. 或路径分隔符
    name = resp.json()["filename"]
    assert ".." not in name
    assert "/" not in name and "\\" not in name
