"""structures.json 存放位置的测试（1.3.2 的遗留问题 ①）。

背景：原来结构文件写在 data/papers/ 里，而那个目录被 .gitignore 排除，
换台机器就没有章节树了。现在默认写 data/meta/structures.json（可提交），
同时兼容老环境里还留在 data/papers/ 的旧文件。
"""

import json

from app import papers


def test_default_structures_file_is_outside_gitignored_papers_dir():
    """默认路径必须在 data/meta 下，不能再落回 data/papers。"""
    default = papers.DEFAULT_STRUCTURES_FILE
    assert default.parent.name == "meta"
    assert default.parent.parent.name == "data"
    assert "papers" not in default.parts, "又写回被 gitignore 的目录了"


def test_structures_file_prefers_configured_path(monkeypatch, tmp_path):
    """STRUCTURES_FILE 被显式指定（测试里常见）时就用它，不去猜旧文件。"""
    target = tmp_path / "custom" / "structures.json"
    monkeypatch.setattr(papers, "STRUCTURES_FILE", target)
    assert papers.structures_file() == target


def test_structures_file_falls_back_to_legacy(monkeypatch, tmp_path):
    """老环境：新路径还没有、旧路径有文件 → 读旧的（不丢已有数据）。"""
    meta = tmp_path / "meta" / "structures.json"
    legacy = tmp_path / "papers" / "structures.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(papers, "STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "DEFAULT_STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "LEGACY_STRUCTURES_FILE", legacy)

    assert papers.structures_file() == legacy


def test_structures_file_uses_default_when_nothing_exists(monkeypatch, tmp_path):
    """全新环境：两个路径都没有 → 返回新默认路径，供后续写入。"""
    meta = tmp_path / "meta" / "structures.json"
    legacy = tmp_path / "papers" / "structures.json"

    monkeypatch.setattr(papers, "STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "DEFAULT_STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "LEGACY_STRUCTURES_FILE", legacy)

    assert papers.structures_file() == meta


def test_save_structures_migrates_to_meta_dir(monkeypatch, tmp_path):
    """老环境第一次 save 要写到新位置（顺手迁移），不能继续写 data/papers。"""
    meta = tmp_path / "meta" / "structures.json"
    legacy = tmp_path / "papers" / "structures.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(papers, "STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "DEFAULT_STRUCTURES_FILE", meta)
    monkeypatch.setattr(papers, "LEGACY_STRUCTURES_FILE", legacy)

    written = papers.save_structures({"a.pdf": {"title": "A"}})
    assert written == meta
    assert meta.is_file(), "没有自动建目录并写入新位置"
    assert json.loads(meta.read_text(encoding="utf-8"))["a.pdf"]["title"] == "A"
    # 旧文件保持原样，不做删除（宁可多留一份，也不冒丢数据的险）
    assert legacy.read_text(encoding="utf-8") == "{}"
