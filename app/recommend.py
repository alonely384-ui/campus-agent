"""新论文推荐：找候选、打分、写理由、记反馈。

信号与赛题 1.4.3 对应：

    * 相关性      —— 候选摘要与"库里所有论文"的主题向量余弦相似度
    * 引用重叠    —— 有几个库内论文被这篇论文引用（赛题说的"大量引用了库里的论文"）
    * GitHub 代码 —— 摘要/评论里有仓库地址就加分（赛题要求"有代码优先"）
    * 新鲜度      —— 年份越新越高
    * 用户反馈    —— 👍/👎 直接加减分，下次刷新生效

主题向量直接用检索模块的 ``embed()``（本地哈希向量），不联网、不额外装模型。
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.llm import chat
from app.logging_config import get_logger
from app.online import (
    OnlineError,
    arxiv_id_of,
    citations,
    find_github,
    read_cache_any_age,
    write_cache,
)
from app.papers import BASE_DIR, load_structures, sync_index
from app.rag.pipeline import embed

logger = get_logger(__name__)

META_DIR = BASE_DIR / "data" / "meta"
FEEDBACK_FILE = META_DIR / "feedback.json"
RECOMMEND_FILE = META_DIR / "recommendations.json"
CACHE_KEY = "recommendations_latest"

RANK_LIMIT = 12
MAX_SOURCE_PAPERS = 6
PER_SOURCE = 25
RECENT_YEARS = 2

WEIGHT_OVERLAP = 0.35
WEIGHT_TOPIC = 0.35
WEIGHT_CODE = 0.15
WEIGHT_FRESH = 0.15
VOTE_UP = 0.15
VOTE_DOWN = -0.35


# --------------------------------------------------------------------------- 反馈


def load_feedback() -> dict[str, str]:
    """读用户反馈：{arxiv_id: "up"|"down"}。"""
    if not FEEDBACK_FILE.exists():
        return {}
    try:
        payload = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items() if str(v) in {"up", "down"}}


def save_feedback(arxiv_id: str, vote: str) -> dict[str, str]:
    """记一条反馈（up/down，传空串表示取消）。"""
    arxiv_id = arxiv_id.strip()
    if not arxiv_id:
        raise ValueError("缺少 arXiv 编号")
    if vote not in {"up", "down", ""}:
        raise ValueError("vote 只能是 up / down / 空")
    data = load_feedback()
    if vote:
        data[arxiv_id] = vote
    else:
        data.pop(arxiv_id, None)
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- 库内画像


def library_rows() -> list[dict[str, Any]]:
    """库里每篇论文的 {paper_id, filename, arxiv_id, title, abstract}。"""
    structures = load_structures()
    rows: list[dict[str, Any]] = []
    for paper in sync_index():
        meta = structures.get(paper.filename) or {}
        arxiv_id = arxiv_id_of(paper.filename) or arxiv_id_of(str(meta.get("title") or ""))
        rows.append(
            {
                "paper_id": paper.paper_id,
                "filename": paper.filename,
                "arxiv_id": arxiv_id,
                "title": str(meta.get("title") or Path(paper.filename).stem),
                "abstract": str(meta.get("abstract") or "")[:1200],
            }
        )
    return rows


def _cosine(left: list[float], right: list[float]) -> float:
    """两个向量夹角的余弦；任一为零向量就当 0。"""
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def library_vector(rows: list[dict[str, Any]]) -> list[float]:
    """把库内论文的标题+摘要平均成一个主题向量（保留给需要"整体画像"的场景）。"""
    vectors = [embed(f"{row['title']}\n{row['abstract']}") for row in rows if row["title"]]
    if not vectors:
        return []
    dim = len(vectors[0])
    total = [0.0] * dim
    for vec in vectors:
        for i, value in enumerate(vec):
            total[i] += value
    return [value / len(vectors) for value in total]


def library_vectors(rows: list[dict[str, Any]]) -> list[tuple[str, list[float]]]:
    """每篇库内论文一个向量：候选只跟"最像的那一篇"比，比跟 39 篇的平均值比更有意义。

    用平均向量时，一篇讲"Transformer 用于医学问卷"的论文会因为这 39 篇里有很多
    不相干的方向而被稀释；取最大相似度得到的是"跟库里某一篇最接近"，这才是
    "这篇推荐给你是因为它像你库里的 X"。
    """
    out: list[tuple[str, list[float]]] = []
    for row in rows:
        title = row["title"]
        if not title:
            continue
        out.append((title, embed(f"{title}\n{row['abstract']}")))
    return out


# --------------------------------------------------------------------------- 候选


def collect_candidates(
    max_papers: int = MAX_SOURCE_PAPERS, per_source: int = PER_SOURCE
) -> tuple[list[dict[str, Any]], list[str]]:
    """顺着库内论文的"被引用"列表收集候选，返回 (候选, 出错的来源)。

    只取"别人引用了库里的论文"这一路——它同时满足赛题的两句话
    （"大量引用了库里的论文"就是引用关系，"主题非常相关"用向量再算一遍）。
    """
    rows = library_rows()
    known = {row["arxiv_id"] for row in rows if row["arxiv_id"]}
    known_titles = {_title_key(row["title"]) for row in rows}
    # 新论文优先从最近入库的论文上找；越老的论文引出的候选越不新鲜
    sources = sorted(rows, key=lambda row: (not row["arxiv_id"], row["filename"]), reverse=False)
    sources = [row for row in sources if row["arxiv_id"]][:max_papers]

    candidates: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for source in sources:
        try:
            cited = citations(source["arxiv_id"], limit=per_source)
        except OnlineError as exc:
            errors.append(f"{source['title']}: {exc}")
            continue
        for item in cited:
            arxiv_id = item["arxiv_id"]
            title = item["title"]
            if not title or _title_key(title) in known_titles:
                continue
            if arxiv_id and arxiv_id in known:
                continue
            key = arxiv_id or _title_key(title)
            entry = candidates.get(key)
            if entry is None:
                entry = {
                    "key": key,
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "year": int(item.get("year") or 0),
                    "abstract": str(item.get("abstract") or "")[:1200],
                    "authors": item.get("authors") or [],
                    "url": item.get("url") or "",
                    "cited_by": [],
                }
                candidates[key] = entry
            entry["cited_by"].append(source["title"])
    return list(candidates.values()), errors


def _title_key(title: str) -> str:
    """标题归一化成小写字母数字，用来判"是不是同一篇"。"""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:120]


def score_candidates(
    candidates: list[dict[str, Any]],
    lib_vecs: list[tuple[str, list[float]]],
    feedback: dict[str, str] | None = None,
    current_year: int | None = None,
) -> list[dict[str, Any]]:
    """给候选打分并排序（分数 0~1，越高越该推荐）。

    `topic` = 与库内**最相似那一篇**的余弦相似度；`topic_of` 记下是哪一篇，
    用来写"这篇像你库里的 X"。明显不相关的（既没多少引用重叠、主题也不像的）
    直接丢掉，否则引用 Transformer 的医学、燃料电池论文都会冒进推荐列表。
    """
    feedback = feedback or {}
    year_now = current_year or datetime.now(UTC).year
    kept: list[dict[str, Any]] = []
    for item in candidates:
        overlap_score = min(1.0, len(item["cited_by"]) / 3.0)
        text = f"{item['title']}\n{item.get('abstract') or ''}"
        vec = embed(text) if lib_vecs else []
        topic_score, topic_of = 0.0, ""
        for title, lib_vec in lib_vecs:
            sim = max(0.0, _cosine(lib_vec, vec))
            if sim > topic_score:
                topic_score, topic_of = sim, title
        # 相关性闸门：引用重叠够多，或者主题足够像，才留；两个都很弱就是噪声
        if len(item["cited_by"]) < 2 and topic_score < 0.22:
            continue
        if topic_score < 0.10:
            continue
        github = find_github(item.get("abstract") or "", item["title"])
        code_score = 1.0 if github else 0.0
        age = max(0, year_now - int(item.get("year") or 0)) if item.get("year") else 3
        fresh_score = max(0.0, 1.0 - age / 4.0)
        vote = feedback.get(item.get("key") or "", "")
        vote_score = VOTE_UP if vote == "up" else VOTE_DOWN if vote == "down" else 0.0
        base = (
            WEIGHT_OVERLAP * overlap_score
            + WEIGHT_TOPIC * topic_score
            + WEIGHT_CODE * code_score
            + WEIGHT_FRESH * fresh_score
        )
        item["github"] = github
        item["overlap"] = round(overlap_score, 3)
        item["topic"] = round(topic_score, 3)
        item["topic_of"] = topic_of
        # base 是"不含用户反馈"的分数；点赞点踩后只要 base + 新票就能重排，
        # 不用把库重新扫一遍（见 rerank_with_feedback）。
        item["base"] = round(base, 4)
        item["score"] = round(base + vote_score, 4)
        item["vote"] = vote
        kept.append(item)
    return sorted(kept, key=lambda item: item["score"], reverse=True)


def rerank_with_feedback(
    items: list[dict[str, Any]], feedback: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """按当前反馈重新算分并排序（点完 👍/👎 立刻生效，不必重新抓网）。"""
    feedback = load_feedback() if feedback is None else feedback
    for item in items:
        vote = feedback.get(item.get("key") or "", "")
        base = float(item.get("base") or item.get("score") or 0.0)
        vote_score = VOTE_UP if vote == "up" else VOTE_DOWN if vote == "down" else 0.0
        item["vote"] = vote
        item["score"] = round(base + vote_score, 4)
    return sorted(items, key=lambda item: item["score"], reverse=True)


# --------------------------------------------------------------------------- 理由

_REASON_PROMPT = """你是一个学术助理。下面是若干篇候选论文，以及用户的论文库里已有哪些论文。

