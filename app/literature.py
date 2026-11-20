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
from app.papers import DATA_DIR

logger = get_logger(__name__)

CATALOG_FILE = DATA_DIR / "catalog.json"
# catalog.json 只记了 authors_count（计数），没有名单；作者名在 structures.json 里。
# 1.6.1 生成参考文献时要写作者，所以得从这里补。
STRUCTURES_FILE = DATA_DIR / "meta" / "structures.json"
CACHE_DIR = DATA_DIR / "meta" / "online"
CACHE_TTL = 60 * 60 * 6  # 6 小时：检索结果变化慢，缓存能挡掉重复演示的等待

ARXIV_API = "http://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
# OpenAlex 礼貌池标识。带 mailto 的请求限速宽很多，用域名占位避免写进个人信息。
OPENALEX_MAILTO = "campus-agent@example.org"
USER_AGENT = "campus-agent/0.1 (research demo; contact: local)"
TIMEOUT = 20.0

# 检索结果来源标记。前端直接显示这些词，别改。
SOURCE_LOCAL = "local"
SOURCE_ARXIV = "arxiv"
SOURCE_CROSSREF = "crossref"
SOURCE_OPENALEX = "openalex"

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
    #: 本库论文的正文证据块（见 app/writing_evidence.py）。原文完整保留，
    #: 只在渲染 prompt 时截断。外面检索的论文这里是空列表。
    evidence: list[dict] = field(default_factory=list)
    #: 这篇是怎么进池的。正常检索为 ``{"kind": "retrieval"}``；
    #: 由正文证据补进来的带命中 query 与门统计，供审计。
    entry_reason: dict = field(default_factory=dict)

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
    """标题归一化，用于跨来源判重：只保留字母数字与汉字，全小写。

    **必须保留汉字**。旧实现是 ``re.sub(r"[^0-9a-z]+", "", ...)``，纯中文标题
    归一化后得到空字符串，而 ``_dedupe`` 会把空 key 过滤掉（``if k``），于是
    《关于拟阵基图一个猜想的证明》这类中文文献的判重键全为空 —— 同一篇论文
    被检索两次就进池两次，最终参考文献里出现 C1/C7/C9 三条一模一样的条目。
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _unligature(text or "").lower())


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


_ARXIV_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of for from with without
    in on at to into onto by as is are was were be been being do does did
    based via using use towards toward about over under between among
    approach approaches method methods model models paper papers study
    new novel towards system systems framework frameworks
    """.split()
)


_API_EXPR_RE = re.compile(r'[a-z]+:[^\s]|"')
"""判断查询是不是已经拼好的 arXiv 检索表达式（含字段前缀或引号）。

``_arxiv_expr`` 见到它就原样透传，不再按空格切词——切词会把
``all:"citation graph"`` 拆成 ``all:"citation`` 和 ``graph"``，表达式必空。
"""


def _arxiv_expr(q: str) -> str:
    """把查询词拼成 arXiv 检索表达式。

    旧实现是 ``all:"{q}"``（整句当短语），实测对多词查询近乎必空——
    ``all:"citation graph based paper recommendation"`` 返回 0 条，而
    ``ti:"citation" AND abs:"recommendation"`` 能命中。arXiv 的 ``all:`` 只认单 token，
    加引号即要求整句连续出现，长查询必然打空。

    现在的拼法：过滤停用词后取前 5 个实词，**第一个词限定标题**（保证主题对得上），
    其余词用 ``all:`` 放开字段（摘要/标题/作者都算），交给 ``sortBy=relevance`` 排序。
    实测：``retrieval augmented generation for academic paper recommendation`` 命中 5 篇，
    ``attention mechanism`` 命中 5 篇。

    如果拿来的是**已经拼好的布尔表达式**（调用方 ``_english_expr`` 产出的
    ``all:"RAG" AND all:"citation graph"``），原样透传：按空格切词会把
    ``all:"citation`` 和 ``graph"`` 切成两半，拼出来的表达式在 arXiv 上必空。
    """
    if _API_EXPR_RE.search(q):
        return q

    words = [
        w
        for w in re.split(r"[\s,;，、]+", q)
        if len(w) >= 3 and w.lower() not in _ARXIV_STOPWORDS
    ][:5]
    if not words:
        # 全是停用词/短词时退回裸词，不加引号（加了必空）
        return " ".join(w for w in re.split(r"\s+", q) if w) or f'all:"{q}"'
    clauses = [f'ti:"{words[0]}"'] + [f'all:"{w}"' for w in words[1:]]
    return " AND ".join(clauses)


