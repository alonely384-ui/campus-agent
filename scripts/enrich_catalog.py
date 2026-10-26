"""给 `data/catalog.json` 补"类型"字段（B1 + B2）。

B1：能解析出 arXiv 编号的，用 arXiv 官方 API 批量查 `arxiv:primary_category`（权威、免费）。
B2：文件名不是 arXiv 编号的（比如 `N19-1423.pdf`、`language_understanding_paper.pdf`），
    用 LLM 按标题归到 5 个粗类之一。

用法：
    python scripts/enrich_catalog.py            # B1 + B2
    python scripts/enrich_catalog.py --no-llm   # 只跑 B1（不花 API 额度）
"""

from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.config import settings  # noqa: E402

CATALOG_PATH = BASE / "data" / "catalog.json"
ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

LABELS = ["自然语言处理", "机器学习", "计算机视觉", "强化学习", "理论与其他"]

# arXiv 主分类里没有"强化学习"这一类（PPO/DPO 这类论文常标 cs.LG），
# 按标题补一条规则，让分类更贴近论文主题（答辩时能解释："cs.LG 里再按标题细分"）。
RL_HINTS = ("reinforcement learning", "policy optimization", "preference optimization", "rlhf")


def rl_override(item: dict) -> bool:
    title = (item.get("title") or "").lower()
    return any(h in title for h in RL_HINTS)


def coarse(term: str) -> str:
    """arXiv 主分类 → 粗类之一。"""
    if term.startswith("cs.CL"):
        return "自然语言处理"
    if term.startswith(("cs.LG", "cs.AI", "cs.NE", "stat.ML", "cs.MA")):
        return "机器学习"
    if term.startswith("cs.CV"):
        return "计算机视觉"
    if term.startswith("cs.RO"):
        return "机器人"
    if term.startswith(("cs.IR", "cs.SE", "cs.DC", "cs.HC")):
        return "系统与工程"
    return "理论与其他"


def arxiv_categories(ids: list[str]) -> dict[str, str]:
    """一次最多 20 个 id，返回 {arxiv_id: primary_category}。"""
    out: dict[str, str] = {}
    client = httpx.Client(timeout=60, trust_env=False, headers={"User-Agent": "campus-agent/0.1"})
    for i in range(0, len(ids), 20):
        batch = ids[i : i + 20]
        try:
            r = client.get(ARXIV_API, params={"id_list": ",".join(batch), "max_results": 20})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            got = 0
            for entry in root.findall(f"{ATOM}entry"):
                raw_id = (entry.findtext(f"{ATOM}id") or "").rstrip("/")
                arx = raw_id.rsplit("/abs/", 1)[-1].split("v")[0]
                term = entry.findtext(f"{ARXIV_NS}primary_category") or ""
                if not term:
                    cat = entry.find(f"{ATOM}category")
                    term = cat.get("term", "") if cat is not None else ""
                if arx and term:
                    out[arx] = term
                    got += 1
            print(f"  arXiv 批次 {i // 20 + 1}: 请求 {len(batch)} 个，拿到 {got} 个")
        except Exception as e:  # noqa: BLE001
            print(f"  arXiv 批次 {i // 20 + 1} 失败: {type(e).__name__}: {e}")
        time.sleep(3)  # 官方建议限速
    return out


def llm_label(title: str) -> str:
    """B2：让模型按标题归到 5 个粗类之一；失败返回空串。"""
    prompt = (
        "只输出一个类别名，不要解释。类别只能是：" + "、".join(LABELS) + "。\n"
        f"论文标题：{title}"
    )
    try:
        r = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "temperature": 0,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
            trust_env=False,
        )
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        for label in LABELS:
            if label in text:
                return label
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"    LLM 打标签失败: {type(e).__name__}: {e}")
        return ""


def main() -> None:
    no_llm = "--no-llm" in sys.argv
    items = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    ids = sorted({i["arxiv_id"] for i in items if i.get("arxiv_id")})
    print(f"清单 {len(items)} 篇，其中 {len(ids)} 篇有 arXiv 编号")

    cats = arxiv_categories(ids)
    hit = 0
    for item in items:
        term = cats.get(item.get("arxiv_id", ""))
        if term:
            item["arxiv_category"] = term
            item["category"] = coarse(term)
            hit += 1
    print(f"B1：{hit} 篇按 arXiv 主分类归类")

    rest = [i for i in items if not i.get("category") or i["category"] == "未分类"]
    print(f"B2：剩余 {len(rest)} 篇没有 arXiv 编号")
    for item in rest:
        if no_llm:
            continue
        label = llm_label(item.get("title") or item.get("file", ""))
        if label:
            item["category"] = label
            item["category_source"] = "llm"
            print(f"  {item['file']} → {label}")
        else:
            item["category"] = item.get("category") or "未分类"
            print(f"  {item['file']} → 保持未分类")

    # arXiv 没有"强化学习"主分类，PPO/DPO 这类论文按标题补一条规则
    rl = 0
    for item in items:
        if rl_override(item):
            item["category"] = "强化学习"
            rl += 1
    print(f"标题规则：{rl} 篇归为强化学习")

    CATALOG_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    dist = Counter(i.get("category") or "未分类" for i in items)
    print("\n写入完成，分类分布：")
    for label, n in dist.most_common():
        print(f"  {label}: {n} 篇")


if __name__ == "__main__":
    main()
