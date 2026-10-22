"""为 data/papers 下所有 PDF 生成结构化元数据缓存（标题/作者/章节树）。

产物：data/papers/structures.json，形如
    {"1706.03762v7.pdf": {"title": "...", "authors": [...], "sections": [...], "pages": 15}}

用法（在 campus-agent 目录）：
    .\\.venv\\Scripts\\python.exe scripts\\build_structure.py
可反复执行；解析失败的论文只记空值，不影响其他篇。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.structure import parse_structure  # noqa: E402

PAPERS_DIR = Path("data/papers")
OUT = PAPERS_DIR / "structures.json"


def main() -> None:
    pdfs = sorted(PAPERS_DIR.glob("*.pdf"))
    print(f"论文数: {len(pdfs)}")
    out: dict[str, dict] = {}
    t0 = time.time()
    for i, pdf in enumerate(pdfs, 1):
        s = parse_structure(str(pdf))
        # 章节树只留前 60 条：足够答辩展示，又不至于把 JSON 撑大
        s["sections"] = s.get("sections", [])[:60]
        out[pdf.name] = s
        ok = "OK" if s.get("title") and s.get("authors") else "空"
        print(f"[{i}/{len(pdfs)}] {ok} {pdf.name}: {s.get('title','')[:50]} / {len(s.get('authors',[]))} 作者")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    got = sum(1 for v in out.values() if v.get("authors"))
    print(f"完成: {got}/{len(pdfs)} 篇抽到作者，耗时 {time.time()-t0:.0f}s -> {OUT}")


if __name__ == "__main__":
    main()
