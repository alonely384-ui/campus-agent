"""批量生成论文"精华卡"：摘要抽取 + LLM 精炼 + 规则兜底。

用法：
    python scripts/build_cards.py                # 只补没有卡片的篇（增量）
    python scripts/build_cards.py --rebuild      # 全部重生成
    python scripts/build_cards.py --only dpo     # 只跑文件名含 dpo 的（大小写不敏感）
    python scripts/build_cards.py --limit 3      # 只跑前 3 篇（试跑）

为什么增量：40 篇 × 一次 LLM 调用要几分钟，以后加论文只跑新的一篇最省时间。

模板（与 app/cards.py 的 CARD_FIELDS 一致）：
    title / abstract / problem / method / contributions / results / limitations / keywords

LLM 只允许依据摘要原文作答；失败（超时/非 JSON/字段缺失）时退回规则兜底，
卡片仍然写入，`generated_by` 标成 "rule"，不会静默丢失。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cards import load_cards, save_cards  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm import get_client  # noqa: E402
from app.papers import PAPERS_DIR  # noqa: E402

CATALOG = PAPERS_DIR.parent / "catalog.json"
# 摘要结束的常见标记（不同模板排版不一样，宁可多列几个）
_ABSTRACT_END = re.compile(
    r"(?:\n\s*(?:\d+\.?\s*)?introduction\b|\n\s*1\s+introduction\b|"
    r"\n\s*keywords?\b|\n\s*\*?\s*index terms\b)",
    re.IGNORECASE,
)
_ABSTRACT_START = re.compile(r"\babstract\b[\s:.\-—]*", re.IGNORECASE)

PROMPT = """你在给一篇论文做"精华卡"。只能依据下面这段摘要原文作答，不许补充摘要里没有的信息。
如果某个字段摘要里确实没有，就写"摘要未提及"（列表字段就给空数组）。

论文标题：{title}

摘要原文：
{abstract}

只输出 JSON 对象，字段如下：
{{
  "problem": "这篇论文要解决的研究问题（1-2 句，中文）",
  "method": "方法/技术路线（1-2 句，中文）",
  "contributions": ["主要贡献，最多 3 条，每条 1 句"],
  "results": ["关键结果或数字，最多 3 条；没有数字就写定性结论"],
  "limitations": ["局限或未涉及的部分，最多 2 条"],
  "keywords": ["关键词，最多 6 个"]
}}
"""


def extract_abstract(text: str, fallback_chars: int = 1200) -> str:
    """从第 1 页文本里切摘要；切不到就取开头一段（去掉标题行）。"""
    text = re.sub(r"[ \t]+", " ", text)
    match = _ABSTRACT_START.search(text)
    start = match.end() if match else 0
    tail = text[start:]
    end = _ABSTRACT_END.search(tail)
    body = (tail[: end.start()] if end else tail).strip()
    if len(body) < 120:
        # 摘要标题没识别出来：退化为"跳过头几行标题"取前 N 字
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        body = "\n".join(lines[2:])[:fallback_chars].strip()
    return re.sub(r"\s+", " ", body).strip()


def _first_sentence(text: str, limit: int = 160) -> str:
    parts = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    return (parts[0] if parts else "").strip()[:limit]


def rule_card(title: str, abstract: str) -> dict:
    """LLM 不可用时的兜底：只做能确定的部分，不编造。"""
    problem = _first_sentence(abstract) if abstract else "扫描件无文本层，摘要未能抽取（建议 OCR 后再生成）"
    return {
        "title": title,
        "abstract": abstract,
        "problem": problem or "摘要未提及",
        "method": "摘要未提及",
        "contributions": [],
        "results": [],
        "limitations": [],
        "keywords": [],
        "generated_by": "rule",
    }


async def llm_card(title: str, abstract: str) -> dict:
    """调模型生成卡片；任何异常都抛给调用方决定是否兜底。"""
    client = get_client()
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": PROMPT.format(title=title, abstract=abstract)}],
        temperature=0,
        max_tokens=700,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    card = {"title": title, "abstract": abstract, "generated_by": "llm"}
    for key in ("problem", "method"):
        card[key] = str(data.get(key) or "摘要未提及")[:400]
    for key in ("contributions", "results", "limitations", "keywords"):
        value = data.get(key) or []
        if isinstance(value, str):
            value = [value]
        card[key] = [str(v) for v in value if str(v).strip()][:6]
    return card


def page1_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return (reader.pages[0].extract_text() or "").strip() if reader.pages else ""


async def build_one(item: dict, sem: asyncio.Semaphore) -> tuple[str, dict, str]:
    """生成一篇的卡片，返回 (文件名, 卡片, 状态)。"""
    file = str(item.get("file"))
    path = PAPERS_DIR / file
    title = str(item.get("title") or file)
    if not path.exists():
        return file, {}, "缺文件"
    try:
        text = page1_text(path)
    except Exception as exc:  # noqa: BLE001 - 坏 PDF 不能让整批失败
        return file, {}, f"读 PDF 失败: {exc}"
    abstract = extract_abstract(text)
    if len(abstract) < 60:
        # 扫描件/图片型 PDF：没有文本层，写一张"如实说明"的卡片，不静默丢篇
        return file, rule_card(title, ""), "无文本层→规则卡片"

    async with sem:
        try:
            card = await asyncio.wait_for(llm_card(title, abstract), timeout=60)
            return file, card, "llm"
        except Exception as exc:  # noqa: BLE001 - 兜底继续
            return file, rule_card(title, abstract), f"LLM 失败({type(exc).__name__})→规则兜底"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="全部重生成")
    parser.add_argument("--only", default="", help="只跑文件名包含该字符串的篇")
    parser.add_argument("--limit", type=int, default=0, help="最多跑几篇（试跑用）")
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    existing = load_cards()
    todo = [i for i in catalog if args.only.lower() in str(i.get("file", "")).lower()]
    if not args.rebuild:
        todo = [i for i in todo if str(i.get("file")) not in existing]
    if args.limit:
        todo = todo[: args.limit]

    print(f"待生成 {len(todo)} 篇（已有 {len(existing)} 篇）")
    sem = asyncio.Semaphore(3)  # 并发 3：快，但别把 API 打爆
    results = await asyncio.gather(*(build_one(item, sem) for item in todo))

    ok, fail = 0, []
    for file, card, status in results:
        if card:
            existing[file] = card
            ok += 1
        else:
            fail.append((file, status))
        print(f"  [{status}] {file}")

    save_cards(existing, built_at=datetime.now().isoformat(timespec="seconds"))
    print(f"\n完成：新增/更新 {ok} 篇，失败 {len(fail)} 篇，现有 {len(existing)} 篇")
    for file, status in fail:
        print(f"  失败：{file} -> {status}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
