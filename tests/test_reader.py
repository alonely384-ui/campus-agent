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
from app.reader import (
    PAPERS_DIR,
    list_reader_papers,
    locate_claim_in_pdf,
    reader_meta,
    safe_paper_path,
)

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


# ---------------- 引用出处定位（写作页「读原文」） ----------------

def test_locate_claim_returns_page_and_rect_for_known_paper():
    """给一句英文陈述，要能在这篇 PDF 里定到具体页码和矩形。

    这是「读原文」的核心：不能只回到标题页，得落到支撑那句话的那一段。
    """
    papers = _papers()
    target = next((p for p in papers if p["filename"] == "1706.03762v1.pdf"), None)
    if target is None:
        pytest.skip("本地库里没有 Attention Is All You Need，跳过")

    got = locate_claim_in_pdf(
        "1706.03762v1.pdf",
        "Self-attention allows direct dependencies between any two positions in a sequence, "
        "but its computation and memory costs grow quadratically with sequence length.",
    )
    assert got is not None, "这句在原文里明明有对应段落，不该定位不到"
    # 该段落在第 2 页（第 1 页是标题+摘要）
    assert got["page"] == 2, f"定位到第 {got['page']} 页，应该在正文第 2 页"
    x0, y0, x1, y1 = got["rect"]
    assert x1 > x0 and y1 > y0, "矩形宽高必须为正，否则前端画不出高亮"
    assert got["hits"] >= 2
    assert "attention" in got["snippet"].lower()


def test_locate_claim_is_deterministic():
    """同一句话连测 5 次必须落在完全相同的位置。

    回归用：早先的打分写成 `score > (best[0], -best[1])`，
    命中数与跨度都相等时不判"更大"，结果取决于遍历顺序——
    同一篇论文两次调用分别落在第 7 页和第 10 页。
    """
    papers = _papers()
    name = "2205.14135v2.pdf"
    if not any(p["filename"] == name for p in papers):
        pytest.skip("本地库里没有 FlashAttention，跳过")
    sentence = (
        "FlashAttention adopts tiling and recomputation to reduce the memory footprint "
        "of attention without approximation."
    )
    seen = set()
    for _ in range(5):
        got = locate_claim_in_pdf(name, sentence)
        assert got is not None
        seen.add((got["page"], tuple(got["rect"])))
    assert len(seen) == 1, f"同一输入定位出了 {len(seen)} 种位置：{seen}"


def test_locate_claim_returns_none_for_unknown_file():
    """文件名不存在时返回 None，不能抛异常——前端要能退回标题页。"""
    assert locate_claim_in_pdf("no-such-paper.pdf", "any sentence here") is None


def test_locate_claim_endpoint_404_for_unknown_paper():
    """接口层：未知论文走安全路径校验，404 而不是 500。"""
    _papers()
    resp = client.get("/paper/not-a-real-paper.pdf/claim-locate", params={"q": "attention"})
    assert resp.status_code == 404


def test_reader_html_supports_rect_highlight():
    """阅读页要支持 rect 参数并画独立图层。

    这几个字符串是「读原文」链路的关键拼图，缺一个高亮就画不出来：
      START_RECT     解析 URL 里的矩形
      claimLayer     独立图层（不能混进 searchLayer，两者坐标系不同）
      pdfToCss       pymupdf 左上原点 → pdf.js 左下原点的翻转
    """
    html = (WEB_DIR / "reader.html").read_text(encoding="utf-8")
    assert "START_RECT" in html
    assert "claimLayer" in html
    assert "function pdfToCss" in html
    # 翻转 y 的关键一行：pdf.js 与 pymupdf 的 y 轴方向相反
    assert "pageHeightPt - y" in html, "少了 y 轴翻转，高亮会上下镜像"


def test_writing_html_requests_claim_english_before_jump():
    """写作页点「读原文」要先请求中文句回译，再拿英文去定位。"""
    html = (WEB_DIR / "writing.html").read_text(encoding="utf-8")
    assert "/writing/claim-english" in html, "没接回译接口，中文句拿去英文 PDF 里搜必然 0 命中"
    assert "/claim-locate" in html
    assert "openLocalPaper" in html
