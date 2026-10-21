"""论文库：存论文、列论文、删论文，并判断「这篇是不是已经传过」。

为什么单独一个模块，而不是塞进 rag/pipeline.py：
    - 「管文件」和「搜内容」是两件事，混在一起以后加功能会互相牵连；
    - 去重需要「读文字 + 比对相似度」，属于文件管理，不属于向量检索；
    - 测试时可以用假文字替换真实 PDF，不依赖 400MB 的样例数据。

索引为什么用 JSON 而不是数据库：
    这是 40 篇论文的量级，一个 index.json 足够；换成 MySQL 只是把
    _load_index / _save_index 两个函数重写，调用方一行不用改。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.logging_config import get_logger

logger = get_logger(__name__)

# 项目根目录：本文件在 app/ 下，往上一级就是仓库根
BASE_DIR = Path(__file__).resolve().parent.parent
PAPERS_DIR = BASE_DIR / "data" / "papers"
INDEX_FILE = PAPERS_DIR / "index.json"
TEXT_DIR = PAPERS_DIR / "text"  # 抽出来的文字缓存，供查重使用

MAX_SIZE = 30 * 1024 * 1024  # 30MB，和赛题里「很长的书」留出余量
SUPPORTED = (".pdf",)

# 查重的相似度阈值：两篇论文前 3 页文字的「二字组重合度」超过它，就当成同一篇
DUP_THRESHOLD = 0.80


@dataclass
class Paper:
    """一篇论文在索引里的样子。字段就是前端列表要展示的东西。"""

    paper_id: str          # 短哈希，用于文件名和接口路径
    filename: str          # 用户上传时的原始文件名
    size: int              # 字节数
    pages: int             # 页数（解析失败记 0）
    uploaded_at: str       # 上传时间（ISO 格式字符串）
    duplicate_of: str = ""  # 若被判重，填库中已存在那篇的 paper_id


# ---------------------------------------------------------------- 索引读写


def _ensure_dirs() -> None:
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> list[Paper]:
    if not INDEX_FILE.exists():
        return []
    try:
        raw = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("index.json 损坏，按空库处理: %s", INDEX_FILE)
        return []
    return [Paper(**item) for item in raw]


def _save_index(papers: list[Paper]) -> None:
    _ensure_dirs()
    # 内存里永远保持「新的在前」，写盘时也按这个顺序
    INDEX_FILE.write_text(
        json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 文本抽取与查重


def _extract_text(path: Path, max_pages: int = 3) -> str:
    """抽前 max_pages 页文字。只抽前几页是性能考虑：查重不需要读完整本书。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as exc:  # 加密/损坏/扫描件都可能走到这里
        logger.warning("抽取文字失败 %s: %s", path.name, exc)
        return ""


def _normalize(text: str) -> str:
    """去掉空白和标点、转小写，让同一段文字的细微排版差异不影响比对。"""
    return re.sub(r"[\s\W_]+", "", text.lower())


def _grams(text: str, n: int = 2) -> set[str]:
    """把文字切成 n 字组集合。中文用二字组，英文用三字母组会更好，这里统一 2。"""
    clean = _normalize(text)
    if not clean:
        return set()
    return {clean[i : i + n] for i in range(max(0, len(clean) - n + 1))} or {clean}


def similarity(text_a: str, text_b: str) -> float:
    """Jaccard 相似度：交集 / 并集，取值 0~1。

    为什么不用完全相等：v1 和 v7 是同一篇论文的不同版本，文字一定不完全一样。
    用「字面重合度」能在没有模型的情况下把「同一篇的两次投稿」认出来。
    """
    a, b = _grams(text_a), _grams(text_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_duplicate(text: str) -> tuple[Paper | None, float]:
    """拿新论文的文字，和库里每篇的缓存文字比一遍，返回最像的那篇和分数。"""
    best: Paper | None = None
    best_score = 0.0
    for paper in _load_index():
        cached = _cached_text(paper.paper_id)
        if not cached:
            continue
        score = similarity(text, cached)
        if score > best_score:
            best, best_score = paper, score
    if best is not None and best_score >= DUP_THRESHOLD:
        return best, round(best_score, 3)
    return None, round(best_score, 3)


def _cached_text(paper_id: str) -> str:
    path = TEXT_DIR / f"{paper_id}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------- 对外功能


def list_papers() -> list[Paper]:
    """列出所有论文，新的在前。"""
    return _load_index()


def get_paper(paper_id: str) -> Paper | None:
    for paper in _load_index():
        if paper.paper_id == paper_id:
            return paper
    return None


def paper_path(paper_id: str) -> Path | None:
    """返回论文原件的磁盘路径；索引里有但文件丢了就返回 None。"""
    paper = get_paper(paper_id)
    if paper is None:
        return None
    path = PAPERS_DIR / f"{paper_id}{Path(paper.filename).suffix.lower()}"
    return path if path.exists() else None


def save_paper(
    filename: str,
    content: bytes,
    text_extractor=lambda path: _extract_text(path),
) -> tuple[Paper, Paper | None, float]:
    """保存一篇上传的 PDF。

    返回 (新论文, 若重复则命中已有论文, 相似度分数)。

    流程：校验 → 落盘 → 抽前几页文字 → 查重 → 记索引。
    重复不拦截，而是「照样存、但标记 duplicate_of」，让用户自己决定留哪版
    —— 赛题原文就是这么要求的（提示用户选择保留哪个版本）。
    """
    if not filename.lower().endswith(SUPPORTED):
        raise ValueError("只支持 PDF")
    if len(content) > MAX_SIZE:
        raise ValueError(f"文件超过 {MAX_SIZE // 1024 // 1024}MB")

    _ensure_dirs()

    # 用一个短哈希做 id：同内容不同文件名也能得到相同 id（幂等）
    paper_id = hashlib.sha256(content).hexdigest()[:12]
    safe_name = Path(filename).name

    dest = PAPERS_DIR / f"{paper_id}.pdf"
    dest.write_bytes(content)

    text = text_extractor(dest)
    cached = TEXT_DIR / f"{paper_id}.txt"
    cached.write_text(text, encoding="utf-8")

    # 先把新内容从索引里排除（同一个文件重复上传时，不该跟自己比）
    existing = [p for p in _load_index() if p.paper_id != paper_id]
    best, score = _find_duplicate(text)

    pages = 0
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(dest)).pages)
    except Exception:
        pages = 0

    paper = Paper(
        paper_id=paper_id,
        filename=safe_name,
        size=len(content),
        pages=pages,
        uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        duplicate_of=best.paper_id if best else "",
    )

    _save_index([paper, *existing])
    logger.info(
        "保存论文 %s (%s, %d 页, 重复=%s)",
        paper_id,
        safe_name,
        pages,
        paper.duplicate_of or "否",
    )
    return paper, best, score


def delete_paper(paper_id: str) -> bool:
    """删除论文原件、文字缓存和索引项。返回是否真的删掉了。"""
    remaining = [p for p in _load_index() if p.paper_id != paper_id]
    if len(remaining) == len(_load_index()):
        return False

    for path in (PAPERS_DIR / f"{paper_id}.pdf", TEXT_DIR / f"{paper_id}.txt"):
        path.unlink(missing_ok=True)
    _save_index(remaining)
    return True