def search_arxiv(query: str, limit: int = 10) -> list[Candidate]:
    """调 arXiv Atom API 检索。失败返回空列表（调用方退回本库结果）。"""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"arxiv_{q}_{limit}"
    if (cached := _read_cache(cache_key)) is not None:
        return [Candidate(**row) for row in cached]

    rows = _arxiv_get(_arxiv_expr(q), limit)
    if not rows:
        # 概念 AND 里只要有一个词在论文里不出现就全灭（实测加 all:"hallucination"
        # 直接从 5 条变 0 条），所以打空时逐级放宽：3 个概念 → 2 个 → 1 个。
        # 只放宽我们自己拼的布尔表达式，普通关键词查询原样返回（上面已经查过了）。
        for relaxed in _relax_api_expr(_arxiv_expr(q)):
            rows = _arxiv_get(relaxed, limit)
            if rows:
                logger.info("arXiv 查询打空，已放宽为 %s", relaxed)
                break
    if not rows:
        return []

    _write_cache(cache_key, [c.to_dict() for c in rows])
    return rows


def _arxiv_get(expr: str, limit: int) -> list[Candidate]:
    """单次 arXiv 请求。失败/非 200 一律返回空列表，由调用方决定怎么降级。"""
    params = {
        "search_query": expr,
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
        return _parse_arxiv(resp.text)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("arXiv 检索失败: %s", exc)
        return []


def _relax_api_expr(expr: str) -> list[str]:
    """把 ``A AND B AND C`` 逐级放宽成 ``A AND B``、``A``。

    只处理调用方拼好的整段 ``all:"..."`` 表达式；普通关键词（``ti:"x" AND all:"y"``）
    也照样子句切开重拼，但**至少保留 2 个概念**时才降级——1 个宽泛的
    ``all:"retrieval"`` 会把整个候选池冲成泛 RAG 论文，比空池更糟。
    """
    parts = [p.strip() for p in expr.split(" AND ") if p.strip()]
    if len(parts) < 2:
        return []
    return [" AND ".join(parts[:n]) for n in range(len(parts) - 1, 1, -1)]


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


# --------------------------------------------------------------------------- OpenAlex


def search_openalex(query: str, limit: int = 8) -> list[Candidate]:
    """调 OpenAlex ``/works`` 做自然语言检索（第四个来源）。

    为什么加它：arXiv 的检索语法是「字段 + 布尔」，一个概念里有一个词对不上就全灭；
    实测加个 ``hallucination`` 直接从 5 条变 0 条。OpenAlex 的 ``search`` 参数是
    **全文自然语言检索**，由它自己做相关度排序，不会因为多一个词就打空。
    （Semantic Scholar 的 ``paper/search`` 本来是首选，但匿名调用实测持续 429，
    见 2026-11-11 记录，所以不用它。）

    ``abstract_inverted_index`` 是「词 → 出现位置列表」的倒排结构，
    要还原成正常语序才能当摘要用，见 ``_openalex_abstract``。

    失败一律返回空列表：这是补漏来源，挂了不能影响 arXiv/Crossref 的结果。
    """
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"openalex_{q}_{limit}"
    if (cached := _read_cache(cache_key)) is not None:
        return [Candidate(**row) for row in cached]

    params = {
        "search": q,
        "per-page": max(1, min(limit, 20)),
        "select": (
            "id,doi,title,publication_year,authorships,primary_location,"
            "cited_by_count,abstract_inverted_index"
        ),
        # OpenAlex 的礼貌池：带邮箱的请求进「polite pool」，限速更宽。
        # 这里不写死成真实邮箱，用项目域名占位，避免把个人信息写进源码。
        "mailto": OPENALEX_MAILTO,
    }
    try:
        with _client() as client:
            resp = client.get(OPENALEX_API, params=params)
        if resp.status_code != 200:
            logger.warning("OpenAlex 检索返回 %s", resp.status_code)
            return []
        rows = _parse_openalex(resp.json())
    except (httpx.HTTPError, OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("OpenAlex 检索失败: %s", exc)
        return []

    _write_cache(cache_key, [c.to_dict() for c in rows])
    return rows


def _openalex_abstract(inverted: dict | None) -> str:
    """把 OpenAlex 的倒排摘要还原成正常语序。

    结构长这样：``{"Naive": [0], "Retrieval-Augmented": [1], "Generation": [2], ...}``
    ——词是键，值是该词出现的位置列表（同一个词出现多次就有多个位置）。
    按位置排序再把词拼回去就是原文。
    """
    if not inverted:
        return ""
    slots: dict[int, str] = {}
    for word, positions in inverted.items():
        for pos in positions or []:
            slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots))


