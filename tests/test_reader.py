"""阅读页（1.3.2）相关接口与静态资源的测试。

分两层：
    1. 静态层 —— web/reader.html 与 vendor 的 pdf.js 必须跟着仓库走，
       克隆下来不联网也能打开阅读页。这一类永远跑，不会因为缺数据而跳过。
    2. 数据层 —— /paper/{filename} 的 Range 支持与路径穿越防护、结构元数据接口。
       data/papers/ 被 .gitignore 排除（39 篇 PDF 几十 MB），干净克隆里没有，
       所以这一层在无数据时 skip 而不是失败：本地跑全绿，CI 上也不会假红。
"""

import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reader import PAPERS_DIR, list_reader_papers, reader_meta, safe_paper_path

client = TestClient(app)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
VENDOR_JS = ("pdf.min.js", "pdf.worker.min.js")


# ---------------- 第 1 层：前端静态资源（永远跑） ----------------

def test_reader_html_exists_and_loads_local_pdfjs():
    """阅读页必须在仓库里，且脚本标签指向本地 vendor 而不是 CDN。"""
    reader = WEB_DIR / "reader.html"
    assert reader.is_file(), "web/reader.html 不见了（1.3.2 的交付物）"

    html = reader.read_text(encoding="utf-8")
    assert "vendor/pdfjs/pdf.min.js" in html
    assert "cdn.jsdelivr.net" not in html and "unpkg.com" not in html, (
        "阅读页不能依赖 CDN：断网评审会白屏"
    )


@pytest.mark.parametrize("name", VENDOR_JS)
def test_pdfjs_vendor_files_committed(name):
    """pdf.js 三个文件里至少 min 版和 worker 版要进仓库，且不是空占位。"""
    path = WEB_DIR / "vendor" / "pdfjs" / name
    assert path.is_file(), f"web/vendor/pdfjs/{name} 缺了，跑 scripts/fetch_pdfjs.py 补"
    assert path.stat().st_size > 50_000, f"{name} 只有 {path.stat().st_size} 字节，像是下载失败"


def test_reader_page_served_by_ui_mount():
    """通过 /ui/ 静态挂载能取到阅读页，说明路径和 URL 约定对得上。"""
    resp = client.get("/ui/reader.html")
    assert resp.status_code == 200
    assert "reader" in resp.text.lower()


def test_reader_html_supports_author_year_citations():
    """作者-年份引用（[Ioffe and Szegedy, 2015]）也要能点：逻辑必须在页面里。

    这类引用没有编号，和 [38] 的跳转是两条路径，所以单独立个用例盯着，
    免得以后改正则时被顺手删掉。
    """
    html = (WEB_DIR / "reader.html").read_text(encoding="utf-8")
    for needle in ("CITE_ANY_RE", "parseAuthorCite", "jumpToAuthorCitation", "cite-a"):
        assert needle in html, f"阅读页里找不到 {needle}，作者-年份引用又断了"


def test_reader_html_marks_borrowed_section_tree():
    """扫描件借用别篇的章节树时，侧栏要注明来源，不能假装是自己解析的。"""
    html = (WEB_DIR / "reader.html").read_text(encoding="utf-8")
    assert "sections_borrowed_from" in html
    assert "tocNote" in html


# ---------------- 第 2 层：论文数据（无数据时跳过） ----------------

def _papers():
    """取论文清单；一个 PDF 都没有（干净克隆）就让整组测试 skip。"""
    papers = list_reader_papers()
    if not papers:
        pytest.skip(f"{PAPERS_DIR} 下没有 PDF（data/papers 被 gitignore），跳过数据层测试")
    return papers


def test_reader_papers_endpoint_lists_all_pdfs():
    """/reader/papers 列出的条数要和磁盘上的 PDF 数一致，且每条都有文件名。"""
    papers = _papers()
    resp = client.get("/reader/papers")
    assert resp.status_code == 200

    data = resp.json()
    assert set(data) == {"total", "papers"}
    assert data["total"] == len(papers)
    assert data["total"] > 0
    for item in data["papers"]:
        assert item["filename"].endswith(".pdf")
        assert item["title"]


