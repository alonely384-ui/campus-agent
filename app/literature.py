"""文献检索与核实：为写作助手（赛题 1.6）提供"候选集约束生成"的候选池。

## 为什么要这个模块

赛题 1.6.1 的原话是「确保 References 是真实没有幻觉」。最靠谱的做法不是
「让模型写完再检查有没有编」，而是**让它根本没有机会编**：

    检索真实文献 → 构成候选池 → 模型只能从池子里挑 → 输出时带上每个候选的来源标记

这样「References 里出现一篇不存在的论文」在结构上就不可能发生，而不是靠概率。

## 三个来源（按可信度排序）

1. **本库**（``data/catalog.json``）：库里真实存在的 39 篇 PDF，命中即可直接关联到本地文件。
   断网也能用，是最可靠的一层。
2. **arXiv Atom API**：实测可用（约 1.6 秒返回）。能拿到 id / 标题 / 作者 / 发布日期 / 摘要。
3. **Crossref**：覆盖非 arXiv 的期刊与会议论文。实测可用。

Semantic Scholar 的 ``/paper/search`` 实测稳定 429（共享 IP 限流），**不用它做检索**，
只保留已有的「按 arXiv id 查引用关系」用途（那是 ``app/online.py`` 的事）。

## 关于代理

这里和 ``app/online.py`` 一样用 ``trust_env=False``：环境里的 ``HTTP_PROXY``
指向本地代理，如果哪天代理没开，httpx 默认读环境变量会让所有请求直接失败。
显式关掉更可控。

## 网络结论的时效性

``app/online.py`` 的注释写着「arXiv 官方 Atom API 在本机连不通」——那是当时的
实测结果。2026-10-29 复测时 Atom API **已经可用**（HTTP 200，1.6 秒），
所以这里直接用它，不再走 HTML 解析。两条路都留着没坏处：Atom 挂了就退回 HTML。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.logging_config import get_logger
from app.papers import BASE_DIR

logger = get_logger(__name__)

CATALOG_FILE = BASE_DIR / "data" / "catalog.json"
# catalog.json 只记了 authors_count（计数），没有名单；作者名在 structures.json 里。
# 1.6.1 生成参考文献时要写作者，所以得从这里补。
STRUCTURES_FILE = BASE_DIR / "data" / "meta" / "structures.json"
CACHE_DIR = BASE_DIR / "data" / "meta" / "online"
CACHE_TTL = 60 * 60 * 6  # 6 小时：检索结果变化慢，缓存能挡掉重复演示的等待

ARXIV_API = "http://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"
USER_AGENT = "campus-agent/0.1 (research demo; contact: local)"
TIMEOUT = 20.0

# 检索结果来源标记。前端直接显示这两个词，别改。
SOURCE_LOCAL = "local"
SOURCE_ARXIV = "arxiv"
SOURCE_CROSSREF = "crossref"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- 数据形状


@dataclass
class Candidate:
    """一篇候选文献。

    这个结构是「候选池」的唯一货币：检索层产出它，LLM 只能从中挑，
    输出层按 ``key`` 把模型的选择映射回真实文献。
    """

    key: str  # 池内唯一标识，模型引用它来"点名"某篇
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    source: str = SOURCE_ARXIV
    arxiv_id: str = ""
    doi: str = ""
    venue: str = ""
    abstract: str = ""
    url: str = ""
    #: 若这篇就是本库里的文件，记下文件名（可用于跳转阅读页）
    local_file: str = ""
    #: 相关度分数，仅用于排序，不对外承诺语义
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 工具函数


def _clean(text: str) -> str:
    """去标签 + 压空白（arXiv 的标题里常有换行）。"""
    text = _TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", text).strip()


# PDF 抽出来的标题常带连字（ﬃ/ﬀ/ﬁ）和软连字符，不归一的话同一篇会被判成两篇
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u00ad": "",
}


def _unligature(text: str) -> str:
    """把 PDF 连字还原成普通字母。

    库里 ``2205.14135v2.pdf`` 的标题抽出来是 ``Eﬃcient``（U+FB03），
    直接拿去比对会和 ``Efficient`` 对不上。
    """
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    return text


def _norm_title(text: str) -> str:
    """标题归一化，用于跨来源判重：只保留字母数字，全小写。"""
    return re.sub(r"[^0-9a-z]+", "", _unligature(text or "").lower())


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", key).strip("_")[:120] or "item"
    return CACHE_DIR / f"lit_{safe}.json"


def _read_cache(key: str) -> list[dict] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(key: str, rows: list[dict]) -> None:
    path = _cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # 缓存写不进去不该让主流程挂掉
        logger.warning("写检索缓存失败 %s: %s", path.name, exc)


# --------------------------------------------------------------------------- 本库


def load_catalog() -> list[dict[str, Any]]:
    """读本库目录。文件不存在就返回空列表，不抛异常。"""
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读 catalog 失败: %s", exc)
        return []
    return data if isinstance(data, list) else []


def load_local_authors() -> dict[str, list[str]]:
    """按 PDF 文件名索引 structures.json 里的作者名单。

    catalog.json 里只有 authors_count，没有名单；抽出作者名的那一轮工作把结果
    落在 structures.json（38/39 篇有）。这里读一次做映射，供 search_local 补作者。
    读不到就返回空字典——下游按「作者信息缺失」处理，不影响主流程。
    """
    try:
        raw = json.loads(STRUCTURES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读 structures 失败（作者将缺失）: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for filename, item in raw.items():
        if not isinstance(item, dict):
            continue
        names = item.get("authors")
        if isinstance(names, list):
            cleaned = [str(n).strip() for n in names if str(n).strip()]
            if cleaned:
                out[str(filename)] = cleaned
    return out


def search_local(query: str, limit: int = 8) -> list[Candidate]:
    """在本库里按关键词找论文。

    打分规则刻意做得简单可解释：标题命中词越多分越高，年份/分类命中小幅加分。
    不引入向量检索——本库只有 39 篇，关键词足够，而且结果可解释，答辩时说得清。
    """
    tokens = [t for t in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", (query or "").lower()) if t]
    if not tokens:
        return []

    out: list[Candidate] = []
    authors_by_file = load_local_authors()
    for row in load_catalog():
        title = str(row.get("title") or "")
        if not title:
            continue
        haystack = " ".join(
            str(row.get(k) or "") for k in ("title", "topic", "category", "arxiv_category")
        ).lower()
        score = 0.0
        for tok in tokens:
            if tok in title.lower():
                score += 2.0
            elif tok in haystack:
                score += 0.5
        if score <= 0:
            continue
        year = None
        try:
            year = int(row["year"]) if row.get("year") else None
        except (TypeError, ValueError):
            year = None
        filename = str(row.get("file") or "")
        out.append(
            Candidate(
                key="",
                title=title,
                year=year,
                source=SOURCE_LOCAL,
                arxiv_id=str(row.get("arxiv_id") or ""),
                local_file=filename,
                venue=str(row.get("category") or ""),
                authors=authors_by_file.get(filename, []),
                score=score,
            )
        )
    out.sort(key=lambda c: -c.score)
    return out[:limit]


# --------------------------------------------------------------------------- arXiv


def search_arxiv(query: str, limit: int = 10) -> list[Candidate]:
    """调 arXiv Atom API 检索。失败返回空列表（调用方退回本库结果）。"""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"arxiv_{q}_{limit}"
    if (cached := _read_cache(cache_key)) is not None:
        return [Candidate(**row) for row in cached]

    # arXiv 的检索语法里空格是 AND；用 all: 做全字段匹配最省事。
    # 双引号包起来当作短语，能明显提高精确率。
    params = {
        "search_query": f'all:"{q}"',
        "start": 0,
        "max_results": max(1, min(limit, 20)),
        "sortBy": "relevance",
    }
    try:
        with _client() as client:
            resp = client.get(ARXIV_API, params=params)
        if resp.status_code != 200:
            logger.warning("arXiv 检索返回 %s", resp.status_code)
            return []
        rows = _parse_arxiv(resp.text)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("arXiv 检索失败: %s", exc)
        return []

    _write_cache(cache_key, [c.to_dict() for c in rows])
    return rows


def _parse_arxiv(xml: str) -> list[Candidate]:
    """解析 Atom XML。手写正则比引 lxml 省一个依赖，arXiv 的格式足够稳定。"""
    out: list[Candidate] = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        id_m = re.search(r"<id>\s*(.*?)\s*</id>", block, re.S)
        title_m = re.search(r"<title>(.*?)</title>", block, re.S)
        if not title_m:
            continue
        title = _clean(title_m.group(1))
        if not title:
            continue

        raw_id = _clean(id_m.group(1)) if id_m else ""
        # http://arxiv.org/abs/1706.03762v7 -> 1706.03762
        arxiv_id = ""
        if (m := re.search(r"/(\d{4}\.\d{4,5})", raw_id)) or (m := re.search(r"/([a-z\-]+/\d{7})", raw_id)):
            arxiv_id = m.group(1)

        authors = [_clean(a) for a in re.findall(r"<name>(.*?)</name>", block, re.S)][:8]

        year = None
        if (pub := re.search(r"<published>\s*(\d{4})-", block)):
            year = int(pub.group(1))

        abstract = ""
        if (ab := re.search(r"<summary>(.*?)</summary>", block, re.S)):
            abstract = _clean(ab.group(1))

        out.append(
            Candidate(
                key="",
                title=title,
                authors=authors,
                year=year,
                source=SOURCE_ARXIV,
                arxiv_id=arxiv_id,
                abstract=abstract,
                url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else raw_id,
            )
        )
    return out


def fetch_arxiv_by_id(arxiv_id: str) -> Candidate | None:
    """按 arXiv id 精确取一篇（用于「把参考文献里的 id 落实成真实条目」）。"""
    aid = (arxiv_id or "").strip()
    if not aid:
        return None
    cache_key = f"arxivid_{aid}"
    if (cached := _read_cache(cache_key)) is not None:
        return Candidate(**cached[0]) if cached else None

    try:
        with _client() as client:
            resp = client.get(ARXIV_API, params={"id_list": aid, "max_results": 1})
        if resp.status_code != 200:
            return None
        rows = _parse_arxiv(resp.text)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("arXiv 按 id 取失败 %s: %s", aid, exc)
        return None

    _write_cache(cache_key, [c.to_dict() for c in rows])
    return rows[0] if rows else None


# --------------------------------------------------------------------------- Crossref


def search_crossref(query: str, limit: int = 8) -> list[Candidate]:
    """调 Crossref 检索（覆盖期刊/会议）。失败返回空列表。"""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"crossref_{q}_{limit}"
    if (cached := _read_cache(cache_key)) is not None:
        return [Candidate(**row) for row in cached]

    params = {
        "query.bibliographic": q,
        "rows": max(1, min(limit, 20)),
        "select": "DOI,title,author,issued,container-title,abstract",
    }
    try:
        with _client() as client:
            resp = client.get(CROSSREF_API, params=params)
        if resp.status_code != 200:
            logger.warning("Crossref 检索返回 %s", resp.status_code)
            return []
        rows = _parse_crossref(resp.json())
    except (httpx.HTTPError, OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Crossref 检索失败: %s", exc)
        return []

    _write_cache(cache_key, [c.to_dict() for c in rows])
    return rows


def _parse_crossref(payload: dict) -> list[Candidate]:
    out: list[Candidate] = []
    for item in (payload.get("message") or {}).get("items") or []:
        titles = item.get("title") or []
        title = _clean(titles[0]) if titles else ""
        if not title:
            continue
        authors: list[str] = []
        for a in item.get("author") or []:
            name = " ".join(p for p in (a.get("given"), a.get("family")) if p).strip()
            if name:
                authors.append(name)
        year = None
        parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            try:
                year = int(parts[0])
            except (TypeError, ValueError):
                year = None
        venues = item.get("container-title") or []
        doi = str(item.get("DOI") or "")
        out.append(
            Candidate(
                key="",
                title=title,
                authors=authors[:8],
                year=year,
                source=SOURCE_CROSSREF,
                doi=doi,
                venue=_clean(venues[0]) if venues else "",
                abstract=_clean(item.get("abstract") or ""),
                url=f"https://doi.org/{doi}" if doi else "",
            )
        )
    return out


# --------------------------------------------------------------------------- 汇总


def _dedupe(rows: list[Candidate]) -> list[Candidate]:
    """跨来源判重：同一篇只留一个。

    优先级：本库 > arXiv > Crossref。理由是本库条目能跳转到阅读页看原文，
    信息最完整；Crossref 的标题偶尔带期刊后缀，最不可靠。

    注意本库内部**也有重复文件**：``Attention Is All You Need`` 在库里存了
    ``1706.03762v1.pdf`` 和 ``1706.03762v7.pdf`` 两份（版本不同）。
    判重时按归一化标题 + arXiv 基础号双键，命中就只保留第一个。
    """
    rank = {SOURCE_LOCAL: 0, SOURCE_ARXIV: 1, SOURCE_CROSSREF: 2}
    seen: dict[str, Candidate] = {}
    out: list[Candidate] = []

    for c in sorted(rows, key=lambda x: rank.get(x.source, 9)):
        keys = [k for k in (_norm_title(c.title), c.arxiv_id.strip()) if k]
        if any(k in seen for k in keys):
            continue  # 已收录更可信的同一篇
        for k in keys:
            seen[k] = c
        out.append(c)
    return out


def build_candidates(
    queries: list[str],
    local_limit: int = 6,
    arxiv_limit: int = 8,
    crossref_limit: int = 6,
    total_limit: int = 16,
) -> list[Candidate]:
    """把多个查询词扫一遍三个来源，合并去重后返回候选池。

    返回的每篇都带 ``key``（``C1``…``Cn``），模型后续只能引用这些 key。
    这是「候选集约束生成」的入口，池子里的每一条都**真实存在**。
    """
    pool: list[Candidate] = []
    for q in queries:
        pool.extend(search_local(q, limit=local_limit))
        pool.extend(search_arxiv(q, limit=arxiv_limit))
        pool.extend(search_crossref(q, limit=crossref_limit))

    merged = _dedupe(pool)
    # 本库优先，其余按来源顺序；同来源内保持检索顺序（arXiv/Crossref 已按相关度排）
    rank = {SOURCE_LOCAL: 0, SOURCE_ARXIV: 1, SOURCE_CROSSREF: 2}
    merged.sort(key=lambda c: (rank.get(c.source, 9), -c.score))
    merged = merged[:total_limit]

    for i, c in enumerate(merged, start=1):
        c.key = f"C{i}"
    return merged