def _parse_openalex(payload: dict) -> list[Candidate]:
    out: list[Candidate] = []
    for item in payload.get("results") or []:
        title = _clean(item.get("title") or item.get("display_name") or "")
        if not title:
            continue
        authors: list[str] = []
        for a in item.get("authorships") or []:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)
        year = item.get("publication_year")
        if not isinstance(year, int):
            year = None
        # DOI 在 OpenAlex 里是完整链接形式（https://doi.org/10.xxxx），
        # 而 Candidate.doi 的约定是裸 DOI，两条路都要能认
        doi = str(item.get("doi") or "")
        if doi.lower().startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/") :]
        venue = ""
        loc = item.get("primary_location") or {}
        if isinstance(loc, dict):
            src = loc.get("source") or {}
            if isinstance(src, dict):
                venue = _clean(src.get("display_name") or "")
        out.append(
            Candidate(
                key="",
                title=title,
                authors=authors[:8],
                year=year,
                source=SOURCE_OPENALEX,
                doi=doi,
                venue=venue,
                abstract=_clean(_openalex_abstract(item.get("abstract_inverted_index"))),
                url=f"https://doi.org/{doi}" if doi else (item.get("id") or ""),
            )
        )
    return out


# --------------------------------------------------------------------------- 汇总


def _dedupe(rows: list[Candidate]) -> list[Candidate]:
    """跨来源判重：同一篇只留一个。

    优先级：本库 > arXiv > Crossref > OpenAlex。理由是本库条目能跳转到阅读页看原文，
    信息最完整；Crossref 的标题偶尔带期刊后缀，最不可靠；OpenAlex 只做补漏，
    和 arXiv 重叠最多（arXiv 里的论文 OpenAlex 大多也收了），所以排最后。

    注意本库内部**也有重复文件**：``Attention Is All You Need`` 在库里存了
    ``1706.03762v1.pdf`` 和 ``1706.03762v7.pdf`` 两份（版本不同）。
    判重时按归一化标题 + arXiv 基础号双键，命中就只保留第一个。
    """
    rank = {
        SOURCE_LOCAL: 0,
        SOURCE_ARXIV: 1,
        SOURCE_CROSSREF: 2,
        SOURCE_OPENALEX: 3,
    }
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


_RRF_K = 60


