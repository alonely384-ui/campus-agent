"""一条命令复现 1.1.2 整改的全部关键数字。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\check_112.py

它做三件事：
1. 用**现在的**代码重新解析一篇论文，和 structures.json 里存的对一遍 —— 查"数据与代码是否同步"；
2. 统计全库十一项的覆盖率 —— 查报告里那些数字是否属实；
3. 抽几篇把附录/作者-单位映射/图表图片摊开给人看 —— 查"是不是真的有用的东西，还是凑数"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STRUCT = ROOT / "data" / "meta" / "structures.json"
FIGDIR = ROOT / "data" / "meta" / "figures"

from app.structure import parse_structure  # noqa: E402


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "-"


def section_1_coverage(data: dict) -> None:
    print("=" * 62)
    print("① 全库覆盖率（分母 = structures.json 里的篇数）")
    print("=" * 62)
    n = len(data)
    print(f"总篇数 {n}；data/papers 下 PDF {len(list((ROOT / 'data' / 'papers').glob('*.pdf')))} 个")
    print()

    def has(fn) -> int:
        return sum(1 for s in data.values() if fn(s))

    rows = [
        ("标题", has(lambda s: bool((s.get("title") or "").strip()))),
        ("作者", has(lambda s: bool(s.get("authors")))),
        ("摘要", has(lambda s: bool((s.get("abstract") or "").strip()))),
        ("章节", has(lambda s: bool(s.get("sections")))),
        ("一级标题", has(lambda s: any(x.get("level") == 1 for x in (s.get("sections") or [])))),
        ("二三级标题", has(lambda s: any(x.get("level", 1) >= 2 for x in (s.get("sections") or [])))),
        ("表格", has(lambda s: bool(s.get("tables")))),
        ("图表", has(lambda s: bool(s.get("figures")))),
        ("公式", has(lambda s: bool(s.get("formulas")))),
        ("参考文献", has(lambda s: int((s.get("references") or {}).get("count") or 0) > 0)),
        ("附录", has(lambda s: bool(s.get("appendix")))),
        ("作者-单位映射", has(lambda s: bool(s.get("author_affiliations")))),
    ]
    for label, k in rows:
        print(f"  {label:<12} {k:>3}/{n}   {_pct(k, n):>6}")

    mail = sum(1 for s in data.values() for a in (s.get("affiliations") or []) if "@" in a)
    print(f"\n  单位列表里混进邮箱的条数：{mail}   （整改前是 61，必须为 0）")

    print()
    print("  明细（条目总数）：")
    sec = sum(len(s.get("sections") or []) for s in data.values())
    tab = sum(len(s.get("tables") or []) for s in data.values())
    trow = sum(1 for s in data.values() for t in (s.get("tables") or []) if t.get("rows"))
    fig = sum(len(s.get("figures") or []) for s in data.values())
    fimg = sum(1 for s in data.values() for f in (s.get("figures") or []) if f.get("image"))
    fml = sum(len(s.get("formulas") or []) for s in data.values())
    ref = sum(int((s.get("references") or {}).get("count") or 0) for s in data.values())
    app = sum(len(s.get("appendix") or []) for s in data.values())
    print(f"    章节 {sec} / 表格 {tab}（有单元格 {trow}）/ 图表 {fig}（有图片 {fimg}）")
    print(f"    公式 {fml} / 参考文献 {ref} / 附录 {app}")


def section_2_sync(data: dict) -> None:
    print()
    print("=" * 62)
    print("② 数据与代码是否同步（重解析一篇，和存的对比）")
    print("=" * 62)
    name = "2307.09288v2.pdf"
    pdf = ROOT / "data" / "papers" / name
    if not pdf.exists():
        print(f"  跳过：{name} 不在磁盘上")
        return
    print(f"  重新解析 {name} …（约 60 秒）")
    fresh = parse_structure(str(pdf))          # 注意：不传 figure_dir，不重复渲染图片
    stored = data.get(name) or {}
    for key in ("sections", "tables", "figures", "formulas", "appendix"):
        a = len(stored.get(key) or [])
        b = len(fresh.get(key) or [])
        flag = "OK" if a == b else "!! 不一致"
        print(f"    {key:<10} 存 {a:>4}  重算 {b:>4}   {flag}")
    ra = int((stored.get("references") or {}).get("count") or 0)
    rb = int((fresh.get("references") or {}).get("count") or 0)
    print(f"    {'refs':<10} 存 {ra:>4}  重算 {rb:>4}   {'OK' if ra == rb else '!! 不一致'}")


def section_3_samples(data: dict) -> None:
    print()
    print("=" * 62)
    print("③ 抽样看内容（不是只看数字）")
    print("=" * 62)

    print("\n  [附录] 2201.11903v6.pdf（整改前 0 条，现在应该是 21 条）")
    for a in (data.get("2201.11903v6.pdf", {}).get("appendix") or [])[:6]:
        print(f"    p{a['page']:<4} {a['number']:<8} {a['title'][:46]}")

    print("\n  [作者-单位] 1706.03762v1.pdf（每行是「作者 → 单位/邮箱」）")
    aa = data.get("1706.03762v1.pdf", {}).get("author_affiliations") or {}
    for k, v in list(aa.items())[:4]:
        print(f"    {k:<20} {v}")

    print("\n  [图表图片] Attention 论文导出到磁盘的那几张")
    figs = [f for f in (data.get("1706.03762v1.pdf", {}).get("figures") or []) if f.get("image")]
    for f in figs:
        raw = Path(f["image"])
        p = raw if raw.is_absolute() else FIGDIR / raw.name
        exists = p.exists()
        size = p.stat().st_size if exists else 0
        print(f"    {p.name:<26} {size:>7} 字节  {'存在' if exists else '缺失 !!'}")
    missing = [
        f.get("image")
        for s in data.values()
        for f in (s.get("figures") or [])
        if f.get("image") and not (FIGDIR / Path(f["image"]).name).exists()
    ]
    print(f"    全库图片缺失：{len(missing)} 张（应为 0）")

    print("\n  [公式] Attention 论文的 9 条（应该都是真题，没有 'r = 1' 这种）")
    for f in (data.get("1706.03762v1.pdf", {}).get("formulas") or []):
        t = f.get("text") if isinstance(f, dict) else f
        print(f"    p{f.get('page') if isinstance(f, dict) else '?':<3} {t[:60]}")


def main() -> int:
    if not STRUCT.exists():
        print(f"找不到 {STRUCT}")
        return 1
    data = json.loads(STRUCT.read_text(encoding="utf-8"))
    section_1_coverage(data)
    section_2_sync(data)
    section_3_samples(data)
    print()
    print("=" * 62)
    print("④ 门禁（另开命令跑）")
    print("=" * 62)
    print(r"  .venv\Scripts\python.exe -m pytest tests -q")
    print(r"  .venv\Scripts\python.exe -m ruff check .")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())