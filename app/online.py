"""网上抓取：arXiv 搜索/元数据/PDF 与 Semantic Scholar 的引用关系。

为什么这么写（都是本机实测出来的约束，别改回去）：

1. 本机的 ``HTTP_PROXY`` 指向一个没开着的代理（127.0.0.1:65532），httpx 默认会读环境变量，
   于是所有请求直接失败。这里统一 ``trust_env=False`` 并显式清掉代理变量。
2. arXiv 官方的 Atom API（export.arxiv.org）在本机连不通（15 秒超时），但 ``arxiv.org``
   主站是通的：搜索页 ``/search/``、详情页 ``/abs/``、PDF ``/pdf/`` 都能访问。
   所以搜索走 HTML 解析，不依赖 Atom API（也就不用装 sickle）。
3. Semantic Scholar 的 ``/paper/search`` 持续 429（共享 IP 限流），但
   ``/paper/arXiv:<id>/references`` 与 ``/citations`` 可用。所以引用关系用它，搜索不用它。
4. 搜索页一次要 20 秒上下，S2 又限流，所以每类请求都落盘缓存；
   拿不到网的时候调用方读缓存，页面显示"上次刷新时间"，不至于白屏。
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.logging_config import get_logger
from app.papers import BASE_DIR

logger = get_logger(__name__)

CACHE_DIR = BASE_DIR / "data" / "meta" / "online"
ARXIV_BASE = "https://arxiv.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
USER_AGENT = "campus-agent/0.1 (research demo; contact: local)"
TIMEOUT = 30.0

SEARCH_TTL = timedelta(days=7)
META_TTL = timedelta(days=30)
CITE_TTL = timedelta(days=3)
RECENT_TTL = timedelta(days=1)

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})")
_GITHUB_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
_TAG_RE = re.compile(r"<[^>]+>")


class OnlineError(RuntimeError):
    """联网失败（超时、限流、解析不出结果）。调用方应退回缓存。"""


def _client() -> httpx.Client:
    """造一个不吃系统代理的客户端。"""
    return httpx.Client(
        timeout=TIMEOUT,
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _strip(html: str) -> str:
    """去掉标签并压掉空白（arXiv 页面里作者是 <a> 连着逗号的）。"""
    text = _TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- 缓存


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", key).strip("_")[:120] or "item"
    return CACHE_DIR / f"{safe}.json"


def read_cache(key: str, ttl: timedelta) -> Any | None:
    """读缓存；超过 ttl 视为过期，返回 None。"""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    saved_at = str(payload.get("saved_at") or "")
    try:
        saved = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=UTC)
    if datetime.now(UTC) - saved > ttl:
        return None
    return payload.get("data")


def read_cache_any_age(key: str) -> Any | None:
    """读缓存，不管过期——断网时用来兜底。"""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("data")
    except (OSError, ValueError):
        return None


def write_cache(key: str, data: Any) -> None:
    """写缓存（原子替换，避免半截文件）。"""
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "data": data}
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def cache_time(key: str) -> str:
    """缓存写入时间（给人看的字符串），没有就空串。"""
    path = _cache_path(key)
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("saved_at") or "")
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------- arXiv


def arxiv_id_of(text: str) -> str:
    """从任意文本（标题、文件名、URL）里抠出 arXiv 编号，抠不到返回空串。"""
    match = _ARXIV_ID_RE.search(text or "")
    return match.group(1) if match else ""


def find_github(*texts: str) -> str:
    """从若干段文字里找 GitHub 仓库地址；跳过明显的占位符。"""
    for text in texts:
        for match in _GITHUB_RE.finditer(text or ""):
            url = match.group(0).rstrip(".,);]")
            tail = url.split("github.com/", 1)[-1]
            owner, _, repo = tail.partition("/")
            if owner.lower() in {"xxx", "yourname", "username", "user", "example"}:
                continue
            if repo.lower() in {"xxx", "repo", "your-repo", "repository"}:
                continue
            return url
    return ""


GITHUB_API = "https://api.github.com/search/repositories"
GITHUB_TTL = timedelta(days=14)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOP_TOKENS = {"the", "and", "for", "with", "from", "using", "via", "based", "towards", "into"}


def _tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall((text or "").lower()) if tok not in _STOP_TOKENS}


def github_repo(title: str) -> str:
    """GitHub 搜索兜底：摘要里没写仓库地址时，按标题找仓库。

    赛题 1.4.3 要求"是否包含 GitHub 代码，有代码优先"。实测不少论文摘要不写链接
    （``find_github()`` 找不到），但仓库名往往就是论文名，所以再查一次搜索接口。
    未认证的搜索接口限制 10 次/分钟，因此：只查标题、每次缓存 14 天、
    标题关键词与仓库名/描述重合度不到 60% 就当没找到（宁可不给，也别给错的）。
    """
    title = (title or "").strip()
    if len(title) < 8:
        return ""
    key = f"github_search_{title[:90]}"
    cached = read_cache(key, GITHUB_TTL)
    if cached is not None:
        return str(cached or "")
    target = _tokens(title)
    # 少于 3 个关键词的标题不做（"Layer Normalization" 这种会误配到 RMSNorm 仓库：
    # 它的描述里同样有 layer + normalization，但根本不是同一篇论文的代码）
    if len(target) < 3:
        return ""
    found = ""
    try:
        with _client() as client:
            resp = client.get(
                GITHUB_API,
                params={"q": f'"{title[:64]}" in:name,description', "per_page": 5},
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                raise OnlineError(f"GitHub 搜索返回 {resp.status_code}")
            rows = (resp.json() or {}).get("items") or []
    except (httpx.HTTPError, ValueError, OnlineError) as exc:
        logger.info("GitHub 搜索失败：%s", exc)
        return ""
    for row in rows:
        name = str(row.get("full_name") or "")
        desc = str(row.get("description") or "")
        hit = len(target & _tokens(name + " " + desc))
        name_hit = len(target & _tokens(name))
        # 双条件：关键词要基本全覆盖（≥80%），且仓库名本身也要对上一半以上
        if hit / max(1, len(target)) >= 0.8 and name_hit / max(1, len(target)) >= 0.5:
            found = f"https://github.com/{name}"
            break
    write_cache(key, found)
    return found


def search_papers(title: str, limit: int = 6) -> list[dict[str, Any]]:
    """按标题在 arXiv 上搜，返回 [{arxiv_id, title, url, submitted}]。

    注意 arXiv 的"按标题搜"并不精确：搜 "Layer Normalization" 的第一条可能是
    别的论文，所以结果要由调用方再做一次标题相似度挑选，不能盲取第一条。
    """
    title = (title or "").strip()
    if not title:
        return []
    key = f"arxiv_search_{title}"
    cached = read_cache(key, SEARCH_TTL)
    if cached is not None:
        return list(cached)[:limit]

    url = f"{ARXIV_BASE}/search/"
    params = {"query": title, "searchtype": "title", "size": "25"}
    try:
        with _client() as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError as exc:  # 网络问题一律降级
        fallback = read_cache_any_age(key)
        if fallback is not None:
            logger.warning("arXiv 搜索失败，改用过期缓存：%s", exc)
            return list(fallback)[:limit]
        raise OnlineError(f"连不上 arXiv 搜索：{exc}") from exc

    results = _parse_search(html)
    if not results:
        raise OnlineError("arXiv 搜索没有返回结果（可能是页面结构变了或关键词太窄）")
    write_cache(key, results)
    return results[:limit]


def _parse_search(html: str) -> list[dict[str, Any]]:
    """解析 arXiv 搜索结果页。"""
    results: list[dict[str, Any]] = []
    for block in re.split(r'<li class="arxiv-result"', html)[1:]:
        id_match = re.search(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", block)
        title_match = re.search(r'<p class="title is-5 mathjax">(.*?)</p>', block, re.S)
        if not id_match or not title_match:
            continue
        submitted = ""
        sub_match = re.search(r"Submitted</span>\s*([^;<]+)", block)
        if sub_match:
            submitted = _strip(sub_match.group(1))
        results.append(
            {
                "arxiv_id": id_match.group(1),
                "title": _strip(title_match.group(1)),
                "url": f"{ARXIV_BASE}/abs/{id_match.group(1)}",
                "submitted": submitted,
            }
        )
    return results


def recent_papers(
    categories: tuple[str, ...] = ("cs.CL", "cs.LG"),
    per_page: int = 100,
    days: int = 14,
) -> list[dict[str, Any]]:
    """抓 arXiv 各分类的「recent」列表，返回最近 ``days`` 天内上架的论文。

    为什么还要这条路：只靠引用关系，刚发出来、还没人引用的论文永远进不来；
    但赛题 1.4.3 要的是"可能与库里论文相关的新论文"，所以再扫一遍新上架列表，
    用主题相似度筛。列表页结构是 ``<dt>编号</dt><dd>标题</dd>``，日期在 ``<h3>``。
    """
    out: dict[str, dict[str, Any]] = {}
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    for category in categories:
        key = f"arxiv_recent_{category}"
        cached = read_cache(key, RECENT_TTL)
        if cached is None:
            url = f"{ARXIV_BASE}/list/{category}/recent"
            try:
                with _client() as client:
                    response = client.get(url, params={"skip": "0", "show": str(per_page)})
                    response.raise_for_status()
                    items = _parse_recent(response.text)
            except httpx.HTTPError as exc:
                fallback = read_cache_any_age(key)
                if fallback is None:
                    logger.warning("抓 %s 新上架失败：%s", category, exc)
                    continue
                items = list(fallback)
            else:
                write_cache(key, items)
        else:
            items = list(cached)
        for item in items:
            posted = item.get("posted") or ""
            try:
                when = datetime.strptime(posted, "%a, %d %b %Y").replace(tzinfo=UTC)
            except ValueError:
                when = None
            if when is not None and when < cutoff:
                continue
            row = dict(item)
            row["category"] = category
            out.setdefault(row["arxiv_id"], row)
    return list(out.values())


def _parse_recent(html: str) -> list[dict[str, Any]]:
    """解析 arXiv ``/list/{cat}/recent`` 页面：``<dt>`` 编号 + ``<dd>`` 标题 + ``<h3>`` 日期。"""
    # 日期段：<h3>Tue, 27 Oct 2026 (showing ...)</h3>，位置在紧跟其后的条目之前
    marks: list[tuple[int, str]] = []
    for match in re.finditer(r"<h3>([^<]+)</h3>", html):
        label = match.group(1).strip()
        date = label.split("(")[0].strip()
        marks.append((match.start(), date))

    results: list[dict[str, Any]] = []
    for match in re.finditer(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", html, re.S):
        head, body = match.group(1), match.group(2)
        id_match = re.search(r'href\s*=\s*"/abs/([0-9]{4}\.[0-9]{4,5})"', head)
        title_match = re.search(r"list-title mathjax['\"]>(.*?)</div>", body, re.S)
        if not id_match or not title_match:
            continue
        title = _strip(title_match.group(1))
        # 页面上标题前面有个 <span class='descriptor'>Title:</span>，_strip 之后可能带换行/空格
        title = re.sub(r"^\s*(?:Title|标题)\s*[:：]\s*", "", title, flags=re.I).strip()
        posted = ""
        for pos, date in marks:
            if pos < match.start():
                posted = date
        results.append(
            {
                "arxiv_id": id_match.group(1),
                "title": title,
                "posted": posted,
                "url": f"{ARXIV_BASE}/abs/{id_match.group(1)}",
            }
        )
    return results


def paper_meta(arxiv_id: str) -> dict[str, Any]:  # noqa: C901 - 字段多，拆开反而更难读
    """取 arXiv 详情页的标题/作者/摘要/提交日期/GitHub 地址。"""
    arxiv_id = arxiv_id_of(arxiv_id) or (arxiv_id or "").strip()
    if not arxiv_id:
        raise OnlineError("没有 arXiv 编号")
    key = f"arxiv_abs_{arxiv_id}"
    cached = read_cache(key, META_TTL)
    if cached is not None:
        return dict(cached)

    try:
        with _client() as client:
            response = client.get(f"{ARXIV_BASE}/abs/{arxiv_id}")
            response.raise_for_status()
            html = response.text
    except httpx.HTTPError as exc:
        fallback = read_cache_any_age(key)
        if fallback is not None:
            logger.warning("arXiv 详情页失败，改用过期缓存 %s：%s", arxiv_id, exc)
            return dict(fallback)
        raise OnlineError(f"连不上 arXiv 详情页：{exc}") from exc

    def pick(pattern: str) -> str:
        match = re.search(pattern, html, re.S)
        return _strip(match.group(1)) if match else ""

    title = pick(r'<h1 class="title mathjax">(.*?)</h1>')
    # abs 页标题同样带 <span class="descriptor">Title:</span>
    title = re.sub(r"^\s*(?:Title|标题)\s*[:：]\s*", "", title, flags=re.I)
    authors_raw = pick(r'<div class="authors">(.*?)</div>')
    authors_raw = re.sub(r"^Authors:\s*", "", authors_raw)
    authors = [a.strip() for a in authors_raw.split(",") if a.strip()]
    abstract = pick(r'<blockquote class="abstract mathjax">(.*?)</blockquote>')
    abstract = re.sub(r"^Abstract:\s*", "", abstract)
    dateline = pick(r'<div class="dateline">(.*?)</div>')
    comments = pick(r'Comments:</span>(.*?)</td>') or pick(r"Comments:\s*(.*?)</td>")
    subjects = pick(r'<td class="tablecell subjects">(.*?)</td>')
    github = find_github(abstract, comments, title)
    # dateline 形如 "Submitted on 27 Oct 2026"；年份用来算新鲜度，解析不到就是 0
    year_match = re.search(r"(?:19|20)\d{2}", re.sub(r"\[v\d+\].*$", "", dateline))
    year = int(year_match.group(0)) if year_match else 0

    meta = {
        "arxiv_id": arxiv_id,
        "title": title or f"arXiv:{arxiv_id}",
        "authors": authors,
        "abstract": abstract,
        "published": dateline,
        "year": year,
        "comments": comments,
        "subjects": subjects,
        "github": github,
        "url": f"{ARXIV_BASE}/abs/{arxiv_id}",
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_cache(key, meta)
    return meta


def download_pdf(arxiv_id: str, dest: Path) -> Path:
    """把 arXiv 的 PDF 下到 dest（调用方负责命名与去重）。"""
    arxiv_id = arxiv_id_of(arxiv_id) or (arxiv_id or "").strip()
    if not arxiv_id:
        raise OnlineError("没有 arXiv 编号")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    try:
        with _client() as client:
            with client.stream("GET", f"{ARXIV_BASE}/pdf/{arxiv_id}") as response:
                response.raise_for_status()
                size = 0
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(65536):
                        size += len(chunk)
                        handle.write(chunk)
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise OnlineError(f"下载 PDF 失败：{exc}") from exc
    if size < 10_000:
        tmp.unlink(missing_ok=True)
        raise OnlineError(f"下载到的 PDF 只有 {size} 字节，不像论文")
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------- Semantic Scholar


def _s2(path: str, params: dict[str, Any], key: str, ttl: timedelta) -> Any:
    """调 Semantic Scholar，带限流退避；失败时退回过期缓存。"""
    cached = read_cache(key, ttl)
    if cached is not None:
        return cached
    last_error: Exception | None = None
    for _attempt, wait in enumerate((0.0, 3.0, 8.0)):
        if wait:
            time.sleep(wait)
        try:
            with _client() as client:
                response = client.get(f"{S2_BASE}{path}", params=params)
            if response.status_code == 429:
                last_error = OnlineError("Semantic Scholar 限流（429）")
                continue
            response.raise_for_status()
            data = response.json()
            write_cache(key, data)
            return data
        except httpx.HTTPError as exc:
            last_error = exc
    fallback = read_cache_any_age(key)
    if fallback is not None:
        logger.warning("Semantic Scholar 失败，改用过期缓存 %s：%s", key, last_error)
        return fallback
    raise OnlineError(f"Semantic Scholar 请求失败：{last_error}")


def citations(arxiv_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """谁引用了这篇论文（用来找"最近大量引用库里论文"的新论文）。"""
    arxiv_id = arxiv_id_of(arxiv_id)
    if not arxiv_id:
        return []
    data = _s2(
        f"/paper/arXiv:{arxiv_id}/citations",
        {"fields": "title,year,externalIds,abstract,authors", "limit": limit},
        f"s2_citations_{arxiv_id}",
        CITE_TTL,
    )
    out: list[dict[str, Any]] = []
    for item in data.get("data") or []:
        paper = item.get("citingPaper") or {}
        out.append(_paper_row(paper))
    return [row for row in out if row["arxiv_id"] or row["title"]]


def references(arxiv_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """这篇论文引用了谁（用来批量下载参考文献）。"""
    arxiv_id = arxiv_id_of(arxiv_id)
    if not arxiv_id:
        return []
    data = _s2(
        f"/paper/arXiv:{arxiv_id}/references",
        {"fields": "title,year,externalIds,abstract,authors", "limit": limit},
        f"s2_references_{arxiv_id}",
        CITE_TTL,
    )
    out: list[dict[str, Any]] = []
    for item in data.get("data") or []:
        paper = item.get("citedPaper") or {}
        out.append(_paper_row(paper))
    return [row for row in out if row["arxiv_id"] or row["title"]]


def _paper_row(paper: dict[str, Any]) -> dict[str, Any]:
    """把 S2 的 paper 对象压成我们用的字段。"""
    ids = paper.get("externalIds") or {}
    arxiv_id = str(ids.get("ArXiv") or "").strip()
    authors = [str(a.get("name") or "") for a in (paper.get("authors") or []) if a.get("name")]
    return {
        "arxiv_id": arxiv_id,
        "title": str(paper.get("title") or "").strip(),
        "year": int(paper.get("year") or 0),
        "abstract": str(paper.get("abstract") or "").strip(),
        "authors": authors[:6],
        "url": f"{ARXIV_BASE}/abs/{arxiv_id}" if arxiv_id else "",
    }


# ------------------------------------------------------------------- 引用频次


def citation_counts(text: str) -> dict[int, int]:
    """数正文里每个引用编号出现了几次：``[12]``、``[3, 5]``、``[7–9]`` 都算。

    PDF 里一篇文章的"重点引用"往往是反复被提到的那些；只出现一次的多半是
    "为了引用而引用"。这里给出计数，让上层按阈值筛。
    """
    counts: dict[int, int] = {}
    for match in re.finditer(r"\[([0-9][0-9,;–\-\s]*)\]", text or ""):
        body = match.group(1)
        for part in re.split(r"[,;]", body):
            part = part.strip()
            if not part:
                continue
            if "–" in part or "-" in part:
                sep = "–" if "–" in part else "-"
                left, _, right = part.partition(sep)
                if left.strip().isdigit() and right.strip().isdigit():
                    low, high = int(left), int(right)
                    if 0 < high - low <= 30:
                        for num in range(low, high + 1):
                            counts[num] = counts.get(num, 0) + 1
                continue
            if part.isdigit():
                num = int(part)
                if 0 < num <= 500:
                    counts[num] = counts.get(num, 0) + 1
    return counts
