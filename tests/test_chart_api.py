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
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parent.parent

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


def test_frontend_sends_kind_in_form_not_query():
    """前端必须把 kind / note 放进 FormData。

    FastAPI 的 ``Form(...)`` 只读请求体，不做查询参数回退。前端若把 kind
    拼到 URL 上（``fetch('/writing/chart?' + qs)``），接口收到的永远是空串，
    用户选什么图型都落到自动选型——接口层测试全绿也发现不了，所以这里
    直接读前端源码钉住这个契约。
    """
    html = (ROOT / "web" / "writing.html").read_text(encoding="utf-8")
    assert "fd.append('kind'" in html, "前端没有把 kind 放进 FormData"
    assert "fd.append('note'" in html, "前端没有把 note 放进 FormData"
    assert "qs.set('kind'" not in html, "前端又把 kind 拼回 URL 了"
    assert "writing/chart?' + qs" not in html, "前端又给 /writing/chart 拼查询串了"


def test_caption_never_contains_chinese():
    """caption 是给论文用的英文，统计摘要里不能夹中文键名。"""
    r = _post("name\nAlice\nBob\nCarol\n")
    assert r.status_code == 200, r.text[:300]
    caption = r.json().get("caption") or ""
    bad = [c for c in caption if "\u4e00" <= c <= "\u9fff"]
    assert not bad, caption


def test_radar_export_is_polar_not_bar():
    """radar 导出的必须是极坐标雷达图，不是柱状图。

    早先这里「图省事」把 radar 导成了一根根柱子（各列均值），结果
    caption 按 spec.kind=radar 写着 "Radar chart comparing ..."，
    导出的 PNG 却是柱状图 —— 图文自相矛盾，进论文是要出事的。
    这里直接盯 matplotlib 的 axes 类型，比比对像素可靠。
    """
    import matplotlib

    matplotlib.use("Agg")

    from app import chart as ch

    df = ch.load_csv(CSV)
    captured = {}
    original_savefig = matplotlib.figure.Figure.savefig

    def _spy(self, *a, **kw):
        captured["polar"] = [getattr(ax, "name", "") for ax in self.axes]
        return original_savefig(self, *a, **kw)

    matplotlib.figure.Figure.savefig = _spy
    try:
        png = ch.export_png(df, {"kind": "radar", "x": "model", "y": "accuracy,f1,params_M"})
    finally:
        matplotlib.figure.Figure.savefig = original_savefig

    assert png[:8] == b"\x89PNG\r\n\x1a\n", "导出的不是 PNG"
    assert captured.get("polar") == ["polar"], f"导出的坐标系是 {captured.get('polar')}"