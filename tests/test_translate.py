"""翻译功能（1.3.3）的测试：不真调模型，只验证拼装、缓存与接口契约。

分三层：
    1. 纯函数：文本清洗、页码解析、缓存读写
    2. 接口：404 / 503（没配 Key）/ 正常返回（把 translate_text 换成假的）
    3. 静态：reader.html 里得有译文栏、SSE、划词翻译这些锚点
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.translate import _chunks, _clean_block, page_texts

client = TestClient(app)
BASE_DIR = Path(__file__).resolve().parent.parent
PAPERS_DIR = BASE_DIR / "data" / "papers"
READER = BASE_DIR / "web" / "reader.html"


def _load_script(name: str):
    """scripts/ 不是包，按文件路径加载（parse_pages 这类纯函数才测得到）。"""
    path = BASE_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_clean_block_removes_control_chars_and_hyphenation():
    raw = "inter-\nesting work\x00 with  \x01bullets\u00ad"
    out = _clean_block(raw)
    assert "\x00" not in out and "\x01" not in out
    assert "interesting work with bullets" in out
    assert "  " not in out


def test_chunks_split_long_text_at_paragraphs():
    text = "\n\n".join(["a" * 4000, "b" * 4000, "c" * 4000])
    parts = _chunks(text)
    assert len(parts) >= 3
    assert all(len(p) <= 6000 for p in parts)


def test_parse_pages_handles_all_and_ranges():
    mod = _load_script("translate_papers")
    assert mod.parse_pages("1-3,7", ref_page=10, total_pages=12) == [1, 2, 3, 7]
    assert mod.parse_pages("all", ref_page=11, total_pages=14) == list(range(1, 11))
    # 没有 References 页时 all 就翻全文
    assert mod.parse_pages("all", ref_page=None, total_pages=3) == [1, 2, 3]
    # 超出总页数的页码被丢掉
    assert mod.parse_pages("9-99", ref_page=None, total_pages=12) == list(range(9, 13))
    assert mod.parse_pages("9-99", ref_page=None, total_pages=5) == []


def test_translation_cache_roundtrip(tmp_path, monkeypatch):
    import app.translate as tr

    monkeypatch.setattr(tr, "TRANSLATIONS_DIR", tmp_path / "translations")
    assert tr.translation_file("abc.pdf").name == "abc.json"
    assert tr.load_translation("abc.pdf")["pages"] == {}

    data = {"pages": {"3": {"hash": "x", "markdown": "第三页译文"}}}
    saved = tr.save_translation("abc.pdf", data)
    assert saved.is_file()
    back = tr.load_translation("abc.pdf")
    assert back["pages"]["3"]["markdown"] == "第三页译文"
    assert back["source"] == "abc.pdf"


def test_translation_file_uses_stem(tmp_path, monkeypatch):
    import app.translate as tr

    monkeypatch.setattr(tr, "TRANSLATIONS_DIR", tmp_path)
    assert tr.translation_file("1607.06450v1.pdf") == tmp_path / "1607.06450v1.json"


def test_unknown_paper_returns_404():
    assert client.get("/paper/nope.pdf/translation").status_code == 404
    assert client.get("/paper/nope.pdf/translate?pages=1").status_code == 404


def test_translate_and_selection_require_api_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    assert client.get("/paper/1607.06450v1.pdf/translate?pages=1").status_code == 503
    r = client.post("/translate/selection", json={"text": "hello"})
    assert r.status_code == 503
    assert "LLM_API_KEY" in r.json()["detail"]


def test_selection_translation_returns_markdown(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_translate(text: str) -> str:
        seen["text"] = text
        return "**译文**"

    monkeypatch.setattr("app.main.translate_text", fake_translate)
    r = client.post(
        "/translate/selection",
        json={"text": "Layer normalization stabilizes RNNs.", "context": "LayerNorm"},
    )
    assert r.status_code == 200
    assert r.json()["markdown"] == "**译文**"
    # 上下文会拼到正文前面，并且明确告诉模型"别翻它"
    assert "LayerNorm" in seen["text"]
    assert "不要翻译它" in seen["text"]
    assert "Layer normalization stabilizes RNNs." in seen["text"]


def test_page_texts_real_paper():
    pdf = PAPERS_DIR / "1607.06450v1.pdf"
    if not pdf.is_file():
        pytest.skip("data/papers 不在（被 gitignore，换机器要重新下载）")
    texts = page_texts(pdf, max_pages=3)
    assert sorted(texts) == [1, 2, 3]
    assert len(texts[1]) > 300
    assert "Layer Normalization" in texts[1]
    # 清洗后不该再有控制字符
    assert not any(c in texts[1] for c in "\x00\x01\x02")


def test_reader_html_has_translation_pane():
    html = READER.read_text(encoding="utf-8")
    for needle in (
        'id="transPane"',
        "EventSource",
        "/translate/selection",
        "function mdToHtml",
        "function startTranslate",
        "function scrollTransTo",
    ):
        assert needle in html, needle


def test_translation_routes_registered():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/paper/{filename}/translation" in paths
    assert "/paper/{filename}/translate" in paths
    assert "/translate/selection" in paths


def test_translate_rejects_unparsable_pages():
    r = client.get("/paper/1607.06450v1.pdf/translate?pages=abc")
    assert r.status_code == 400
    assert "pages" in r.json()["detail"]


def _collect(agen):
    async def run():
        return [ev async for ev in agen]

    return asyncio.run(run())


def test_translate_pages_empty_list_calls_nothing(monkeypatch):
    pdf = PAPERS_DIR / "1607.06450v1.pdf"
    if not pdf.is_file():
        pytest.skip("data/papers 不在")
    import app.translate as tr

    called: list[str] = []

    async def fake_chat(messages, temperature=0.3):
        called.append(messages[-1]["content"])
        return "不该被调用"

    monkeypatch.setattr(tr, "chat", fake_chat)
    events = _collect(tr.translate_pages("empty.pdf", pdf, pages=[]))
    assert events == [{"type": "done", "total": 0, "translated": []}]
    assert called == []


def test_translate_pages_hits_cache_without_calling_model(tmp_path, monkeypatch):
    pdf = PAPERS_DIR / "1607.06450v1.pdf"
    if not pdf.is_file():
        pytest.skip("data/papers 不在")
    import app.translate as tr

    monkeypatch.setattr(tr, "TRANSLATIONS_DIR", tmp_path)
    first = tr.page_texts(pdf, max_pages=1)[1]
    tr.save_translation("cached.pdf", {"pages": {"1": {"hash": tr._digest(first), "markdown": "缓存里的译文"}}})

    async def fake_chat(messages, temperature=0.3):
        raise AssertionError("命中缓存时不应该调用模型")

    monkeypatch.setattr(tr, "chat", fake_chat)
    events = _collect(tr.translate_pages("cached.pdf", pdf, pages=[1]))
    assert events[0]["page"] == 1
    assert events[0]["cached"] is True
    assert events[0]["markdown"] == "缓存里的译文"
    assert events[-1]["type"] == "done"


def test_translate_pages_saves_new_translation(tmp_path, monkeypatch):
    pdf = PAPERS_DIR / "1607.06450v1.pdf"
    if not pdf.is_file():
        pytest.skip("data/papers 不在")
    import app.translate as tr

    monkeypatch.setattr(tr, "TRANSLATIONS_DIR", tmp_path)

    async def fake_chat(messages, temperature=0.3):
        return "**第一页译文**"

    monkeypatch.setattr(tr, "chat", fake_chat)
    events = _collect(tr.translate_pages("fresh.pdf", pdf, pages=[1]))
    assert events[0]["markdown"] == "**第一页译文**"
    assert events[0]["cached"] is False
    back = tr.load_translation("fresh.pdf")
    assert back["pages"]["1"]["markdown"] == "**第一页译文**"
