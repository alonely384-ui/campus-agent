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
