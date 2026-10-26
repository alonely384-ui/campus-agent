"""论文"精华卡"：每篇论文一份结构化摘要，模板固定、可直接渲染进上下文。

为什么需要它：
    "这篇讲了什么 / 研究问题是什么 / 有什么贡献"这类问题，答案就在论文第 1 页，
    但它和正文其他内容一样要经过向量检索——检索器分不清"论文自己的摘要"和
    "别人论文参考文献里引用过的综述条目"（实测踩过：问强化学习主旨，召回的是
    Llama 2 第 81 页的 References）。

    所以摘要级问题不走检索，直接读预计算好的卡片。

模板（字段顺序固定，渲染时按这个顺序输出）：
    title / abstract / problem / method / contributions / results / limitations / keywords

新增论文时的做法：
    1. 把 PDF 放进 data/papers/
    2. 跑 `python scripts/build_cards.py`（只补没卡片的篇，已存在的跳过）
    3. 卡片写进 data/cards.json，问答侧自动生效，不需要改代码
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.papers import PAPERS_DIR

logger = logging.getLogger(__name__)

CARDS_PATH = PAPERS_DIR / "cards.json"

# 固定模板：解析与渲染都按这个顺序，保证卡片长相一致、可被测试断言
CARD_FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "problem",
    "method",
    "contributions",
    "results",
    "limitations",
    "keywords",
)

_CACHE: dict[str, dict] | None = None
_CACHE_MTIME: float = 0.0


def _normalize(card: dict) -> dict:
    """补全缺失字段并统一类型，避免渲染时到处判空。"""
    out: dict = {}
    for key in CARD_FIELDS:
        value = card.get(key)
        if key in ("contributions", "results", "limitations", "keywords"):
            if isinstance(value, str):
                value = [value] if value.strip() else []
            elif not isinstance(value, list):
                value = []
            out[key] = [str(v).strip() for v in value if str(v).strip()]
        else:
            out[key] = str(value or "").strip()
    # 保留生成方式，便于区分"LLM 精炼"和"规则兜底"
    out["generated_by"] = str(card.get("generated_by") or "unknown")
    return out


def load_cards() -> dict[str, dict]:
    """读 data/cards.json（按 mtime 缓存，脚本重跑后自动生效）。"""
    global _CACHE, _CACHE_MTIME
    try:
        mtime = CARDS_PATH.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _CACHE is not None and mtime == _CACHE_MTIME:
        return _CACHE
    try:
        raw = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 cards.json 失败: %s", exc)
        return {}
    cards = {str(k): _normalize(v) for k, v in (raw.get("cards") or {}).items()}
    _CACHE, _CACHE_MTIME = cards, mtime
    return cards


def get_card(source: str) -> dict | None:
    """按文件名取卡片；取不到返回 None，调用方要能降级。"""
    return load_cards().get(source)


def render_card(card: dict, max_chars: int = 900) -> str:
    """把卡片渲染成一段中文文本（给 prompt 用），超长就截断。"""
    card = _normalize(card)
    lines = [f"标题：{card['title']}"]
    if card["abstract"]:
        lines.append(f"摘要原文：{card['abstract'][:400]}")
    if card["problem"]:
        lines.append(f"研究问题：{card['problem']}")
    if card["method"]:
        lines.append(f"方法：{card['method']}")
    for label, key in (("贡献", "contributions"), ("结果", "results"), ("局限", "limitations")):
        items = card[key]
        if items:
            lines.append(f"{label}：")
            lines.extend(f"- {item}" for item in items)
    if card["keywords"]:
        lines.append("关键词：" + "、".join(card["keywords"]))
    return "\n".join(lines)[:max_chars]


def cards_available() -> int:
    """已生成卡片的篇数（体检脚本和日志用）。"""
    return len(load_cards())


def save_cards(cards: dict[str, dict], built_at: str) -> None:
    """写回卡片文件：先写临时文件再替换，避免中途失败把好文件写坏。"""
    payload = {
        "schema_version": 1,
        "built_at": built_at,
        "fields": list(CARD_FIELDS),
        "cards": cards,
    }
    tmp = CARDS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(tmp).replace(CARDS_PATH)
    logger.info("cards.json 已写入：%d 篇", len(cards))
