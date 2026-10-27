"""重算 data/papers/structures.json 里的「章节树」，补出二级/三级标题。

为什么需要它：赛题 1.3.2 第 1 条要求「一级和二级标题做成树状导航，能跳 3.2.2
Multi-Head Attention 这种小节」，但旧的 `_sections()` 只认「字号比正文大」的标题行；
很多论文（Attention、LLaMA 2、LayerNorm…）的小节标题是和正文同字号、单独一行的加粗
编号（"3.2.2" 换行 "Multi-Head Attention"），于是 28/39 篇只剩一级标题。

做法：
  1. 备份 structures.json → structures.json.bak（只备份一次，不覆盖已有备份）；
  2. 逐篇用 app.structure 的 `_lines/_body_size/_sections` 重算 sections 和 appendix，
     其余字段（title/authors/tables/references…）一个都不动；
  3. 默认只处理「现有 sections 里没有二级标题」的论文，--all 才全量重算；
  4. 写回前 json.load 自检，写完打印「篇数 / 二级标题总数 / 三级总数」并再解析一次。

用法：
    python scripts/refresh_sections.py            # 只补缺二级标题的论文
    python scripts/refresh_sections.py --all      # 全部重算
    python scripts/refresh_sections.py --dry-run  # 只看会改什么，不写文件
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pymupdf  # noqa: E402

from app.papers import PAPERS_DIR, load_structures, save_structures, structures_file  # noqa: E402
from app.structure import _appendix, _body_size, _lines, _sections  # noqa: E402

MAX_PAGES = 25      # 正文/目录的"真正文"判断沿用 parse_structure 的 25 页
MAX_SCAN_PAGES = 200   # 章节树往后多扫一些：GPT-3 有 75 页，只扫 25 页会丢掉第 4 章之后


def sections_of(pdf_path: Path) -> list[dict]:
    """重算章节树（不碰表格/作者等重活）。

    字号基线只从前面 MAX_PAGES 页取样（后面都是正文，不会更准），
    但章节本身扫到 MAX_SCAN_PAGES 页，否则长论文的章节树会在中间断掉。
    """
    doc = pymupdf.open(pdf_path)
    try:
        pages = min(len(doc), MAX_SCAN_PAGES)
        body_head: list[dict] = []
        for pno in range(min(MAX_PAGES, pages)):
            body_head += _lines(doc[pno], pno)
        body = _body_size(body_head)
        head = body_head[:]
        for pno in range(min(MAX_PAGES, pages), pages):
            head += _lines(doc[pno], pno)
        return _sections(head, body)
    finally:
        doc.close()


def level_counts(sections: list[dict]) -> dict[int, int]:
    out: dict[int, int] = {}
    for s in sections:
        lv = int(s.get("level") or 1)
        out[lv] = out.get(lv, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="重算 structures.json 的章节树")
    ap.add_argument("--all", action="store_true", help="全部论文重算（默认只补缺二级标题的）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    data = load_structures()
    if not data:
        print("structures.json 不存在或为空，先跑 scripts/build_structure.py")
        return 1
    targets = []
    for name, meta in data.items():
        secs = meta.get("sections") or []
        if args.all or level_counts(secs).get(2, 0) == 0:
            targets.append(name)

    print(f"structures.json: {len(data)} 篇；待重算 {len(targets)} 篇（--all={args.all}）")

    changed = 0
    for name in targets:
        pdf = PAPERS_DIR / name
        if not pdf.is_file():
            print(f"  skip {name}（PDF 不在磁盘上）")
            continue
        old = data[name].get("sections") or []
        new = sections_of(pdf)
        old_lv, new_lv = level_counts(old), level_counts(new)
        if not new:
            print(f"  skip {name}（重算后仍没有章节）")
            continue
        if new_lv.get(2, 0) == 0 and old_lv.get(2, 0) == 0:
            print(f"  keep {name}（重算也没有二级标题，保持原样）")
            continue
        changed += 1
        print(f"  upd  {name}: {len(old)} 节 {old_lv} -> {len(new)} 节 {new_lv}")
        data[name]["sections"] = new
        # appendix 是从 sections 派生的，跟着一起更新
        data[name]["appendix"] = _appendix(new)

    if args.dry_run:
        print(f"[dry-run] 会改 {changed} 篇，未写文件")
        return 0

    target = structures_file()
    backup = target.with_suffix(".json.bak")
    if not backup.exists():
        shutil.copy2(target, backup)
        print(f"已备份 -> {backup.name}")
    else:
        print(f"备份已存在，保留原备份 -> {backup.name}")

    written = save_structures(data)

    # 写回后解析校验 + 回显统计，确认没有写坏
    again = json.loads(written.read_text(encoding="utf-8"))
    total2 = sum(level_counts(m.get("sections") or []).get(2, 0) for m in again.values())
    total3 = sum(level_counts(m.get("sections") or []).get(3, 0) for m in again.values())
    no2 = sum(1 for m in again.values() if level_counts(m.get("sections") or []).get(2, 0) == 0)
    print(f"校验：json.load OK，{len(again)} 篇；二级标题 {total2} 个，三级 {total3} 个；仍无二级标题 {no2} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
