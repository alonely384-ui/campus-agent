"""评测脚本：算出"检索命中率"这个能写进简历的数字。

为什么一定要做评测？
    因为面试官会问"你怎么知道你的方案更好？"
    没有评测集，你只能答"感觉好用了" —— 这句话在面试里等于零分。
    有了评测集，你可以说"我把 chunk size 从 500 改到 800，命中率从 72% 提升到 88%"。

用法：
    python eval/run_eval.py                 # 跑全量
    python eval/run_eval.py --limit 10      # 只跑前 10 题（省钱）
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 让脚本能 import 到 app 包（直接 python eval/run_eval.py 时也能跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

QUESTIONS_FILE = Path(__file__).parent / "questions.json"


def load_questions() -> list[dict]:
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    return data["questions"]


def judge(answer: str, item: dict) -> tuple[bool, str]:
    """判断一道题答对没有。

    规则故意做得简单粗暴，因为复杂的自动打分本身就不靠谱：
      - 该拒答的题：答案里出现"没有"/"无法"/"资料中未"等词 → 算对
      - 该答对的题：所有关键词都出现 → 算对
    """
    if item.get("should_refuse"):
        refuse_signals = ["没有", "无法", "未找到", "资料中未", "不清楚", "不能回答", "没有相关"]
        ok = any(s in answer for s in refuse_signals)
        return ok, "正确拒答" if ok else "本应拒答但编造了答案"

    keywords = item.get("expected_keywords") or []
    if not keywords:
        return True, "无关键词要求，跳过"

    missing = [k for k in keywords if k not in answer]
    if missing:
        return False, f"缺少关键词: {missing}"
    return True, "全部关键词命中"


async def evaluate(limit: int | None = None) -> None:
    from app.agent import run_agent  # 延迟导入：避免没配 .env 时一运行就崩

    questions = load_questions()
    if limit:
        questions = questions[:limit]

    if len(questions) < 10:
        print(f"⚠️  评测集只有 {len(questions)} 题，样本太少，结论不可信。")
        print("   第 2 周请补到 100 题再跑正式评测。\n")

    correct = 0
    results = []

    for i, item in enumerate(questions, 1):
        q = item["question"]
        print(f"[{i}/{len(questions)}] {q}")

        try:
            result = await run_agent(q)
            answer = result.answer
        except Exception as e:
            print(f"   ❌ 执行失败: {e}")
            results.append({**item, "answer": "", "correct": False, "reason": f"执行失败: {e}"})
            continue

        ok, reason = judge(answer, item)
        correct += int(ok)
        mark = "✅" if ok else "❌"
        print(f"   {mark} {reason}")
        print(f"   答案: {answer[:100]}{'...' if len(answer) > 100 else ''}\n")

        results.append({**item, "answer": answer, "correct": ok, "reason": reason})

    total = len(questions)
    rate = correct / total * 100 if total else 0

    print("=" * 50)
    print(f"命中率: {correct}/{total} = {rate:.1f}%")
    print("=" * 50)
    print("\n👉 把这个数字填进 README 的「关键指标」表格。")

    # 保存明细，方便复盘哪些题错了
    out = Path(__file__).parent / "results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已保存: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跑评测集，算命中率")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题，省钱用")
    args = parser.parse_args()

    asyncio.run(evaluate(args.limit))


if __name__ == "__main__":
    main()
