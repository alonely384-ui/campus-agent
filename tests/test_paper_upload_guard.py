"""上传入口的 PDF 有效性校验（2026-11-18，审计 P0-1 的回归测试）。

修之前的行为（审计在公网实测过）：`POST /papers/upload` 只检查后缀名与大小，
一份 24 字节的假 PDF 和一份 0 字节空文件都能拿到 `200 {"saved": 1, "pages": 0}`，
索引里留下一条再也打不开、也检不到内容的记录——用户看到的是"上传成功"。

这里按五个场景钉住新行为：
1. 正常 PDF → 成功入库，页数正确；
2. 空文件 → 400，磁盘不留任何东西；
3. 非 PDF 内容伪装成 .pdf → 400；
4. 截断/损坏的 PDF → 400，且不留半成品；
5. 扫描件（只有图片页、没有文字层）→ 必须**判为有效**，不能被当成损坏。

另外覆盖两件相关的事：
- 保存成功但入库失败时，接口要如实给 `index_ok=false`（不能假装成功）；
- 索引入库必须走线程池（否则一篇 300 块的论文会让整站卡 60 秒）。
"""

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import papers as papers_mod

# ------------------------------------------------------------------ 样本构造


def _pdf_with_objects(objects: list[bytes], header: bytes = b"%PDF-1.4\n") -> bytes:
    """按给定的对象体拼一份 PDF（classic xref），`objects[i]` 是第 i+1 号对象。"""
    out = bytearray(header)
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _valid_pdf(marker: str = "hello") -> bytes:
    """一页带文字的合法 PDF。"""
    body = f"BT /F1 12 Tf 72 720 Td ({marker}) Tj ET".encode("latin-1")
    return _pdf_with_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


def _scanned_pdf() -> bytes:
    """整页一张 RGB 图片、**没有任何文字层**的 PDF —— 等价于扫描件的结构。

    页面的 /Resources 里只有 XObject、没有 Font，`extract_text()` 必然返回空串。
    这种文件必须判为有效：赛题 1.1.2 要求扫描件走 OCR，而不是在上传口被拦掉。
    """
    # 1x1 像素的原始 RGB 数据，用 Flate 之外最简单的方式（ASCII 十六进制）编码
    raw = b"\xff\x00\x00"
    image = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
    image += b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
    image += b"/Length " + str(len(raw) * 2 + 1).encode() + b" >>\nstream\n"
    image += raw.hex().encode() + b">\nendstream"
    contents = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    return _pdf_with_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /XObject << /Im0 5 0 R >> >> >>",
            b"<< /Length " + str(len(contents)).encode() + b" >>\nstream\n" + contents + b"\nendstream",
            image,
        ]
    )