def test_structure_endpoint_returns_sections_with_1based_pages():
    """结构接口给阅读页画章节树：章节页码必须是 1 基，references 也得有个页码。"""
    papers = _papers()
    target = next((p for p in papers if p["has_structure"]), None)
    if target is None:
        pytest.skip("这批 PDF 还没有 structures.json 条目，跳过")

    resp = client.get(f"/paper/{target['filename']}/structure")
    assert resp.status_code == 200

    meta = resp.json()
    assert meta["filename"] == target["filename"]
    assert meta["pages"] >= 1
    assert isinstance(meta["sections"], list)
    for sec in meta["sections"]:
        # 1 基页码：JSON 里是 0 基，接口里已经 +1；0 说明忘了转换
        assert sec["page"] >= 1, f"章节 {sec['title']!r} 页码还是 0 基"
        assert 1 <= sec["level"] <= 3
    refs = meta["references"]
    assert refs["page"] is None or refs["page"] >= 1, "参考文献页码同样必须是 1 基"


def test_borrowed_section_tree_exposes_source():
    """扫描件抽不出标题时，章节树借自同篇文本版——接口要把来源带出来。

    前端据此在侧栏提示「页码以文本版为准」，避免用户以为扫描件自己解析出了目录。
    """
    papers = _papers()
    borrowed = next(
        (p for p in papers if reader_meta(p["filename"]).get("sections_borrowed_from")),
        None,
    )
    if borrowed is None:
        pytest.skip("当前数据里没有被借树的论文（structures.json 可能刚被整体重算过）")

    meta = reader_meta(borrowed["filename"])
    assert meta["sections"], "标了借树却没有章节"
    assert meta["sections_borrowed_from"].endswith(".pdf")


def test_structure_endpoint_404_for_unknown_paper():
    """不存在的论文要 404，而不是 500 或返回空结构。"""
    _papers()
    resp = client.get("/paper/not-a-real-paper.pdf/structure")
    assert resp.status_code == 404


def test_pdf_route_serves_pdf_with_range_support():
    """PDF 路由要能整篇下载，也要支持 Range（阅读页滚动时按需取片段）。"""
    papers = _papers()
    name = papers[0]["filename"]
    quoted = urllib.parse.quote(name)

    full = client.get(f"/paper/{quoted}")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("application/pdf")
    assert int(full.headers["content-length"]) > 1000

    partial = client.get(f"/paper/{quoted}", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206, "缺少 Range 支持会让大 PDF 只能整体加载"
    assert partial.headers["content-range"].startswith("bytes 0-99/")
    assert len(partial.content) == 100


@pytest.mark.parametrize(
    "bad_name",
    [
        "../secret.pdf",  # 上一级
        "..%2Fsecret.pdf",  # 编码过的斜杠
        "sub/dir/paper.pdf",  # 带子目录
        "1607.06450v1.txt",  # 扩展名不对
        "not-here.pdf",  # 名字合法但文件不存在
    ],
)
def test_pdf_route_rejects_traversal_and_non_pdf(bad_name):
    """路径穿越、非 PDF、未知文件三种情况都必须 404，绝不能把别的文件读出来。"""
    _papers()
    resp = client.get(f"/paper/{bad_name}")
    assert resp.status_code == 404, f"{bad_name} 应该被拒绝"


def test_safe_paper_path_guards():
    """直接测解析函数：合法名给绝对路径，非法名一律 None。"""
    papers = _papers()
    ok = safe_paper_path(papers[0]["filename"])
    assert ok is not None and ok.is_file()
    assert ok.parent == PAPERS_DIR.resolve()

    for bad in ["", "..", "../app/main.py", "a/b.pdf", "/etc/passwd.pdf", "x.docx"]:
        assert safe_paper_path(bad) is None, f"{bad!r} 竟然通过了校验"
