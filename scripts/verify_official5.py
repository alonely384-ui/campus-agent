r"""官方 5 问端到端验收：打真实 /chat 接口，把回答、引用、证据页码落成一张表。

用法（服务需已启动在 127.0.0.1:8000）：
    .\.venv\Scripts\python.exe scripts\verify_official5.py
    .\.venv\Scripts\python.exe scripts\verify_official5.py --source 2005.14165v4.pdf
"""

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

CASE_SOURCE = "1706.03762v7.pdf"

# (题号, 问题, 官方期望页码, 人工判据)
CASES = [
    ("Q1", "这篇论文的第六作者是谁？他/她属于哪个单位？", 1, "Aidan N. Gomez / University of Toronto"),
    ("Q2", "论文里 Adam 优化器的 beta1 和 beta2 分别取多少？", 7, "β1 = 0.9, β2 = 0.98, ε = 1e-9（原文 p7）"),
    ("Q3", "训练用了多少块 GPU、什么型号？训练了多久？", 7, "8 × NVIDIA P100，3.5 天"),
    ("Q4", "本文在 WMT 2014 英德翻译上达到多少 BLEU？比之前最好结果高多少？", 8, "28.4，比之前最好高 2.0 以上"),
    ("Q5", "LayerNorm 的原始参考文献是哪一篇？", 10, "Ba, Kiros & Hinton (2016) Layer Normalization"),
]

CITE_RE = re.compile(r"\[来源[:：]\s*([^\]\s]+)\s*第?(\d+)?\s*页?\]")


def ask(client: httpx.Client, question: str, source: str) -> dict:
    resp = client.post(
        "/chat",
        json={"message": question, "history": [], "source": source},
        timeout=180.0,
    )
    resp.raise_for_status()
    return resp.json()


def check_citation(answer: str, evidence: list[dict]) -> tuple[str, str]:
    """返回 (判据, 说明)。判据：一致 / 无引用 / 无页码 / 页码不在证据里。"""
    cites = CITE_RE.findall(answer)
    if not cites:
        return "无引用", "回答里没有 [来源: … 第N页]"
    pages = {int(m.group(2)) for m in CITE_RE.finditer(answer) if m.group(2)}
    ev_pages = {e["page"] for e in evidence}
    if not pages:
        return "无页码", f"引用缺页码：{cites[:2]}"
    if pages & ev_pages:
        return "一致", f"引用页码 {sorted(pages)} 在证据里"
    return "页码不在证据里", f"引用 {sorted(pages)}，证据 {sorted(ev_pages)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=CASE_SOURCE)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default=r"docs\acceptance-official5.md")
    args = parser.parse_args()

    rows = []
    # trust_env=False：环境里 NO_PROXY 含 "[::1]"，httpx 会把它当 URL 解析并报 Invalid port
    with httpx.Client(base_url=args.base, trust_env=False) as client:
        health = client.get("/health", timeout=15).json()
        print(f"服务 {args.base} 健康：{health}")
        for cid, question, expect_page, judge in CASES:
            data = ask(client, question, args.source)
            answer = (data.get("answer") or "").strip()
            evidence = data.get("evidence") or []
            verdict, note = check_citation(answer, evidence)
            rows.append(
                {
                    "id": cid,
                    "question": question,
                    "answer": answer,
                    "expect_page": expect_page,
                    "judge": judge,
                    "evidence": evidence,
                    "verdict": verdict,
                    "note": note,
                }
            )
            print(f"\n===== {cid} =====")
            print("问：", question)
            print("答：", answer)
            print("证据：", [(e["rank"], e["source"], e["page"], round(e["score"], 4)) for e in evidence])
            print("引用判据：", verdict, "|", note)
            hit = any(e["page"] == expect_page for e in evidence)
            print("期望页", expect_page, "是否在证据里：", "是" if hit else "否")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 官方 5 问端到端验收",
        "",
        f"- 数据源：`data/papers/{args.source}`（单篇锁定）",
        "- 判定口径：期望页出现在 evidence（检索正确）+ 回答引用页码与 evidence 交集非空（引用合规）",
        "",
        "| 题号 | 问题 | 回答（截断） | 期望页 | 证据页 | 检索命中 | 引用判据 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ev_pages = sorted({e["page"] for e in r["evidence"]})
        hit = "✅" if r["expect_page"] in ev_pages else "❌"
        answer = r["answer"].replace("\n", " ")[:160].replace("|", "\\|")
        lines.append(
            f"| {r['id']} | {r['question']} | {answer} | p{r['expect_page']} | {ev_pages} | {hit} | {r['verdict']} |"
        )
    lines += ["", "## 人工判据（答案是否等于原文事实）", ""]
    for r in rows:
        lines += [
            f"### {r['id']}",
            "",
            f"- 期望事实：{r['judge']}",
            f"- 实际回答：{r['answer']}",
            f"- 引用判据：{r['verdict']}（{r['note']}）",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已写：{out.resolve()}")

    hits = sum(1 for r in rows if any(e["page"] == r["expect_page"] for e in r["evidence"]))
    cites_ok = sum(1 for r in rows if r["verdict"] == "一致")
    print(f"汇总：检索命中页 {hits}/5，引用合规 {cites_ok}/5")
    return 0 if hits == 5 and cites_ok == 5 else 1


if __name__ == "__main__":
    sys.exit(main())