请为每篇候选写一句中文推荐理由（30~60 字），说清它跟用户库里的哪篇论文相关、新在哪里。
只输出 JSON 数组，不要任何解释，格式：
[{"arxiv_id": "2401.00001", "reason": "..."}]

用户库里的论文：
__LIBRARY__

候选论文：
__CANDIDATES__
"""


async def write_reasons(items: list[dict[str, Any]], library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用大模型给候选写中文理由；模型不可用时退回模板句。"""
    if not items:
        return items
    lib_text = "\n".join(f"- {row['title']}" for row in library[:12])
    cand_text = "\n".join(
        f"- arxiv_id={item['arxiv_id'] or item['key']}；标题={item['title']}；"
        f"引用库内 {len(item['cited_by'])} 篇（{'; '.join(item['cited_by'][:3])}）；"
        f"最相近的库内论文={item.get('topic_of') or '无'}（相似度 {item.get('topic', 0):.2f}）；"
        f"年份={item.get('year') or '未知'}；摘要={item.get('abstract', '')[:300]}"
        for item in items
    )
    prompt = _REASON_PROMPT.replace("__LIBRARY__", lib_text).replace("__CANDIDATES__", cand_text)
    try:
        answer = await chat([{"role": "user", "content": prompt}], temperature=0.3)
        reasons = _parse_reasons(answer)
    except Exception as exc:  # 理由只是锦上添花，失败不能让整页挂掉
        logger.warning("生成推荐理由失败：%s", exc)
        reasons = {}
    for item in items:
        reason = reasons.get(item["arxiv_id"] or item["key"], "")
        if not reason:
            reason = (
                f"引用了库内 {len(item['cited_by'])} 篇论文（{item['cited_by'][0]} 等），"
                f"主题相关度 {item['topic']:.2f}"
            )
            if item.get("github"):
                reason += "，且提供了开源代码"
        item["reason"] = reason
    return items