def _rrf_fuse(per_query: list[list[Candidate]]) -> list[Candidate]:
    """把「每个查询各召回一列」按 RRF（倒数排名融合）合成一列。

    为什么不能直接拼接后再截断（旧做法）：拼接会把第一个查询整列放在前面，
    ``merged[:total_limit]`` 于是被第一个查询的「大路货」占满，后面几个查询
    即使命中了更贴题的小众论文也进不了池子。实测一个「引用图谱约束 + 多智能体校验」
    的 idea，英语翻译出的三个词里第一个 ``retrieval-augmented generation`` 就吃掉了
    arXiv 的 8 个名额（全是 Ragas、FAIR-RAG 这类泛 RAG 论文），而真正相关
    「引用图谱」的 CG-RAG 一条都进不来。

    RRF 只看「在各自列表里排第几」，不看来源和绝对分数，所以每个查询都有机会
    靠前——排第一的论文哪怕只被一个查询召回，也能压过被所有查询排第八的泛论文。
    这里和 RAG 检索链路用的是同一套思路（``app/rag/pipeline.py`` 的 _RRF_K），
    但两侧数据源不同，不共用常量。

    同一篇在多个查询里出现时分数累加——这是 RRF 的性质：多路一致召回说明更相关。
    """
    scores: dict[str, float] = {}
    first: dict[str, Candidate] = {}
    for column in per_query:
        for idx, c in enumerate(column, start=1):
            ident = (c.arxiv_id or "").strip() or _norm_title(c.title) or c.title
            if not ident:
                continue
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (_RRF_K + idx)
            first.setdefault(ident, c)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [first[ident] for ident, _ in ordered]


def _concept_terms(text: str) -> list[str]:
    """从 idea 原文里挑出「必须是复合概念」的词，供 ``_covered`` 判断覆盖。

    带连字符的缩写（ ``RAG``、``LoRA`` ）和两个词的英文组合（ ``citation graph`` ）
    都是强信号：论文标题里出现它们，基本可以确定同一主题。中文的「引用图谱」
    「检索增强」这类四字词同理——单独一个字太泛，连起来才有区分度。
    """
    terms: list[str] = []
    for m in re.findall(r"[A-Za-z]{2,}(?:[\s\-]+[A-Za-z]{2,})+|[A-Z][A-Za-z]{3,}", text or ""):
        t = m.strip()
        if len(t) >= 3 and t.lower() not in _GENERIC_EN:
            terms.append(t)
    for m in re.findall(r"[\u4e00-\u9fff]{4,}", text or ""):
        if m not in terms:
            terms.append(m)
    return terms


_ZH_CONCEPT_MAP = {
    "引用": ("citation", "cite", "reference"),
    "图谱": ("graph", "network"),
    "检索增强": ("retrieval", "rag"),
    "增强生成": ("generation", "rag"),
    "多智能体": ("multi-agent", "agent"),
    "智能体": ("agent",),
    "校验": ("verif", "check", "validat"),
    "幻觉": ("hallucinat",),
    "学术": ("academic", "scholar"),
    "问答": ("question answering", "qa", "answer"),
    "长文档": ("long document", "long-document"),
    "摘要": ("summar",),
    "推荐": ("recommend",),
    "知识图谱": ("knowledge graph",),
}

_GENERIC_EN = frozenset(
    {"retrieval", "generation", "augmented", "system", "model", "large", "language"}
)


def _covered(term: str, title_blobs: list[str]) -> bool:
    """一个 idea 里的概念词，在候选池里有没有**单独一篇**论文落到。

    P1 改动：旧实现把所有候选的标题+摘要+作者拼成一个大字符串再查，
    于是「池子里任意一篇标题含 retrieval」就等于「检索增强方向已覆盖」——
    16 篇里几乎每篇都含 retrieval，导致这个函数恒返回 True，
    ``pool_coverage_gaps`` 从来没报出过缺口（实测返回值一直是 ``[]``）。

    现在逐个候选单独判断，命中任意一篇才算覆盖。
    """
    if re.search(r"[\u4e00-\u9fff]", term):
        # 中文概念词在英文标题里不可能出现，翻成对应的英文片段再查
        frags: list[str] = []
        for zh_frag, en_frags in _ZH_CONCEPT_MAP.items():
            if zh_frag in term:
                frags.extend(en_frags)
        frags.append(term.lower())
        return any(any(f in title for f in frags) for title in title_blobs)

    words = [w for w in re.split(r"[\s\-]+", term.lower()) if len(w) >= 3]
    if not words:
        return False
    # 复合概念（citation graph）要求所有词落在**同一篇**标题里，
    # 而不是「citation 在 A 篇、graph 在 B 篇」就算覆盖
    return any(all(w in title for w in words) for title in title_blobs)


