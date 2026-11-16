"""1.1.2「图表 chart」：图表清单与按需裁切。

数据层依赖 data/papers（被 gitignore），没有数据时整组 skip；
静态层永远跑：路由存在、越界 404、缓存目录可写。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import figures as fig_mod
from app.main import app
from app.reader import list_reader_papers, reader_meta

client = TestClient(app)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def test_reader_html_has_figure_section() -> None:
    """静态检查：阅读页侧栏要有「图表」清单，并且缩略图走我们自己的裁切路由。"""
    html = (WEB_DIR / "reader.html").read_text(encoding="utf-8")
    assert 'id="figList"' in html
    assert 'id="figCount"' in html
    assert "/figure/" in html
    assert "data-goto=\"fig\"" in html


def _papers() -> list[dict]:
    items = list_reader_papers()
    if not items:
        pytest.skip("data/papers 里没有 PDF，跳过图表数据层用例")
    return items


@pytest.fixture()
def figures_tmp(tmp_path, monkeypatch):
    """裁出来的图一律落到临时目录，别往仓库里写 PNG。"""
    monkeypatch.setattr(fig_mod, "FIGURES_DIR", tmp_path)
    return tmp_path


def test_reader_meta_lists_figures_with_one_based_pages() -> None:
    papers = _papers()
    meta = None
    for item in papers:
        candidate = reader_meta(item["filename"])
        if candidate and candidate.get("figures"):
            meta = candidate
            break
    if meta is None:
        pytest.skip("现有论文里没有解析到图表题注")

    figs = meta["figures"]
    assert figs, "figures 不能为空"
    for fig in figs:
        assert fig["page"] >= 1, f"页码要 1 基: {fig}"
        assert isinstance(fig["index"], int) and fig["index"] >= 0
        assert fig["caption"], "空题注不该出现在清单里"


def test_formula_samples_are_short_snippets() -> None:
    papers = _papers()
    meta = None
    for item in papers:
        candidate = reader_meta(item["filename"])
        if candidate and candidate.get("formula_samples"):
            meta = candidate
            break
    if meta is None:
        pytest.skip("现有论文里没有公式片段")

    for row in meta["formula_samples"]:
        assert row["page"] >= 1
        assert 0 < len(row["snippet"]) <= 41  # 40 字 + 可能一个省略号


def test_figure_endpoint_returns_png(figures_tmp) -> None:
    papers = _papers()
    for item in papers:
        meta = reader_meta(item["filename"])
        if not meta or not meta.get("figures"):
            continue
        index = meta["figures"][0]["index"]
        r = client.get(f"/paper/{item['filename']}/figure/{index}")
        if r.status_code != 200:
            continue  # 这篇裁不出来（例如纯矢量图又渲染失败），换下一篇
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert r.headers.get("x-figure-crop") in {"figure", "page"}
        # 第二次命中磁盘缓存，内容字节应完全一样
        again = client.get(f"/paper/{item['filename']}/figure/{index}")
        assert again.content == r.content
        return
    pytest.skip("没有一篇论文能裁出图（可能 data/papers 为空）")


def test_figure_endpoint_404_for_bad_index(figures_tmp) -> None:
    papers = _papers()
    name = papers[0]["filename"]
    r = client.get(f"/paper/{name}/figure/9999")
    assert r.status_code == 404
    assert r.json()["detail"] == "没有这张图"


def test_figure_endpoint_404_for_unknown_paper() -> None:
    r = client.get("/paper/not-here.pdf/figure/0")
    assert r.status_code == 404
    assert r.json()["detail"] == "论文不存在"


def test_prebuilt_figure_is_served_without_recropping(tmp_path, monkeypatch) -> None:
    """重建时预裁好的图必须被接口直接用，而不是再裁一遍。

    回归的真实 bug：scripts/rebuild_structures.py 会把图导出成
    data/meta/figures/<stem>_fig<N>.png 并写进 structures.json 的
    figures[i]["image"]，但 /paper/{f}/figure/{i} 从来没读过这个字段，
    每次仍走 figure_bytes() 重新渲染——316 张预裁图等于白算。

    这里造一条假记录 + 一张假 PNG，断言接口原样吐出来，
    从而证明"预裁优先"这条路径没被改掉。
    """
    png = b"\x89PNG\r\n\x1a\n" + b"prebuilt-marker" * 4
    shot = tmp_path / "fakepaper_fig0.png"
    shot.write_bytes(png)
    # 必须改 app.main 里的那个名字：main.py 是 `from app.figures import FIGURES_DIR`，
    # import 时已经把对象绑进自己的命名空间，改 app.figures.FIGURES_DIR 对 _figure_png 无效。
    monkeypatch.setattr("app.main.FIGURES_DIR", tmp_path)

    fake = {"figures": [{"page": 0, "number": "1", "caption": "Figure 1", "image": shot.name}]}
    monkeypatch.setattr("app.main.load_structures", lambda: {"fakepaper.pdf": fake})
    monkeypatch.setattr("app.main.safe_paper_path", lambda name: fake_pdf())
    # 万一走了按需裁这条，直接让它失败，好让测试暴露出"没读预裁图"
    monkeypatch.setattr(
        "app.main.figure_bytes",
        lambda *a, **k: pytest.fail("不应该重新裁图：预裁图存在时必须直接读文件"),
    )

    from app.main import _figure_png

    result = _figure_png(fake_pdf(), 0)
    assert result is not None
    data, crop = result  # type: ignore[misc]
    assert crop == "prebuilt", f"应走预裁路径，实际 crop={crop}"
    assert data == png, "返回的字节必须和预裁文件一模一样"


def fake_pdf() -> Path:
    """一个存在的假 PDF 路径（_figure_png 只用来取 .name 和 .stem）。"""
    return Path("fakepaper.pdf")


def test_figure_lookup_uses_index_name_not_disk_name(tmp_path, monkeypatch) -> None:
    """上传进来的论文：磁盘名是 <paper_id>.pdf，总表的键是上传原名。

    回归的真实 bug（2026-11-16）：上传入口把文件存成 d9d852e28945.pdf，
    而 structures.json 按 paper.filename（上传原名）索引。
    _figure_png 原来用 `load_structures().get(path.name)` 查表，
    path.name 是哈希名 → 查不到 → figures 当成空列表 → 每张图都 404。
    实测 GPT-2 那篇 5 张图全部 404，同一份代码下别的论文都正常。

    修法是把总表的键（key）显式传进来，不再拿物理文件名顶替。
    """
    png = b"\x89PNG\r\n\x1a\n" + b"index-key-marker" * 4
    shot = tmp_path / "hashpaper_fig0.png"
    shot.write_bytes(png)
    monkeypatch.setattr("app.main.FIGURES_DIR", tmp_path)

    fake = {"figures": [{"page": 0, "number": "1", "caption": "Figure 1", "image": shot.name}]}
    # 总表里只有"上传原名"这个键，哈希名查不到
    monkeypatch.setattr("app.main.load_structures", lambda: {"real title.pdf": fake})

    from app.main import _NO_FIGURE, _figure_png

    disk = Path("d9d852e28945.pdf")
    # 不传 key（老调用方式）→ 拿磁盘名查表 → 查不到 → 视为没有这张图
    assert _figure_png(disk, 0) is _NO_FIGURE, "磁盘名查不到总表时按约定返回 _NO_FIGURE"

    # 传了索引原名 → 命中，并走预裁路径
    with_key = _figure_png(disk, 0, "real title.pdf")
    assert with_key is not _NO_FIGURE, "传了索引名就必须命中总表"
    data, crop = with_key  # type: ignore[misc]
    assert crop == "prebuilt"
    assert data == png
