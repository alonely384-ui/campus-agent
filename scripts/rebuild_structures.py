"""重建 data/meta/structures.json —— 之前没有这个脚本，结构与代码早就对不上了。

为什么必须有：structure.py 改完之后，库里存的结构还是老代码跑出来的。
实测漂移很清楚——GPT-4 那份现存 135 条参考文献、重解析是 193 条；
扫描件现存 27 个章节、重解析是 0 条（老代码留下的残留）。
不重建就等于"代码修好了、页面上还是旧数据"。

用法：
    .venv\\Scripts\\python.exe scripts/rebuild_structures.py            # 全部重建
    .venv\\Scripts\\python.exe scripts/rebuild_structures.py --dry-run  # 只比数，不写盘
    .venv\\Scripts\\python.exe scripts/rebuild_structures.py --only 1706.03762v1.pdf

写了什么：
    1. 逐篇调用 parse_structure（整篇读，不再只看前 25 页）；
    2. 把图表图片渲染到 data/meta/figures/<stem>_figN.png；
    3. **整体替换** structures.json（保留原有的、本次没扫到的条目，避免误删）；
    4. 打印逐项前后对比，方便核对。

注意：这个脚本耗时较长（全库约 20 分钟，最大的 541 页书单篇 12 秒），
建议在离线时跑，跑完重启服务让新结构生效。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.structure import parse_structure  # noqa: E402

PAPERS_DIR = ROOT / "data" / "papers"
STRUCT_PATH = ROOT / "data" / "meta" / "structures.json"
FIGURE_DIR = ROOT / "data" / "meta" / "figures"

COUNT_KEYS = ("sections", "tables", "figures", "formulas")


def _counts(s: dict) -> dict[str, int]:
    out = {k: len(s.get(k) or []) for k in COUNT_KEYS}
    out["tables_with_rows"] = sum(1 for t in (s.get("tables") or []) if t.get("rows"))
    out["appendix"] = len(s.get("appendix") or [])
    out["authors"] = len(s.get("authors") or [])
    out["author_affil"] = len(s.get("author_affiliations") or {})
    refs = s.get("references") or {}
    out["references"] = len(refs.get("items") or []) if isinstance(refs, dict) else 0
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 structures.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印对比，不写盘")
    ap.add_argument("--only", default="", help="只重建指定文件（可逗号分隔）")
    ap.add_argument("--no-figures", action="store_true", help="不导出图表图片")
    args = ap.parse_args()

    old: dict = {}
    if STRUCT_PATH.exists():
        old = json.loads(STRUCT_PATH.read_text(encoding="utf-8"))
    print(f"现有 {len(old)} 条结构记录；data/papers 下 {len(list(PAPERS_DIR.glob('*.pdf')))} 个 PDF")

    targets = sorted(PAPERS_DIR.glob("*.pdf"))
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        targets = [p for p in targets if p.name in want]
    if not targets:
        print("没有要处理的 PDF")
        return 1

    new = dict(old)
    t0 = time.time()
    for i, pdf in enumerate(targets, 1):
        t1 = time.time()
        try:
            fresh = parse_structure(
                str(pdf),
                figure_dir="" if args.no_figures else str(FIGURE_DIR),
            )
        except Exception as exc:  # noqa: BLE001 - 单篇失败要如实报，不能整批中断
            print(f"[{i}/{len(targets)}] {pdf.name}: 解析失败 {exc}")
            continue
        dt = time.time() - t1
        before = _counts(old.get(pdf.name) or {})
        after = _counts(fresh)
        new[pdf.name] = fresh
        delta = "  ".join(
            f"{k} {before[k]}→{after[k]}" for k in COUNT_KEYS if before[k] != after[k]
        )
        print(f"[{i}/{len(targets)}] {pdf.name:<42} {dt:6.1f}s  {delta or '无变化'}")

    if args.dry_run:
        print(f"\n--dry-run：未写盘。总耗时 {time.time() - t0:.0f}s")
        return 0

    STRUCT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STRUCT_PATH.exists() and not args.only:
        backup = STRUCT_PATH.with_suffix(".json.bak")
        backup.write_bytes(STRUCT_PATH.read_bytes())
        print(f"已备份原文件到 {backup.name}")
    STRUCT_PATH.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {STRUCT_PATH}（{len(new)} 条），总耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())