"""从论文原文自动派生测试题的真值页码，生成可重复的评测集。

为什么要这样：
    "预期页码"如果靠我记忆写，就会像之前那样把 β2 记成 0.999（原文是 0.98）。
    这里反过来做：先选"能在原文里唯一定位的答案短语"，再让程序去找它出现在第几页。
    只有"该短语只出现在唯一一页"的题目才会被收进测试集 —— 真值可复现、可反驳。

用法：
    python scripts/build_eval_set.py              # 生成 data/eval/qa_v7.json
    python scripts/build_eval_set.py --show-rejected
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = Path(__file__).resolve().parent.parent
PAPER = BASE / "data" / "papers" / "1706.03762v7.pdf"
OUT_DIR = BASE / "data" / "eval"
OUT_FILE = OUT_DIR / "qa_v7.json"

# (问题, 判定用的原文短语)。短语必须是原文里逐字出现的，才可能定位到唯一页。
CANDIDATES = [
    ("论文训练用了多少块 GPU？", "8 NVIDIA P100 GPUs"),
    ("Adam 优化器的 β1 设为多少？", "β1 = 0.9"),
    ("Adam 优化器的 β2 设为多少？", "β2 = 0.98"),
    ("Adam 的 epsilon 设为多少？", "ϵ = 10−9"),
    ("base 模型的层数 N 是多少？", "N = 6"),
    ("d_model 的取值是多少？", "dmodel = 512"),
    ("注意力头数 h 是多少？", "h = 8"),
    ("训练时 dropout 率 Pdrop 是多少？", "Pdrop = 0.1"),
    ("label smoothing 的取值是多少？", "ϵls = 0.1"),
    ("warmup 步数是多少？", "warmup_steps = 4000"),
    ("base 模型训练了多少步？", "100,000 steps"),
    ("big 模型训练了多少步？", "300,000 steps"),
    ("big 模型训练了多久？", "3.5 days"),
    ("每一步训练耗时多久？", "1.0 second"),
    ("WMT 2014 英德任务 big 模型 BLEU 是多少？", "28.4"),
    ("用的优化器是什么？", "Adam optimizer"),
    ("训练用了多少台机器？", "one machine"),
    ("big 模型的参数量是多少？", "213 million"),
    ("base 模型的参数量是多少？", "65 million"),
    ("attention 的 key 维度 dk 是多少？", "dk = 64"),
    ("attention 的 value 维度 dv 是多少？", "dv = 64"),
    ("feed-forward 的维度 dff 是多少？", "dff = 2048"),
]


def page_texts() -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(PAPER))
    return [(page.extract_text() or "") for page in reader.pages]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-rejected", action="store_true")
    args = parser.parse_args()

    texts = page_texts()
    accepted, rejected = [], []
    seen: set[tuple[int, str]] = set()
    for question, phrase in CANDIDATES:
        pages = [i + 1 for i, text in enumerate(texts) if phrase in text]
        if len(pages) == 1:
            key = (pages[0], phrase)
            if key in seen:      # 同一页的同一个短语只收一次，避免重复计分
                continue
            seen.add(key)
            accepted.append({"q": question, "pages": pages, "marker": phrase})
        else:
            rejected.append({"q": question, "marker": phrase, "pages": pages})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {"paper": PAPER.name, "items": accepted, "rejected": rejected},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"论文: {PAPER.name} | 页数 {len(texts)}")
    print(f"收录 {len(accepted)} 题（短语在原文中只出现于一页）→ {OUT_FILE}")
    for item in accepted:
        print(f"  [p{item['pages'][0]:>2}] {item['q']}  ← “{item['marker']}”")
    print(f"淘汰 {len(rejected)} 题（短语出现在多页或找不到）")
    if args.show_rejected:
        for item in rejected:
            print(f"  [p{item['pages']}] {item['q']}  ← “{item['marker']}”")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