def pool_coverage_gaps(idea: str, pool: list[Candidate]) -> list[str]:
    """找出 idea 里哪些概念在候选池中**没有**对应文献。

    用途：把这个缺口如实告诉模型，它才不会为了凑引用去硬挂编号
    （实测正是这样：池子里没有引用图谱方向的论文，模型就把《RAG 检索失败分析》
    挂到「混合专家式稀疏化」这种完全无关的论断上）。

    判据只认**标题**，且必须**同一篇**标题里同时命中概念的所有词：
    摘要太泛（几乎每篇 RAG 论文摘要里都有 retrieval / graph），拿它判覆盖等于不判。
    代价是可能误报——把模型本可以引的方向也标成「无文献」。
    但误报的后果是模型改写措辞（安全），漏报的后果是模型硬挂编号（有害），
    所以这里宁严不宽，与旧注释里「宁可漏报」的取舍相反，理由如上。
    """
    if not pool:
        return []
    titles = [(c.title or "").lower() for c in pool if c.title]
    if not titles:
        return []
    return [t for t in _concept_terms(idea) if not _covered(t, titles)]


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 标题相关的判词下限：太短的词（单字母、两个字母）在标题里几乎必然命中，
# 等于把门槛废掉。英文取 3 个字符起（attention / roberta），中文取 2 字起（注意力）。
_RELEVANCE_MIN_LATIN = 3
_RELEVANCE_MIN_CJK = 2
# 判「标题命中」时词条的最短字符数。比检索词下限更严：``overhead`` / ``cost``
# 这类通用词到处都是，单独命中不能作为「相关」的证据（见 ``_keep_relevant``）。
_RELEVANCE_HIT_MIN_CHARS = 6
# 图表 / 补充材料的片段标题，例如 ``Table 5: Computational overhead comparison.``、
# ``Figure 2: Sparse matrix multiplication...``、``Supplemental Information 13: ...``。
# 它们被 Crossref 当成独立"论文"收录，标题里查询词一个不少，但是正文里的表号，
# 不是文献。``_keep_relevant`` 一刀切掉，判据与 terms 无关。
_FRAGMENT_TITLE_RE = re.compile(
    r"^\s*(table|figure|fig\.?|supplemental|supplementary)\b[^:]{0,40}?\d+", re.IGNORECASE
)
# 「指标修饰词」：出现在查询里时，说明这条查询描述的是某个侧面而不是研究主题。
# 见 ``_is_modifier_phrase``——这类查询不能当主题词去给别的列放行。
_RELEVANCE_MODIFIERS = frozenset(
    {
        "overhead", "cost", "costs", "latency", "speed", "speedup", "time",
        "memory", "efficiency", "efficient", "performance", "accuracy", "quality",
        "size", "scale", "scaling", "budget", "resource", "resources",
    }
)
# 表达「有一篇 / 问一个问题 / 文档」这种结构性含义的泛词。它们几乎出现在
# 每篇论文标题里（``long document`` 出现在任何长文本论文上），留着当命中词
# 等于把门槛降到「标题里有个常见词就算相关」。实测这批中文噪声正是靠
# ``question`` / ``document`` 混进池子的。
_RELEVANCE_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "these", "those",
        "using", "based", "via", "into", "toward", "towards", "their", "its",
        "are", "was", "were", "have", "has", "been", "can", "may", "not",
        "new", "novel", "large", "small", "more", "less", "very",
        "paper", "study", "approach", "method", "methods", "framework", "system",
        "question", "answering", "answer", "document", "documents", "long",
        "short", "text",
    }
)


