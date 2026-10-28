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
    github_repo,
    paper_meta,
    read_cache_any_age,
    recent_papers,
    write_cache,
)
from app.papers import BASE_DIR, load_structures, sync_index
from app.rag.pipeline import embed

logger = get_logger(__name__)

META_DIR = BASE_DIR / "data" / "meta"
FEEDBACK_FILE = META_DIR / "feedback.json"
RECOMMEND_FILE = META_DIR / "recommendations.json"
CACHE_KEY = "recommendations_latest"
DEFAULT_USER = "default"

RANK_LIMIT = 12
MAX_SOURCE_PAPERS = 6
PER_SOURCE = 25
RECENT_YEARS = 2
# 新上架通道：扫这几个分类的 recent 列表，最多挑这么多篇补充摘要
RECENT_CATEGORIES = ("cs.CL", "cs.LG")
RECENT_DAYS = 14
RECENT_KEEP = 12
# 没有引用信号时主题相似度要更严，否则会推荐一堆"只是词面像"的论文
# 0.20 实测仍会放进"训练安全退化"这类只共享个别词的论文，提到 0.24 更干净
RECENT_MIN_TOPIC = 0.24
# 榜单里给「新上架」保底位置：刚发布的论文还没人引用，纯按分数排永远挤不进来，
# 等于 1.4.3 的"每日/每周新论文推荐"形同虚设。
RECENT_QUOTA = 3

WEIGHT_OVERLAP = 0.40
WEIGHT_TOPIC = 0.35
WEIGHT_CODE = 0.15
WEIGHT_FRESH = 0.10
VOTE_UP = 0.15
VOTE_DOWN = -0.35


# --------------------------------------------------------------------------- 反馈


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_all_feedback() -> dict[str, dict[str, str]]:
    """读所有人的反馈：``{user: {arxiv_id: "up"|"down"}}``。

    兼容老格式（扁平 ``{arxiv_id: vote}``）：老数据全部记在 ``DEFAULT_USER`` 名下，
    这样升级到"多用户"之后，之前点的赞不会凭空消失。
    """
    payload = _read_json(FEEDBACK_FILE)
    users = payload.get("users")
    if not isinstance(users, dict):
        flat = {str(k): str(v) for k, v in payload.items() if str(v) in {"up", "down"}}
        return {DEFAULT_USER: flat} if flat else {}
    out: dict[str, dict[str, str]] = {}
    for user, votes in users.items():
        if not isinstance(votes, dict):
            continue
        clean = {str(k): str(v) for k, v in votes.items() if str(v) in {"up", "down"}}
        if clean:
            out[str(user)] = clean
    return out


def load_feedback(user: str = DEFAULT_USER) -> dict[str, str]:
    """读某个用户自己的反馈：``{arxiv_id: "up"|"down"}``（1.4.3 ④ 多用户）。"""
    return dict(load_all_feedback().get(user or DEFAULT_USER, {}))


