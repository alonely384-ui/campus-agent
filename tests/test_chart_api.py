"""1.6.3 数据可视化：接口层的表单字段解析回归测试。

## 为什么单独写这一条

``POST /writing/chart`` 的 ``kind`` 与 ``note`` 必须是 multipart 表单字段。
早先写法是裸的 ``kind: str = ""``，FastAPI 把它当成**查询参数**，
前端塞进表单里的 kind 永远收不到 —— 实测六种取值（bar/pie/radar/radar3d/
scatter/line）返回的 ``spec.kind`` 一律是默认选型的 line，而且六次
PNG 都是同一个文件。这个 bug 在纯函数层完全看不出来，只能走接口才能发现，
所以用 TestClient 从 HTTP 边界钉死它。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app

CSV = (
    "model,accuracy,f1,params_M\n"
    "BERT-base,84.2,82.1,110\n"
    "RoBERTa,86.7,84.9,125\n"
    "DeBERTa,88.1,86.3,140\n"
    "Ours,89.5,87.9,132\n"
)


def _post(csv_text: str, **form: str):
    files = {"file": ("data.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")}
    return TestClient(app).post("/writing/chart", files=files, data=form)


def test_form_kind_is_honoured():
    """表单里的 kind 必须真的改变 spec.kind，而不是被当成查询参数丢掉。"""
    seen = set()
    for kind in ("bar", "pie", "radar", "radar3d"):
        r = _post(CSV, kind=kind)
        assert r.status_code == 200, r.text[:300]
        spec = r.json()["spec"]
        assert spec["kind"] == kind, f"请求 kind={kind}，返回 {spec['kind']}"
        seen.add(r.json()["png_url"])
    # 不同图型必须导出不同的 PNG（缓存键里带 kind 就是为了这个）
    assert len(seen) > 1, seen


def test_form_kind_absent_falls_back_to_auto_choice():
    r = _post(CSV)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["spec"]["kind"] in ("line", "bar", "scatter", "radar", "radar3d", "pie")


def test_caption_never_contains_chinese():
    """caption 是给论文用的英文，统计摘要里不能夹中文键名。"""
    r = _post("name\nAlice\nBob\nCarol\n")
    assert r.status_code == 200, r.text[:300]
    caption = r.json().get("caption") or ""
    bad = [c for c in caption if "\u4e00" <= c <= "\u9fff"]
    assert not bad, caption