def _split_camel(text: str) -> list[str]:
    """把 camelCase / PascalCase 拆开：``NativeSparseAttention`` → 三个词。

    必须在 ``lower()`` **之前**拆，否则大写边界就没了。
    连续大写（``LLM``）不拆，连同后面的小写一并算一个块。
    """
    parts: list[str] = []
    for chunk in re.split(r"[^0-9A-Za-z]+", text):
        if not chunk:
            continue
        parts.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", chunk) or [chunk])
    return parts


def _has_cjk(text: str) -> bool:
    """是否含汉字。含汉字的查询不送 Crossref/OpenAlex（它们不做中文分词）。"""
    return bool(_CJK_RE.search(text or ""))


def _relevance_terms(query: str) -> list[str]:
    """把查询词切成用于「标题至少命中一个」判断的词条。

    先按 camelCase 拆，再按非字母数字切分，丢掉太短的词与泛词。
    含汉字的片段保留为整块（中文没有词边界，硬切只会切出噪声）。
    """
    # 去掉 arXiv 的布尔关键字与引号，否则 all / and 这类词会变成"命中词"
    text = re.sub(r'\b(all|and|or|ti|abs|au)\b|[:"]', " ", query or "")
    out: list[str] = []
    generic: list[str] = []
    for raw in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text):
        if not raw:
            continue
        if _CJK_RE.search(raw):
            if len(raw) >= _RELEVANCE_MIN_CJK:
                out.append(raw.lower())
            continue
        for piece in _split_camel(raw):
            low = piece.lower()
            if len(low) < _RELEVANCE_MIN_LATIN:
                continue
            if low in _RELEVANCE_STOPWORDS:
                generic.append(low)
                continue
            out.append(low)
    # 整条查询全是泛词时（``long document question answering``），门槛没有可判定词，
    # 此时退回泛词本身——有门槛总比没有强，它至少还要求标题里出现 ``document``。
    if not out:
        out = generic
    return list(dict.fromkeys(out))


def _is_modifier_phrase(terms: list[str]) -> bool:
    """查询是不是「名词 + 修饰词」结构，例如 ``computational overhead``、``compute cost``。

    这类短语描述的是**某个指标的侧面**，不是研究主题本身：拿它去 Crossref/OpenAlex
    检索，回的是任何领域里提到这个指标的论文（``Cryptography with constant
    computational overhead``、``Table 5: Computational overhead comparison.``）。
    所以它只能当查询用，不能当「主题词」去给别的列放行。
    """
    return any(t in _RELEVANCE_MODIFIERS for t in terms)


