"""延迟测试：算出首 token 延迟的 P50 / P95。

P95 是什么意思？
    把所有请求的耗时从小到大排队，第 95% 位置的那个值。
    它代表"最慢的那 5% 用户有多难受"，比平均值更能反映真实体验。
    简历上写 P95 而不是平均值，会显得你真的懂性能。

用法：
    python scripts/bench_latency.py            # 默认 20 次
    python scripts/bench_latency.py -n 50      # 跑 50 次
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 用固定问题，避免不同问题难度差异干扰测量
TEST_QUESTION = "简单介绍一下你自己"


async def measure_once() -> tuple[float, float]:
    """返回 (首 token 延迟秒, 总耗时秒)。

    ⚠️ 这里必须用流式接口才能测到"首 token 延迟"。
       非流式接口只能测总耗时 —— 而用户感知的"快慢"主要是首 token。
    """
    from app.llm import chat_stream

    messages = [{"role": "user", "content": TEST_QUESTION}]

    start = time.perf_counter()
    first_token_at = None
    count = 0

    async for _ in chat_stream(messages):
        count += 1
        if first_token_at is None:
            first_token_at = time.perf_counter()

    end = time.perf_counter()

    if first_token_at is None:
        raise RuntimeError("没有收到任何内容")

    return first_token_at - start, end - start


async def main(n: int) -> None:
    print(f"开始测试，共 {n} 次…\n")

    firsts: list[float] = []
    totals: list[float] = []

    for i in range(1, n + 1):
        try:
            first, total = await measure_once()
            firsts.append(first)
            totals.append(total)
            print(f"[{i:>3}/{n}] 首token {first:.2f}s   总耗时 {total:.2f}s")
        except Exception as e:
            print(f"[{i:>3}/{n}] ❌ 失败: {e}")
        # 稍微停一下，避免触发接口限流
        await asyncio.sleep(0.5)

    if not firsts:
        print("\n没有任何有效数据，请检查 API Key 和网络。")
        return

    def pct(values: list[float], p: float) -> float:
        """自己实现分位数，避免额外依赖 numpy。"""
        s = sorted(values)
        idx = min(int(len(s) * p), len(s) - 1)
        return s[idx]

    print("\n" + "=" * 50)
    print(f"有效样本: {len(firsts)}/{n}")
    print(f"首 token 延迟   平均 {statistics.mean(firsts):.2f}s   "
          f"P50 {pct(firsts, 0.50):.2f}s   P95 {pct(firsts, 0.95):.2f}s")
    print(f"总耗时         平均 {statistics.mean(totals):.2f}s   "
          f"P50 {pct(totals, 0.50):.2f}s   P95 {pct(totals, 0.95):.2f}s")
    print("=" * 50)
    print("\n👉 把「首 token 延迟 P95」填进 README 的关键指标表。")


def cli() -> None:
    parser = argparse.ArgumentParser(description="测首 token 延迟")
    parser.add_argument("-n", "--count", type=int, default=20, help="测试次数，默认 20")
    args = parser.parse_args()
    asyncio.run(main(args.count))


if __name__ == "__main__":
    cli()
