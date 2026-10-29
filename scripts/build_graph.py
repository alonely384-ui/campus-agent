"""构建引用图谱并落盘（运行期命令，不参与检索索引）。

用法：
    .\.venv\Scripts\python.exe scripts\build_graph.py
    .\.venv\Scripts\python.exe scripts\build_graph.py --fuzzy-accept 0.90 --out data/meta/citation-graph.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import graph  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fuzzy-accept", type=float, default=graph.FUZZY_ACCEPT)
    parser.add_argument("--fuzzy-review", type=float, default=graph.FUZZY_REVIEW)
    parser.add_argument("--out", default=str(graph.CACHE_FILE))
    args = parser.parse_args()

    begin = time.time()
    data = graph.build_graph(fuzzy_accept=args.fuzzy_accept, fuzzy_review=args.fuzzy_review)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    s = data["summary"]
    print(f"耗时 {time.time() - begin:.1f}s -> {out}")
    print(
        "文件 {file_count} -> 逻辑节点 {node_count}（并掉 {merged_away} 个副本）"
        " / 引用边 {edge_count} / 被引论文 {cited_paper_count} / 引用论文 {citing_paper_count}"
        " / 孤立 {isolated_count}".format(**s)
    )
    print("多副本节点：")
    for n in data["nodes"]:
        if n["copy_count"] > 1:
            print(f"  {n['canonical_id']:22s} x{n['copy_count']}  {n['title'][:50]}  {n['copies']}")
    print("匹配方式统计：", json.dumps(s["refs"], ensure_ascii=False))
    print(f"待人工核验：{len(data['reviews'])} 条")
    for r in data["reviews"][:10]:
        print(f"  {r['source']} ~ {r['candidate']}  {r['match_score']}  「{r['reference_title'][:60]}」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())