def _keep_relevant(
    rows: list[Candidate],
    terms: list[str],
    topic_terms: list[str] | None = None,
) -> list[Candidate]:
    """只留下标题里命中了**足够多**查询词、且**贴着主题**的结果。

    只用于 Crossref/OpenAlex 这类「一定回满 rows 条」的词袋源；
    本库与 arXiv 走真检索，不过这道门槛。

    三层判据，从宽到严依次收口（每一层都有实测依据）：

    0. **图表片段标题一律丢弃**（``Table 5: ...`` / ``Figure 2: ...`` /
       ``Supplemental Information 13: ...``）。它们是正文里的表号，不是文献，
       却能把查询词命中得一个不差。这一层与 ``terms`` 无关，先于其余各层执行。
    1. **命中数 >= 词条数的一半（至少 1 个）**。只要求命中一个时，
       ``computational overhead`` 会把 ``Cryptography with constant computational
       overhead``（MPC 领域的固定说法）捞进稀疏注意力的候选池。
    2. **至少一个命中词是「够具体」的长词**。``overhead`` / ``cost`` 这类短通用词
       到处都是，单独命中不算证据。
    3. **标题里必须出现主题词**（``topic_terms``，即 idea 原始检索词里的英文词）。
       这一层专门挡「查询词自身就偏、于是整列一起跑偏」的情况：实测
       ``computational overhead`` 这一列在 Crossref 回的是
       ``Table 5: Computational overhead comparison.`` 这类图表标题、
       在 OpenAlex 回的是 MPC / 密码学论文、在 arXiv 回的是 ``Low overhead
       quantum computation using lattice surgery`` 这类量子计算论文——它们每一个
       词都命中，但整列与稀疏注意力毫无关系。要求标题里出现 ``sparse`` 或
       ``attention`` 之后，这一列才被清空，而 ``sparse attention`` /
       ``long document question answering`` 两列不受影响。

    判据只看标题：摘要太泛（几乎每篇 ML 论文摘要里都有 model / attention）。
    """
    strong = [t for t in terms if len(t) >= _RELEVANCE_HIT_MIN_CHARS] or terms
    need = max(1, (len(strong) + 1) // 2)
    topic = [t for t in (topic_terms or []) if len(t) >= _RELEVANCE_HIT_MIN_CHARS]
    kept: list[Candidate] = []
    for c in rows:
        title = (c.title or "").lower()
        if _FRAGMENT_TITLE_RE.match(title):
            continue
        if strong and sum(1 for t in strong if t in title) < need:
            continue
        if topic and not any(t in title for t in topic):
            continue
        kept.append(c)
    return kept


def build_candidates(
    queries: list[str],
    local_limit: int = 6,
    arxiv_limit: int = 8,
    crossref_limit: int = 6,
    openalex_limit: int = 8,
    total_limit: int = 16,
) -> list[Candidate]:
    """把多个查询词扫一遍四个来源，合并去重后返回候选池。

    返回的每篇都带 ``key``（``C1``…``Cn``），模型后续只能引用这些 key。
    这是「候选集约束生成」的入口，池子里的每一条都**真实存在**。

    每个查询各自召回一列，再按 RRF 融合（见 ``_rrf_fuse``），而不是先拼接后截断——
    拼接会让第一个查询的名额挤掉后面查询里更贴题的小众论文。

    **中文查询不送 Crossref/OpenAlex**。这两个源都不做中文分词，把整块中文当
    字面词袋硬配：实测「稀疏注意力降低长文档问答中」与「计算开销」两个完全不同的
    中文查询，Crossref 与 OpenAlex 返回的是**同一批**、与内容毫不相干的文献
    （《水文计算在小型水库除险加固设计中需注意的问题》《小学低年级学生注意力
    问题的成因与干预策略》之类）。这些噪声经 RRF 融合后名次不低，会把英文查询
    命中的真论文挤出 16 个名额——实测同一 idea 只用英文查询跑，前 12 名里是
    FlashAttention / Native Sparse Attention / XAttention 这类正主。
    所以中文查询只走本库（字面匹配）与 arXiv，Crossref/OpenAlex 这两个词袋源跳过；
    英文查询三条路照旧。

    **再给所有词袋源的结果加一道相关性门槛**：标题里一个查询词都找不到就整篇
    丢掉。Crossref/OpenAlex 只要不报错就一定回满 ``rows`` 条，与查询有多相关无关，
    不设门槛等于把数据源的召回上限当成「相关」。arXiv 也按关键词词袋排序，同样会
    被 ``computational overhead`` 带回整列量子计算论文，所以一并过门槛。三层判据
    见 ``_keep_relevant``：图表片段标题直接丢、命中数过半、修饰短语列还要求贴主题。
    本库 ``search_local`` 是真字面匹配（有命中分数），不设门槛。

    **不做更细的标题相关度过滤**：曾试过「标题必须含整串查询词」与「标题必须含
    查询词的全部单词」，实测两条路都不通——整串匹配时查询词常是长词组
    （``retrieval augmented generation for``），归一化后
    ``retrievalaugmentedgenerationfor`` 与标题里的 ``FAIR-RAG``、
    ``Engineering the RAG Stack`` 一个字都匹配不上，正经结果被全部误杀；
    全单词匹配时多词查询几乎必然落空。所以门槛只取「至少命中一个」。
    """
    per_query: list[list[Candidate]] = []
    # 主题词只取**单概念短查询**（1~2 个词条、且不是 ``X overhead`` 这种修饰结构）
    # 里的具体词。长查询通常是模型拼出来的偏短语，把它当主题词等于承认这条偏查询
    # 自己就是主题——实测那样一整列 MPC 密码学论文照样能混进来。
    topic_terms: list[str] = []
    for q in queries:
        if _has_cjk(q):
            continue
        q_terms = _relevance_terms(q)
        if 1 <= len(q_terms) <= 2 and not _is_modifier_phrase(q_terms):
            topic_terms.extend(t for t in q_terms if len(t) >= _RELEVANCE_HIT_MIN_CHARS)
    topic_terms = list(dict.fromkeys(topic_terms))

    for q in queries:
        column: list[Candidate] = []
        column.extend(search_local(q, limit=local_limit))
        # Crossref/OpenAlex 不做中文分词，中文词送过去只会换回一批泛结果
        if not _has_cjk(q):
            terms = _relevance_terms(q)
            # 只有「指标修饰短语」列才额外要求贴主题。``long document question
            # answering`` 是 idea 的另一个侧面，它列的论文标题里不会出现
            # ``sparse`` / ``attention``，拿主题词去卡会整列杀光——实测 Crossref
            # 那 6 条真论文（Structure-Aware Quantized Retrieval for Long-Document
            # Question Answering 等）全部被误杀。
            gate_topic = topic_terms if _is_modifier_phrase(terms) else None
            # arXiv 虽是「真检索」，但它按关键词词袋排序：``computational overhead``
            # 同样会回整列量子计算论文（``Low overhead quantum computation using
            # lattice surgery`` 等 5 条，实测占了 16 个名额里的 5 个）。凡按词袋
            # 匹配的源都要过门槛。
            column.extend(
                _keep_relevant(search_arxiv(q, limit=arxiv_limit), terms, gate_topic)
            )
            column.extend(
                _keep_relevant(search_crossref(q, limit=crossref_limit), terms, gate_topic)
            )
            column.extend(
                _keep_relevant(search_openalex(q, limit=openalex_limit), terms, gate_topic)
            )
        else:
            # 中文查询只走本库与 arXiv；arXiv 对中文基本 0 命中，但保留通路
            column.extend(search_arxiv(q, limit=arxiv_limit))
        if column:
            # 列内先去重：同一篇在 arXiv 和 Crossref 都出现时，
            # 不能算成两个名次，否则它的 RRF 分数会凭空翻倍
            per_query.append(_dedupe(column))

    fused = _rrf_fuse(per_query)
    # 排序只看 RRF 分数——这是改过的地方。旧做法是「本库优先，其余按来源排」，
    # 结果是本库 5 篇 + arXiv 前 8 篇就把 16 个名额吃满，而 arXiv 那 8 篇是按
    # 单个查询自己的相关性排的，泛 RAG 论文（Ragas、FAIR-RAG）挤掉了排在
    # 第 9 位的 CG-RAG 这类真正贴题的。RRF 分数已经综合了多路召回的排名，
    # 比「来源 + 单路名次」更可靠，所以让它主导，来源只在分数相同时兜底。
    #
    # fused 已按 RRF 分数降序，用下标当分数即可（下标越小分越高）。
    rank = {
        SOURCE_LOCAL: 0,
        SOURCE_ARXIV: 1,
        SOURCE_CROSSREF: 2,
        SOURCE_OPENALEX: 3,
    }
    order = {id(c): i for i, c in enumerate(fused)}
    # 两趟排序：先按来源（稳定排序会把来源优先级保留为同 RRF 名次时的次序），
    # 再按 RRF 名次
    merged = sorted(fused, key=lambda c: rank.get(c.source, 9))
    merged = sorted(merged, key=lambda c: order[id(c)])
    merged = merged[:total_limit]

    for i, c in enumerate(merged, start=1):
        c.key = f"C{i}"
    return merged