def save_feedback(arxiv_id: str, vote: str, user: str = DEFAULT_USER) -> dict[str, str]:
    """记一条反馈（up/down，传空串表示取消）；返回该用户最新的反馈表。"""
    arxiv_id = arxiv_id.strip()
    if not arxiv_id:
        raise ValueError("缺少 arXiv 编号")
    if vote not in {"up", "down", ""}:
        raise ValueError("vote 只能是 up / down / 空")
    user = (user or DEFAULT_USER).strip() or DEFAULT_USER
    all_votes = load_all_feedback()
    mine = all_votes.get(user, {})
    if vote:
        mine[arxiv_id] = vote
    else:
        mine.pop(arxiv_id, None)
    if mine:
        all_votes[user] = mine
    else:
        all_votes.pop(user, None)
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_FILE.write_text(
        json.dumps({"users": all_votes}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return dict(mine)


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


def collect_recent(
    max_items: int = RECENT_KEEP,
    days: int = RECENT_DAYS,
    lib_vecs: list[tuple[str, list[float]]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """扫 arXiv 近期上架列表，把新论文也当候选（1.4.3 的另一条来路）。

    只靠引用关系，刚发出来还没人引用的论文永远进不来；这条路补的就是它们。
    列表页没有摘要，所以先用**标题**粗排（免费、不联网），只给最像库内的少数几篇
    补 abs 详情页（有 30 天缓存），否则一天 200 篇全抓一遍又慢又没意义。
    """
    rows = library_rows()
    known = {row["arxiv_id"] for row in rows if row["arxiv_id"]}
    known_titles = {_title_key(row["title"]) for row in rows}
    try:
        items = recent_papers(RECENT_CATEGORIES, per_page=100, days=days)
    except OnlineError as exc:  # 这条路挂了不该拖垮整个推荐
        return [], [f"新上架：{exc}"]

    pool: list[dict[str, Any]] = []
    for item in items:
        arxiv_id = item["arxiv_id"]
        title = item["title"]
        if not title or _title_key(title) in known_titles or arxiv_id in known:
            continue
        pool.append(item)

    # 标题粗排：只有一个标题时主题相似度偏低，所以先多留几倍，等详情页拿到摘要再精排
    if lib_vecs and pool:
        scored = []
        for item in pool:
            rough, _, _ = topic_profile(embed(item["title"]), lib_vecs)
            scored.append((rough, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        pool = [item for _, item in scored[: max_items * 4]]

    picked = pool[:max_items]

    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in picked:
        arxiv_id = item["arxiv_id"]
        title = item["title"]
        abstract = ""
        year = 0
        try:
            meta = paper_meta(arxiv_id)
            abstract = str(meta.get("abstract") or "")[:1200]
            year = int(meta.get("year") or 0)
            title = meta.get("title") or title
        except OnlineError as exc:
            errors.append(f"新上架 {arxiv_id}：{exc}")
        out.append(
            {
                "key": arxiv_id,
                "arxiv_id": arxiv_id,
                "title": title,
                "year": year,
                "abstract": abstract,
                "authors": [],
                "url": item.get("url") or "",
                "cited_by": [],
                "source": "recent",
                "posted": item.get("posted") or "",
                "category": item.get("category") or "",
            }
        )
    return out, errors


def topic_profile(
    vec: list[float], lib_vecs: list[tuple[str, list[float]]]
) -> tuple[float, str, list[dict[str, Any]]]:
    """算候选与库内论文的"主题相似度"。

    不用"只跟最像那一篇比"：跨领域时最近邻很容易飘（把 RL 论文算成最像 Llama 2），
    这里把 max 与 top-3 均值混合，既保留"像你库里的 X"，又不被单篇异常值带跑。
    返回 ``(topic, 最像的标题, top3 明细)``。
    """
    sims: list[tuple[str, float]] = []
    for title, lib_vec in lib_vecs:
        sims.append((title, max(0.0, _cosine(lib_vec, vec))))
    if not sims:
        return 0.0, "", []
    sims.sort(key=lambda pair: pair[1], reverse=True)
    top = sims[:3]
    best = top[0][1]
    mean_top = sum(value for _, value in top) / len(top)
    topic = 0.55 * best + 0.45 * mean_top
    return round(topic, 4), top[0][0], [{"title": title, "sim": round(sim, 3)} for title, sim in top]


def score_candidates(
    candidates: list[dict[str, Any]],
    lib_vecs: list[tuple[str, list[float]]],
    feedback: dict[str, str] | None = None,
    current_year: int | None = None,
) -> list[dict[str, Any]]:
    """给候选打分并排序（分数 0~1，越高越该推荐）。

    两路候选的闸门不同：
    - ``source == "cited"``（别人引用了库里的论文）：引用重叠或主题够像都能留；
    - ``source == "recent"``（新上架）：没有引用信号，只能靠主题，所以门槛更高
      （``RECENT_MIN_TOPIC``），否则会塞满"只是词面像"的论文。
    """
    feedback = feedback or {}
    year_now = current_year or datetime.now(UTC).year
    kept: list[dict[str, Any]] = []
    for item in candidates:
        source = item.get("source") or "cited"
        overlap_score = min(1.0, len(item["cited_by"]) / 3.0)
        text = f"{item['title']}\n{item.get('abstract') or ''}"
        vec = embed(text) if lib_vecs else []
        topic_score, topic_of, topic_top = topic_profile(vec, lib_vecs) if lib_vecs else (0.0, "", [])
        if source == "recent":
            if topic_score < RECENT_MIN_TOPIC:
                continue
        else:
            # 相关性闸门：引用重叠够多，或者主题足够像，才留；两个都很弱就是噪声。
            # 0.20 是实测出来的下限：低于它的条目基本只是"碰巧被同一篇引过"
            # （比如 RL 的 PPO 引用链带出来的机器人论文），点进去用不上。
            if topic_score < 0.20:
                continue
            if len(item["cited_by"]) < 2 and topic_score < 0.26:
                continue
        github = find_github(item.get("abstract") or "", item["title"])
        code_score = 1.0 if github else 0.0
        age = max(0, year_now - int(item.get("year") or 0)) if item.get("year") else 3
        fresh_score = max(0.0, 1.0 - age / 4.0)
        key = item.get("key") or item.get("arxiv_id") or ""
        vote = feedback.get(key, "")
        vote_score = VOTE_UP if vote == "up" else VOTE_DOWN if vote == "down" else 0.0
        base = (
            WEIGHT_OVERLAP * overlap_score
            + WEIGHT_TOPIC * topic_score
            + WEIGHT_CODE * code_score
            + WEIGHT_FRESH * fresh_score
        )
        item["source"] = source
        item["github"] = github
        item["overlap"] = round(overlap_score, 3)
        item["topic"] = round(topic_score, 3)
        item["topic_of"] = topic_of
        item["topic_top"] = topic_top
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
        key = item.get("key") or item.get("arxiv_id") or ""
        vote = feedback.get(key, "")
        base = float(item.get("base") or item.get("score") or 0.0)
        vote_score = VOTE_UP if vote == "up" else VOTE_DOWN if vote == "down" else 0.0
        item["vote"] = vote
        item["score"] = round(base + vote_score, 4)
    # 排序口径要和 build_recommendations 一致：有 arXiv 号（能直接下载）的排前面，
    # 同组内再比分数；否则"点赞后重排"会把没 arXiv 号的顶上来，前后两次顺序对不上。
    return sorted(
        items,
        key=lambda item: (bool(item.get("arxiv_id")), item["score"]),
        reverse=True,
    )


# --------------------------------------------------------------------------- 理由

_REASON_PROMPT = """你是一个学术助理。下面是若干篇候选论文，以及用户论文库里已有的论文（附摘要片段）。

请为每篇候选写一句中文推荐理由（35~70 字），要求是**对照式**的：
- 相似度 ≥0.30 时：先点明它在库里的哪一篇**基础上**做（只能用下面给的"最相近的库内论文"或"引用库内"里出现过的篇名，不许编），再说它的**区别/新意**（换了任务、数据、方法或规模）；
- 相似度 <0.30 时：**禁止**写"在《X》的基础上"，直接讲这篇自己在做什么、属于哪个方向，以及它和库内那篇的差异在哪；
- 如果候选摘要里明确写了在某个数据集上提升或超过某个基线，可以点出一句；没写就不要编数字。
只输出 JSON 数组，不要任何解释，格式：
[{"arxiv_id": "2401.00001", "reason": "在《Layer Normalization》的基础上……，区别在于……"}]

用户库里的论文（篇名 — 摘要片段）：
__LIBRARY__

候选论文：
__CANDIDATES__
"""


def _library_brief(library: list[dict[str, Any]], limit: int = 12) -> str:
    """库内论文的「篇名 — 摘要片段」，给理由提示词当对照锚点。"""
    lines: list[str] = []
    for row in library[:limit]:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        abstract = re.sub(r"\s+", " ", str(row.get("abstract") or "")).strip()[:160]
        lines.append(f"- {title}" + (f" — {abstract}" if abstract else ""))
    return "\n".join(lines)


async def write_reasons(items: list[dict[str, Any]], library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用大模型给候选写中文理由；模型不可用时退回模板句。"""
    if not items:
        return items
    lib_text = _library_brief(library)
    cand_text = "\n".join(
        f"- arxiv_id={item['arxiv_id'] or item['key']}；标题={item['title']}；"
        f"来路={'新上架' if item.get('source') == 'recent' else '引用扩展'}；"
        f"引用库内 {len(item['cited_by'])} 篇（{'; '.join(item['cited_by'][:3]) or '无'}）；"
        f"最相近的库内论文={item.get('topic_of') or '无'}（相似度 {item.get('topic', 0):.2f}）；"
        f"有代码={item.get('github') or '无'}；"
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
            anchor = item.get("topic_of") or ""
            topic = float(item.get("topic") or 0.0)
            if item.get("source") == "recent" and topic < 0.30:
                # 相似度不高时不要硬凑"在《X》基础上"，那句话会误导用户
                reason = (
                    f"arXiv 近两周新上架（{item.get('category') or '未分类'}）；"
                    f"与库内《{anchor or '—'}》方向不同（相似度 {topic:.2f}），"
                    f"可作为该方向的旁证参考"
                )
            elif item.get("source") == "recent":
                reason = (
                    f"arXiv 近两周新上架（{item.get('category') or '未分类'}），"
                    f"主题与库内《{anchor or '—'}》最接近（相似度 {item['topic']:.2f}）"
                )
            elif anchor:
                reason = (
                    f"在库内《{anchor}》的基础上（主题相似度 {item['topic']:.2f}）"
                    f"又引用了库内 {len(item['cited_by'])} 篇论文，区别是它换了问题设定与实验对象"
                )
            else:
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


def _select_mixed(ranked: list[dict[str, Any]], limit: int, quota: int = RECENT_QUOTA) -> list[dict[str, Any]]:
    """按分数取前 ``limit`` 条，但给「新上架」留至少 ``quota`` 个名额。

    为什么不用 ``ranked[:limit]``：新上架没有引用信号，分数天然比引用扩展低
    （实测 0.22~0.30 vs 0.35~0.38），纯按分数排会一条都进不去，
    赛题 1.4.3 的"每日/每周新论文推荐"就名存实亡。
    选完仍按 ``(有无 arXiv 号, 分数)`` 排，保证能下载的在前。
    """
    if limit <= 0:
        return []
    recent = [item for item in ranked if item.get("source") == "recent"]
    others = [item for item in ranked if item.get("source") != "recent"]
    keep_recent = recent[: min(quota, len(recent), limit)]
    keep_others = others[: max(0, limit - len(keep_recent))]
    picked = keep_others + keep_recent
    picked.sort(key=lambda item: (bool(item.get("arxiv_id")), item["score"]), reverse=True)
    return picked


async def build_recommendations(
    limit: int = RANK_LIMIT,
    max_papers: int = MAX_SOURCE_PAPERS,
    include_recent: bool = True,
    user: str = DEFAULT_USER,
    period: str = "daily",
    check_code: bool = True,
) -> dict[str, Any]:
    """抓候选 → 打分 → 写理由 → 落缓存。

    两路候选合并：``cited``（别人引用了库里的论文）+ ``recent``（arXiv 近期上架）。
    后者让"刚发布、还没人引用"的相关论文也能进榜，见 ``collect_recent()``。

    ``period`` 只影响"新上架"的时间窗（daily=7 天、weekly=14 天），对应赛题
    1.4.3 的"每日/每周推荐"；``user`` 决定用谁的反馈与协同画像。
    """
    period = period if period in {"daily", "weekly"} else "daily"
    recent_days = 7 if period == "daily" else RECENT_DAYS
    rows = library_rows()
    lib_vecs = library_vectors(rows)
    raw, errors = collect_candidates(max_papers=max_papers)
    recent_scanned = 0
    if include_recent:
        fresh_items, recent_errors = collect_recent(lib_vecs=lib_vecs, days=recent_days)
        recent_scanned = len(fresh_items)
        known = {item["arxiv_id"] for item in raw if item.get("arxiv_id")}
        raw.extend(item for item in fresh_items if item["arxiv_id"] not in known)
        errors = errors + recent_errors
    ranked = score_candidates(
        raw, lib_vecs, load_feedback(user)
    )
    # 有 arXiv 号的排前面：推荐出来却下不下来，对用户没意义；
    # 但也不直接丢掉（期刊论文也可能想看），排后面并标出来。
    ranked.sort(key=lambda item: (bool(item.get("arxiv_id")), item["score"]), reverse=True)
    if check_code:
        _fill_github(ranked[: max(limit * 2, 8)])
        # 补到代码的条目分数会变（+WEIGHT_CODE），所以补完要重新排序，
        # 否则"接口顺序"和"缓存里的顺序"会打架（点赞重排后顺序突变）。
        ranked.sort(key=lambda item: (bool(item.get("arxiv_id")), item["score"]), reverse=True)
    without_arxiv = sum(1 for item in ranked if not item.get("arxiv_id"))
    sources = {
        "cited": sum(1 for item in ranked if item.get("source") != "recent"),
        "recent": sum(1 for item in ranked if item.get("source") == "recent"),
        "github": sum(1 for item in ranked if item.get("github")),
    }
    ranked = await write_reasons(_select_mixed(ranked, limit), rows)
    payload = {
        "updated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "library_size": len(rows),
        "items": ranked,
        "errors": errors,
        "offline": False,
        "candidates": len(raw),
        "without_arxiv": without_arxiv,
        "sources": sources,
        "recent_scanned": recent_scanned,
        "user": user,
        "period": period,
    }
    write_cache(CACHE_KEY, payload)
    RECOMMEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECOMMEND_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _fill_github(items: list[dict[str, Any]]) -> None:
    """给摘要里没写仓库的候选补一次 GitHub 搜索（1.4.3 ②「有代码优先」）。

    只对将要展示的前几条做（搜索接口 10 次/分钟），结果 14 天缓存，
    命中后把 ``WEIGHT_CODE`` 补进 base 与 score。
    """
    for item in items:
        if item.get("github"):
            continue
        try:
            repo = github_repo(str(item.get("title") or ""))
        except Exception as exc:  # 搜索挂了不影响推荐
            logger.info("GitHub 兜底查找失败：%s", exc)
            continue
        if not repo:
            continue
        item["github"] = repo
        item["base"] = round(float(item.get("base") or 0.0) + WEIGHT_CODE, 4)
        item["score"] = round(
            float(item["base"])
            + float(item.get("collab") or 0.0)
            + float(item.get("event") or 0.0)
            + (VOTE_UP if item.get("vote") == "up" else VOTE_DOWN if item.get("vote") == "down" else 0.0),
            4,
        )



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
