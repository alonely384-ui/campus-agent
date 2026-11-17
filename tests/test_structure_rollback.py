"""结构总表（structures.json）在保存失败时的回滚（2026-11-18，审计第二轮缺陷二）。

修之前：`save_paper` 里 `structure_merged` 初始化成 False 之后**从来没被更新过**
（`merge_structure()` 的返回值没接），`_rollback_saved` 里 `if structure_merged:`
是死分支。后果是——总表写成功、索引写失败时，PDF 和旁挂 JSON 都被删了，
`data/meta/structures.json` 里那条却留了下来，指向一个不存在的文件。
阅读页、`/paper/{f}/structure` 都按文件名查这张表，于是会显示一个点开就 404 的条目。

这里按四条边界钉住新行为：
1. 合并成功 → 总表里有这一篇，且**别的论文一条不少**；
2. 合并之前就失败（解析器抛错）→ 总表从头到尾没被碰过；
3. 合并之后索引保存失败 → 总表必须回到操作前的样子（这篇的键消失）；
4. 回滚后的总表与操作前**逐键相等**，其他论文的值不能被改坏。
"""

from __future__ import annotations

import copy
import json

import pytest

from app import papers as papers_mod

# ------------------------------------------------------------------ 样本构造


def _pdf_with_objects(objects: list[bytes], header: bytes = b"%PDF-1.4\n") -> bytes:
    """拼一个结构完整、pypdf 能打开的最小 PDF。"""
    out = bytearray(header)
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _valid_pdf(marker: str = "hello") -> bytes:
    """一页、带文字层的正常 PDF（页数 = 1）。"""
    content = f"BT /F1 12 Tf 72 700 Td ({marker}) Tj ET".encode()
    return _pdf_with_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


# ------------------------------------------------------------------ fixture


@pytest.fixture()
def library(tmp_path, monkeypatch):
    """把论文库四个路径指到 tmp。

    和 `test_paper_upload_guard.py` 里的同名 fixture 有一处刻意不同：
    **不**把 `merge_structure` 打成桩——这批用例要验的就是总表本身。
    """
    monkeypatch.setattr(papers_mod, "PAPERS_DIR", tmp_path / "papers")
    monkeypatch.setattr(papers_mod, "INDEX_FILE", tmp_path / "papers" / "index.json")
    monkeypatch.setattr(papers_mod, "TEXT_DIR", tmp_path / "papers" / "text")
    monkeypatch.setattr(
        papers_mod, "STRUCTURES_FILE", tmp_path / "papers" / "structures.json"
    )
    papers_mod.PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    return papers_mod


def _seed_structure(papers_mod, filename: str, title: str) -> dict:
    """往总表里预置一篇"别的论文"，用来验证回滚不会误伤它。"""
    data = papers_mod.load_structures()
    entry = {"title": title, "authors": ["Someone"], "sections": [{"title": "Intro"}]}
    data[filename] = entry
    papers_mod.save_structures(data)
    return entry


def _snapshot(papers_mod) -> dict:
    """当前总表的深拷贝，用于比较"回滚后是否和操作前一致"。"""
    return copy.deepcopy(papers_mod.load_structures())


def _parser(title: str):
    """假的章节解析器：返回一份够最小结构，不真的去打开 PDF。"""

    def _parse(_path: str) -> dict:
        return {"title": title, "authors": ["Tester"], "sections": [{"title": "Abstract"}]}

    return _parse


# ------------------------------------------------------------------ 1. 合并成功


def test_merge_success_keeps_other_papers(library):
    """正常上传：总表里有新上传这篇，同时别的论文一条不少。"""
    other = _seed_structure(library, "old.pdf", "Other Paper")
    before = _snapshot(library)

    paper, _, _ = papers_mod.save_paper(
        "new.pdf", _valid_pdf("new body"), structure_parser=_parser("New Paper")
    )

    after = library.load_structures()
    assert "new.pdf" in after, "合并成功后总表里应该有新上传的论文"
    assert after["new.pdf"]["title"] == "New Paper"
    assert after["old.pdf"] == other, "别的论文记录必须原样保留"
    assert set(after) == set(before) | {"new.pdf"}
    # 索引也写下去了
    assert any(p.paper_id == paper.paper_id for p in library._load_index())


# ------------------------------------------------------------------ 2. 合并前失败


def test_failure_before_merge_leaves_table_untouched(library):
    """解析器就抛错：总表从头到尾没被碰过，其余文件也已清理。"""
    _seed_structure(library, "old.pdf", "Other Paper")
    before = _snapshot(library)

    def _boom(_path: str) -> dict:
        raise RuntimeError("解析炸了")

    # 解析失败在 save_paper 里是「记日志、跳过」的附加信息，不算整体失败
    paper, _, _ = papers_mod.save_paper(
        "half.pdf", _valid_pdf("half body"), structure_parser=_boom
    )

    assert library.load_structures() == before, "没合并过，总表必须一字节不变"
    assert "half.pdf" not in library.load_structures()
    assert paper.paper_id, "解析失败不影响主流程：论文本身应当正常保存"


# ------------------------------------------------------------------ 3. 合并后索引保存失败


def test_merge_then_index_failure_restores_table(library, monkeypatch):
    """总表写成功、索引写失败 → 总表必须回到操作前的样子。"""
    other = _seed_structure(library, "old.pdf", "Other Paper")
    before = _snapshot(library)

    def _boom(_papers):
        raise OSError("索引写盘失败")

    monkeypatch.setattr(library, "_save_index", _boom)

    with pytest.raises(OSError):
        papers_mod.save_paper(
            "new.pdf", _valid_pdf("new body"), structure_parser=_parser("New Paper")
        )

    after = library.load_structures()
    assert "new.pdf" not in after, "回滚后总表里不能留下指向已删文件的幽灵条目"
    assert after == before, "总表必须逐键回到操作前"
    assert after["old.pdf"] == other, "别的论文记录不能被误删或改坏"


# ------------------------------------------------------------------ 4. 逐键相等 + 其他论文完整


def test_rollback_restores_exact_snapshot_including_other_keys(library, monkeypatch):
    """多篇既有论文 + 回滚 → 每一篇的值都与操作前完全相同（不只是键集合相同）。"""
    _seed_structure(library, "a.pdf", "Paper A")
    _seed_structure(library, "b.pdf", "Paper B")
    _seed_structure(library, "c.pdf", "Paper C")
    before = _snapshot(library)
    assert len(before) == 3

    monkeypatch.setattr(
        library, "_save_index", lambda _papers: (_ for _ in ()).throw(OSError("boom"))
    )

    with pytest.raises(OSError):
        papers_mod.save_paper(
            "d.pdf", _valid_pdf("d body"), structure_parser=_parser("Paper D")
        )

    after = library.load_structures()
    assert after == before
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)


# ------------------------------------------------------------------ 5. 磁盘残留


def test_rollback_leaves_no_orphan_files(library, monkeypatch):
    """回滚之后：PDF、文字缓存、旁挂 JSON 都不该留在盘上。"""
    monkeypatch.setattr(
        library, "_save_index", lambda _papers: (_ for _ in ()).throw(OSError("boom"))
    )

    with pytest.raises(OSError):
        papers_mod.save_paper(
            "ghost.pdf", _valid_pdf("ghost body"), structure_parser=_parser("Ghost")
        )

    leftover = [p.name for p in library.PAPERS_DIR.glob("*.pdf")]
    assert leftover == [], f"回滚后不该留下 PDF：{leftover}"
    assert not list(library.TEXT_DIR.glob("*.txt"))
    assert not list(library.PAPERS_DIR.glob("*.structure.json"))