def _parse_reasons(answer: str) -> dict[str, str]:
    """从模型回复里抠 JSON 数组；抠不出来就返回空字典。"""
    match = re.search(r"\[.*\]", answer or "", re.S)
    if not match:
        return {}
    try:
        rows = json.loads(match.group(0))
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("arxiv_id") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if key and reason:
            out[key] = reason
    return out


# --------------------------------------------------------------------------- 入口


async def build_recommendations(
    limit: int = RANK_LIMIT, max_papers: int = MAX_SOURCE_PAPERS
) -> dict[str, Any]:
    """抓候选 → 打分 → 写理由 → 落缓存。返回 {updated_at, items, errors, offline}。"""
    rows = library_rows()
    lib_vecs = library_vectors(rows)
    raw, errors = collect_candidates(max_papers=max_papers)
    ranked = score_candidates(raw, lib_vecs, load_feedback())
    # 有 arXiv 号的排前面：推荐出来却下不下来，对用户没意义；
    # 但也不直接丢掉（期刊论文也可能想看），排后面并标出来。
    ranked.sort(key=lambda item: (bool(item.get("arxiv_id")), item["score"]), reverse=True)
    without_arxiv = sum(1 for item in ranked if not item.get("arxiv_id"))
    ranked = await write_reasons(ranked[:limit], rows)
    payload = {
        "updated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "library_size": len(rows),
        "items": ranked,
        "errors": errors,
        "offline": False,
        "candidates": len(raw),
        "without_arxiv": without_arxiv,
    }
    write_cache(CACHE_KEY, payload)
    RECOMMEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECOMMEND_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def cached_recommendations() -> dict[str, Any]:
    """读上次的推荐结果；没有就返回空壳（前端显示"还没刷新过"）。"""
    payload = read_cache_any_age(CACHE_KEY)
    if isinstance(payload, dict) and payload.get("items") is not None:
        payload["offline"] = False
        return payload
    if RECOMMEND_FILE.exists():
        try:
            data = json.loads(RECOMMEND_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            data["offline"] = False
            return data
    return {"updated_at": "", "items": [], "errors": [], "offline": True, "library_size": 0}