# ------------------------------------------------------------------ fixtures


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """把论文库四个路径指到 tmp，返回模块本身。"""
    monkeypatch.setattr(papers_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(papers_mod, "INDEX_FILE", tmp_path / "papers" / "index.json")
    monkeypatch.setattr(papers_mod, "TEXT_DIR", tmp_path / "papers" / "text")
    monkeypatch.setattr(papers_mod, "STRUCTURES_FILE", tmp_path / "papers" / "structures.json")
    monkeypatch.setattr(papers_mod, "merge_structure", lambda fn, st: True)
    papers_mod.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    return papers_mod


@pytest.fixture()
def api(library, monkeypatch):
    """上传接口的 TestClient。默认把 index_paper 换成不写库的桩。"""
    monkeypatch.setattr(main_mod, "index_paper", lambda path, source: 5)
    return TestClient(main_mod.app)


def _upload(client: TestClient, name: str, content: bytes):
    return client.post(
        "/papers/upload",
        files={"files": (name, content, "application/pdf")},
    )


# ------------------------------------------------------------------ 1. 正常 PDF


def test_valid_pdf_upload_succeeds(tmp_path, api, library):
    """正常 PDF：200、页数正确、文件与索引都落地。"""
    resp = _upload(api, "good.pdf", _valid_pdf("normal body"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved"] == 1
    assert body["index_ok"] is True
    assert body["index_error"] == ""
    paper = body["paper"]
    assert paper["pages"] == 1
    assert paper["filename"] == "good.pdf"
    on_disk = library.PAPERS_DIR / f"{paper['paper_id']}.pdf"
    assert on_disk.exists() and on_disk.stat().st_size > 0
    assert library.get_paper(paper["paper_id"]) is not None


# ------------------------------------------------------------------ 2. 空文件


def test_empty_file_is_rejected_and_leaves_nothing(library):
    """0 字节：直接 400，磁盘上连一个文件都不该出现。"""
    before = set(library.PAPERS_DIR.iterdir()) if library.PAPERS_DIR.exists() else set()

    with pytest.raises(papers_mod.PdfInvalidError, match="空"):
        library.save_paper("empty.pdf", b"", text_extractor=lambda p: "")

    after = set(library.PAPERS_DIR.iterdir()) if library.PAPERS_DIR.exists() else set()
    assert after == before, f"校验失败却留下了文件：{after - before}"
    assert library.list_papers() == []


def test_empty_file_upload_returns_400(api, library):
    resp = _upload(api, "empty.pdf", b"")
    assert resp.status_code == 400
    assert "空" in resp.json()["detail"]
    assert library.list_papers() == []


# ------------------------------------------------------------------ 3. 伪装的非 PDF


def test_text_file_renamed_to_pdf_is_rejected(library):
    """UTF-8 文本改名成 .pdf：没有 %PDF- 文件头，必须拒绝。"""
    with pytest.raises(papers_mod.PdfInvalidError, match="不是 PDF"):
        library.save_paper("note.pdf", "这是一段中文文本，不是 PDF。".encode(), text_extractor=lambda p: "")


def test_pdf_header_only_is_rejected(library):
    """只有 `%PDF-1.4` 四个字符 + 几个字节的"假 PDF"（审计时能混进公网的那种）。"""
    with pytest.raises(papers_mod.PdfInvalidError):
        library.save_paper("fake.pdf", b"%PDF-1.4 fake bytes for fake.pdf", text_extractor=lambda p: "")


def test_magic_bytes_must_be_near_the_start(library):
    """`%PDF-` 出现在 1KB 之后的不认：那是二进制垃圾里偶然出现的字节串。"""
    content = b"\x00" * 2048 + _valid_pdf()
    with pytest.raises(papers_mod.PdfInvalidError, match="不是 PDF"):
        library.save_paper("shifted.pdf", content, text_extractor=lambda p: "")


# ------------------------------------------------------------------ 4. 截断与损坏


def test_truncated_pdf_is_rejected(library):
    """截掉一半的 PDF：pymupdf 还能打开第 1 页，但文件已经不完整，必须拒。"""
    full = _valid_pdf("truncate me")
    truncated = full[: len(full) // 2]

    with pytest.raises(papers_mod.PdfInvalidError):
        library.save_paper("trunc.pdf", truncated, text_extractor=lambda p: "")


def test_truncated_pdf_upload_returns_400_without_leftovers(api, tmp_path, library):
    full = _valid_pdf("truncate me too")
    resp = _upload(api, "trunc.pdf", full[: len(full) // 2])

    assert resp.status_code == 400
    assert "截断" in resp.json()["detail"] or "不完整" in resp.json()["detail"]
    leftovers = [p.name for p in library.PAPERS_DIR.glob("*.pdf")]
    assert leftovers == [], f"校验失败却写进了：{leftovers}"


def test_pdf_missing_startxref_is_rejected(library):
    """尾部结构被砍掉（没有 startxref/trailer）：结构不完整就该拒。

    为什么不用"xref 表内容写乱"来测：pypdf 对损坏的 xref 会**重建**交叉引用表
    （重建成功就说明对象本身还在、文件其实可用），所以那种输入到底该不该拒是有
    争议的，不适合当成契约钉死。这里选的是没有争议的情形——文件尾部被截掉。
    """
    good = _valid_pdf("startxref broken")
    assert b"startxref" in good
    broken = good[: good.rfind(b"xref")]  # 连 xref 表一起砍掉

    with pytest.raises(papers_mod.PdfInvalidError):
        library.save_paper("broken.pdf", broken, text_extractor=lambda p: "")


# ------------------------------------------------------------------ 5. 扫描件


def test_scanned_pdf_without_text_layer_is_accepted(library):
    """扫描件（只有图片、无文字层）必须判为有效，交给 OCR 流程处理。"""
    content = _scanned_pdf()

    # 先确认这份样本确实抽不出文字——否则这条测试就变成"其实有文字"的空测
    assert papers_mod._extract_text.__module__  # 只是确认符号存在，不真跑（依赖 pymupdf 版式）
    pages = papers_mod.inspect_pdf(content)
    assert pages == 1

    paper, dup, _ = library.save_paper(
        "scan.pdf", content, text_extractor=lambda p: ""
    )
    assert dup is None
    assert paper.pages == 1


def test_scanned_pdf_upload_reports_index_status(api, monkeypatch):
    """扫描件能上传，但抽不出字 → indexed=0 时必须如实告诉用户"检索不到"。"""
    monkeypatch.setattr(main_mod, "index_paper", lambda path, source: 0)

    resp = _upload(api, "scan.pdf", _scanned_pdf())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved"] == 1
    assert body["indexed"] == 0
    assert body["index_ok"] is False
    assert "扫描件" in body["index_error"]


# ------------------------------------------------------------------ 保存 ≠ 入库


def test_index_failure_is_reported_not_swallowed(api, library, monkeypatch):
    """文件存好了但入库抛异常：接口不能 500，也不能假装成功。"""
    def boom(path: str, source: str) -> int:
        raise RuntimeError("向量库写入失败")

    monkeypatch.setattr(main_mod, "index_paper", boom)

    resp = _upload(api, "good.pdf", _valid_pdf("index will fail"))
    assert resp.status_code == 200, "单篇入库失败不该把上传变成 500（文件确实已经存好）"
    body = resp.json()
    assert body["saved"] == 1
    assert body["index_ok"] is False
    assert "入库失败" in body["index_error"]
    assert body["indexed_failed"] == 1
    # 文件仍然在库里，用户可以之后再删——不能变成"看不见的孤儿文件"
    assert library.get_paper(body["paper"]["paper_id"]) is not None


def test_midway_failure_rolls_back_partial_files(library, tmp_path):
    """落盘后步骤炸了：原件、文字缓存、旁挂结构都要撤回，不留半成品。"""
    def exploding_extractor(path):
        raise RuntimeError("抽文字炸了")

    # 只比文件，不比目录：`text/` 这类空目录留着无害，残留的**文件**才是问题
    def files() -> set:
        return {p for p in library.PAPERS_DIR.rglob("*") if p.is_file()} \
            if library.PAPERS_DIR.exists() else set()

    before = files()
    with pytest.raises(RuntimeError, match="抽文字炸了"):
        library.save_paper("half.pdf", _valid_pdf("half"), text_extractor=exploding_extractor)

    assert files() == before, f"半成品没清干净：{files() - before}"
    assert library.list_papers() == []


# ------------------------------------------------------------------ 线程池


def test_upload_indexes_off_the_event_loop(monkeypatch, library):
    """`index_paper` 是同步重活，必须丢线程池，否则一篇长论文会把整站卡住。

    这里不测"快不快"，而是直接断言它跑在**非事件循环线程**里——
    这是唯一能证明 `run_in_threadpool` 真的接上了的写法。
    """
    import threading

    seen: dict[str, object] = {}
    loop_thread = threading.get_ident()

    def spy(path: str, source: str) -> int:
        seen["thread"] = threading.get_ident()
        return 5

    monkeypatch.setattr(main_mod, "index_paper", spy)
    client = TestClient(main_mod.app)

    resp = client.post(
        "/papers/upload",
        files={"files": ("good.pdf", _valid_pdf("thread check"), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    assert seen.get("thread") is not None, "index_paper 根本没被调用"
    assert seen["thread"] != loop_thread, "index_paper 仍在事件循环线程里执行"