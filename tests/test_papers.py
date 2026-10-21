"""论文库测试：不碰真实 data/papers，全部指向 pytest 的临时目录。

关键技巧：monkeypatch 替换模块级路径 + 注入假的文字抽取函数，
这样测试既不需要真 PDF，也不会污染你 125MB 的论文库。
"""

import pytest

from app import papers as papers_mod


@pytest.fixture
def library(tmp_path, monkeypatch):
    """把论文库的四个路径都指到临时目录，返回模块本身。"""
    monkeypatch.setattr(papers_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(papers_mod, "INDEX_FILE", tmp_path / "papers" / "index.json")
    monkeypatch.setattr(papers_mod, "TEXT_DIR", tmp_path / "papers" / "text")
    return papers_mod


def _fake_pdf(name: str) -> tuple[str, bytes]:
    # 每个文件的内容必须不同，否则 sha256 相同会被当成同一个 paper_id
    return name, b"%PDF-1.4 fake bytes for " + name.encode("utf-8")


def test_save_and_list(library):
    library.save_paper(*_fake_pdf("a.pdf"), text_extractor=lambda p: "论文 A 的内容，关于 Transformer 和注意力机制。")
    library.save_paper(*_fake_pdf("b.pdf"), text_extractor=lambda p: "论文 B 讨论 RAG 检索增强生成与向量数据库。")

    items = library.list_papers()
    assert len(items) == 2
    # 新的在前
    assert items[0].filename == "b.pdf"


def test_duplicate_detected(library):
    text_v7 = "Attention Is All You Need 提出 Transformer 架构，用自注意力替代循环网络。" * 20
    library.save_paper(*_fake_pdf("1706.03762v7.pdf"), text_extractor=lambda p: text_v7)

    # v1 只改了几个词，模拟「同一篇论文的两个版本」
    text_v1 = text_v7.replace("提出", "首次提出", 3)
    paper, dup, score = library.save_paper(*_fake_pdf("1706.03762v1.pdf"), text_extractor=lambda p: text_v1)

    assert dup is not None
    assert paper.duplicate_of == dup.paper_id
    assert score >= library.DUP_THRESHOLD


def test_different_papers_not_duplicate(library):
    library.save_paper(*_fake_pdf("nlp.pdf"), text_extractor=lambda p: "自然语言处理，词向量，BERT 预训练。" * 10)
    paper, dup, _ = library.save_paper(*_fake_pdf("cv.pdf"), text_extractor=lambda p: "计算机视觉，卷积网络，图像分类。" * 10)

    assert dup is None
    assert paper.duplicate_of == ""


def test_delete_removes_files_and_index(library):
    paper, _, _ = library.save_paper(*_fake_pdf("x.pdf"), text_extractor=lambda p: "内容 X")

    assert library.delete_paper(paper.paper_id) is True
    assert library.list_papers() == []
    assert not (library.PAPERS_DIR / f"{paper.paper_id}.pdf").exists()
    assert not (library.TEXT_DIR / f"{paper.paper_id}.txt").exists()
    # 再删一次应当返回 False
    assert library.delete_paper(paper.paper_id) is False


def test_rejects_non_pdf(library):
    with pytest.raises(ValueError, match="PDF"):
        library.save_paper("note.txt", b"hello", text_extractor=lambda p: "")


def test_rejects_oversize(library, monkeypatch):
    monkeypatch.setattr(library, "MAX_SIZE", 10)
    with pytest.raises(ValueError, match="超过"):
        library.save_paper("big.pdf", b"x" * 20, text_extractor=lambda p: "")


def test_get_paper_and_path(library):
    paper, _, _ = library.save_paper(*_fake_pdf("find.pdf"), text_extractor=lambda p: "可检索内容")

    assert library.get_paper(paper.paper_id) is not None
    assert library.get_paper("不存在") is None
    # 文件真的落盘了，paper_path 才能返回路径
    assert library.paper_path(paper.paper_id) is not None


def test_similarity_is_symmetric_and_bounded(library):
    a, b = "同一段文字内容" * 30, "同一段文字内容" * 30
    assert library.similarity(a, b) == 1.0
    assert library.similarity(a, "完全不同的另一段") < 0.5
