"""给抽不出章节树的论文「借」一棵树：同一篇论文的扫描版/旧版，标题是一样的。

背景：`1706.03762v1_img.pdf` 这类扫描件没有文本层，正则再准也抽不到标题。
但只要库里还有同一篇论文的可解析版本（`1706.03762v7.pdf`），它的章节树就是
同一份目录——直接借过来，页码按捐赠者那份算（扫描版页码可能有偏移，属已知误差）。

用法（在 campus-agent 目录下）：
    .\\.venv\\Scripts\\python.exe scripts\\borrow_sections.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts\\borrow_sections.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.papers import load_structures, save_structures  # noqa: E402
from app.structure import _appendix  # noqa: E402

_WORD_RE = re.compile(r"[a-z0-9]+")
# arXiv 新版命名：1706.03762v7.pdf / 1706.03762v1_img.pdf 都是同一篇
_ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5})")


def norm_title(title: str) -> str:
    """标题归一化：只留字母数字，忽略大小写、标点、换行。"""
    return " ".join(_WORD_RE.findall((title or "").lower()))


def arxiv_key(filename: str) -> str:
    """文件名里的 arXiv 编号（1706.03762），没有就返回空串。"""
    m = _ARXIV_RE.match(filename)
    return m.group(1) if m else ""


def has_level2(sections: list[dict]) -> bool:
    return any(int(s.get("level") or 1) == 2 for s in sections)


def donor_index(data: dict[str, dict]) -> dict[str, str]:
    """归一化标题 -> 有二级标题的文件名。同标题多篇时取章节最多的一篇。"""
    best: dict[str, tuple[int, str]] = {}
    for name, meta in data.items():
        key = norm_title(str(meta.get("title") or ""))
        secs = meta.get("sections") or []
        if not key or len(key) < 8 or not has_level2(secs):
            continue
        prev = best.get(key)
        if prev is None or len(secs) > prev[0]:
            best[key] = (len(secs), name)
    return {k: v[1] for k, v in best.items()}


def donor_by_arxiv(data: dict[str, dict]) -> dict[str, str]:
    """arXiv 编号 -> 同编号下章节最多的文件名（扫描件常常连标题都抽不出来）。"""
    best: dict[str, tuple[int, str]] = {}
    for name, meta in data.items():
        key = arxiv_key(name)
        secs = meta.get("sections") or []
        if not key or not has_level2(secs):
            continue
        prev = best.get(key)
        if prev is None or len(secs) > prev[0]:
            best[key] = (len(secs), name)
    return {k: v[1] for k, v in best.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="同标题论文之间复用章节树（主要给扫描件用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    data = load_structures()
    if not data:
        print("structures.json 不存在或为空")
        return 1

    donors = donor_index(data)
    arxiv_donors = donor_by_arxiv(data)
    solved = 0
    for name, meta in sorted(data.items()):
        secs = meta.get("sections") or []
        if has_level2(secs):
            continue
        key = norm_title(str(meta.get("title") or ""))
        donor = donors.get(key) if key else None
        how = "同标题"
        if not donor:
            donor = arxiv_donors.get(arxiv_key(name))
            how = "同 arXiv 编号"
        if not donor or donor == name:
            print(f"  无同题可借 {name}（标题：{(meta.get('title') or '')[:50]}）")
            continue
        borrowed = [dict(s) for s in data[donor]["sections"]]
        meta["sections"] = borrowed
        meta["appendix"] = _appendix(borrowed)
        meta["sections_borrowed_from"] = donor
        solved += 1
        print(f"  borrow {name}: {len(borrowed)} 节 <- {donor}（{how}）")

    print(f"共借到 {solved} 篇")
    if args.dry_run or not solved:
        print("[dry-run] 未写文件" if args.dry_run else "没有改动，不写文件")
        return 0
    path = save_structures(data)
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
