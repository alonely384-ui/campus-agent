"""论文库一致性 + 多信号查重的测试。

三个被真实数据打穿过的问题，各写一条：
1. 样例 PDF 直接拷进 data/papers，从没走过上传接口 → index.json 里一条都没有，
   `GET /papers` 只回 1 条而磁盘上有 39 个文件；
2. 扫描件抽不出文字，纯文本相似度恒为 0 → 要把 arXiv 编号当第二个信号；
3. 查重要能说出"凭什么是重复"（文本相似 / 同编号不同版本），前端才好解释。
"""

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app import papers as papers_mod


@pytest.fixture
def library(tmp_path, monkeypatch):
    """把论文库路径指到临时目录，返回模块本身。"""
    monkeypatch.setattr(papers_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(papers_mod, "INDEX_FILE", tmp_path / "papers" / "index.json")
    monkeypatch.setattr(papers_mod, "TEXT_DIR", tmp_path / "papers" / "text")
    monkeypatch.setattr(papers_mod, "STRUCTURES_FILE", tmp_path / "papers" / "structures.json")
    papers_mod.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    return papers_mod


def _fake_pdf(name: str) -> tuple[str, bytes]:
    return name, b"%PDF-1.4 fake bytes for " + name.encode("utf-8")


def test_sync_index_picks_up_files_on_disk(library):
    """直接拷进目录、没经过上传接口的 PDF，也要出现在列表里。"""
    (library.PAPERS_DIR / "sample-a.pdf").write_bytes(b"%PDF-1.4 a")
    (library.PAPERS_DIR / "1706.03762v1.pdf").write_bytes(b"%PDF-1.4 v1")

    items = library.list_papers()

    assert {p.paper_id for p in items} == {"sample-a", "1706.03762v1"}
    assert all(p.size > 0 for p in items)
    # 索引文件写回了，第二次读不会重复登记
    assert len(library.list_papers()) == 2


def test_sync_index_drops_missing_files(library):
    """索引里有、磁盘上已经没有的条目要清掉，否则删除接口会"删不存在的东西"。"""
    paper, _, _ = library.save_paper(*_fake_pdf("gone.pdf"), text_extractor=lambda p: "x")
    (library.PAPERS_DIR / f"{paper.paper_id}.pdf").unlink()

    assert library.list_papers() == []


def test_arxiv_id_and_version_helpers(library):
    assert library.arxiv_id_of("1706.03762v1_img.pdf") == "1706.03762"
    assert library.arxiv_id_of("no-id-here.pdf") == ""
    assert library.arxiv_id_of("", "arXiv:2103.00020 [cs.CV]") == "2103.00020"
    assert library.version_of("1706.03762v7.pdf") == 7
    assert library.version_of("1706.03762v1_img.pdf") == 1
    assert library.version_of("plain.pdf") == 0


def test_duplicate_detected_by_arxiv_id_without_text(library):
    """扫描件抽不出一个字，也要靠编号认出它是同一篇的另一个版本。"""
    text_v7 = "Attention Is All You Need 提出 Transformer 架构。" * 20
    library.save_paper(*_fake_pdf("1706.03762v7.pdf"), text_extractor=lambda p: text_v7)

    paper, dup, score = library.save_paper(
        *_fake_pdf("1706.03762v1_img.pdf"), text_extractor=lambda p: ""
    )

    assert dup is not None
    assert dup.filename == "1706.03762v7.pdf"
    assert paper.duplicate_of == dup.paper_id
    assert paper.duplicate_reason == "同一 arXiv 编号的不同版本"
    assert score >= library.DUP_THRESHOLD


def test_text_duplicate_reason_is_text_similarity(library):
    """文字高度重合时，依据要写"文本相似度"，而不是编号。"""
    text = (
        "Attention Is All You Need 提出 Transformer 架构，用自注意力替代循环网络。"
        "编码器由六层相同的模块堆叠，每层包含多头注意力与前馈网络。"
        "解码器额外插入一层交叉注意力，用来读取编码器的输出。"
    ) * 12
    library.save_paper(*_fake_pdf("paper-v1.pdf"), text_extractor=lambda p: text)
    paper, dup, score = library.save_paper(
        *_fake_pdf("paper-v2.pdf"), text_extractor=lambda p: text.replace("提出", "首次提出", 2)
    )

    assert dup is not None
    assert paper.duplicate_reason == "文本相似度"
    assert score >= library.DUP_THRESHOLD


def test_batch_upload_reports_each_file(library, monkeypatch):
    """批量上传：逐个返回结果，单文件老接口字段仍然保留。"""
    monkeypatch.setattr(main_mod, "index_paper", lambda path, source: 3)
    client = TestClient(main_mod.app)

    resp = client.post(
        "/papers/upload",
        files=[
            ("files", ("batch-a.pdf", b"%PDF-1.4 a", "application/pdf")),
            ("files", ("batch-b.pdf", b"%PDF-1.4 b", "application/pdf")),
        ],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["saved"] == 2
    assert body["duplicates"] == 0
    assert {r["filename"] for r in body["results"]} == {"batch-a.pdf", "batch-b.pdf"}
    assert all(r["indexed"] == 3 for r in body["results"])


def test_batch_upload_flags_newer_version(library, monkeypatch):
    """重复且版本更高时，接口要明确说 newer=True，前端据此默认"保留新版"。"""
    monkeypatch.setattr(main_mod, "index_paper", lambda path, source: 1)
    client = TestClient(main_mod.app)

    client.post(
        "/papers/upload",
        files={"files": ("1706.03762v1.pdf", b"%PDF-1.4 v1", "application/pdf")},
    )
    resp = client.post(
        "/papers/upload",
        files={"files": ("1706.03762v7.pdf", b"%PDF-1.4 v7", "application/pdf")},
    )

    body = resp.json()
    assert body["duplicates"] == 1
    dup = body["duplicate"]
    assert dup["filename"] == "1706.03762v1.pdf"
    assert dup["reason"] == "同一 arXiv 编号的不同版本"
    assert dup["old_version"] == 1
    assert dup["new_version"] == 7
    assert dup["newer"] is True


def test_multi_upload_all_fail_returns_400(library, monkeypatch):
    """批量里一个都没成功时，不能假装成功。"""
    monkeypatch.setattr(main_mod, "index_paper", lambda path, source: 0)
    client = TestClient(main_mod.app)

    resp = client.post(
        "/papers/upload",
        files=[
            ("files", ("a.txt", b"not a pdf", "text/plain")),
            ("files", ("b.txt", b"not a pdf either", "text/plain")),
        ],
    )

    assert resp.status_code == 400
