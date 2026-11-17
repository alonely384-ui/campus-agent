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
import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.logging_config import get_logger

logger = get_logger(__name__)

# 项目根目录：本文件在 app/ 下，往上一级就是仓库根
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据根目录。默认就在仓库的 data/ 下；打包成 exe 时 __file__ 指向 PyInstaller
# 的临时解压目录，那里只读、而且每次启动都会重建，索引和 PDF 放不进去，所以
# 桌面版会设 DATA_DIR 指到用户目录。不设时行为与原来完全一致。
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data")).resolve()

PAPERS_DIR = DATA_DIR / "papers"
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
    # 判重的依据：「文本相似度」还是「同一 arXiv 编号」。扫描件抽不出字，
    # 只能靠编号认亲，所以要把依据也带出去，前端才能解释为什么说它重复。
    duplicate_reason: str = ""


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


_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})")
_VERSION_RE = re.compile(r"v(\d+)(?:\D|$)")


def arxiv_id_of(*texts: str) -> str:
    """从文件名或正文里找 arXiv 编号（如 1706.03762）。

    为什么需要：扫描件 `1706.03762v1_img.pdf` 抽不出一个字，文本相似度恒为 0，
    但它和 `1706.03762v1.pdf` 明明是同一篇。编号是这类情况的唯一可靠信号。
    """
    for text in texts:
        match = _ARXIV_ID_RE.search(text or "")
        if match:
            return match.group(1)
    return ""


def version_of(*texts: str) -> int:
    """从文件名 / 编号里取版本号 vN；取不到返回 0。"""
    for text in texts:
        match = _VERSION_RE.search(text or "")
        if match:
            return int(match.group(1))
    return 0


def _find_duplicate(
    text: str,
    filename: str = "",
    exclude_id: str = "",
) -> tuple[Paper | None, float, str]:
    """拿新论文的文字（和文件名）跟库里比一遍，返回 (最像的一篇, 分数, 依据)。

    两条信号：
    1. 文本相似度 ≥ DUP_THRESHOLD —— 同篇不同版本、改了几个词的重复；
    2. 同一 arXiv 编号 —— 覆盖扫描件（没有文字）与「v1 和 v7」这种版本关系。
    第二条命中时把分数抬到阈值之上，保证调用方只需要看分数。
    """
    new_arxiv = arxiv_id_of(filename, text[:4000])
    best: Paper | None = None
    best_score = 0.0
    best_reason = ""
    for paper in _load_index():
        if paper.paper_id == exclude_id:
            continue
        cached = _text_for(paper)
        score = similarity(text, cached) if cached else 0.0
        reason = "文本相似度" if score >= DUP_THRESHOLD else ""
        old_arxiv = arxiv_id_of(paper.filename, cached[:4000])
        if new_arxiv and old_arxiv == new_arxiv:
            # 编号相同但文字不像（典型是扫描件，抽不出字）：只在这种情况下降级用编号当依据
            if score < DUP_THRESHOLD:
                reason = "同一 arXiv 编号的不同版本"
            score = max(score, DUP_THRESHOLD)
        if score > best_score:
            best, best_score, best_reason = paper, score, reason
    if best is not None and best_score >= DUP_THRESHOLD:
        return best, round(best_score, 3), best_reason
    return None, round(best_score, 3), ""


def _cached_text(paper_id: str) -> str:
    path = TEXT_DIR / f"{paper_id}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _text_for(paper: Paper) -> str:
    """取一篇论文的前几页文字，没有缓存就抽一次并存上。

    老数据（直接放进 data/papers 的 39 篇样例）从来没有文字缓存，
    首次查重时补抽一次，之后就走缓存。
    """
    cached = _cached_text(paper.paper_id)
    if cached:
        return cached
    path = PAPERS_DIR / f"{paper.paper_id}.pdf"
    if not path.exists():
        path = PAPERS_DIR / paper.filename
    if not path.exists():
        return ""
    text = _extract_text(path)
    if text:
        TEXT_DIR.mkdir(parents=True, exist_ok=True)
        (TEXT_DIR / f"{paper.paper_id}.txt").write_text(text, encoding="utf-8")
    return text


def _page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return 0


# PDF 头：规范允许 `%PDF-` 前有少量垃圾字节，所以只在文件开头 1KB 内找
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_SCAN = 1024


class PdfInvalidError(ValueError):
    """上传的文件不是可用的 PDF。

    继承 ValueError 是有意的：`save_paper` 对「不合法输入」历来抛 ValueError，
    接口层已经把它转成 400，调用方不需要多认一个异常类型。
    """


def inspect_pdf(content: bytes) -> int:
    """判断一段字节是不是**完整可用**的 PDF，返回页数。

    - 空内容 → PdfInvalidError("文件是空的")
    - 文件头不是 %PDF- → PdfInvalidError("文件内容不是 PDF")
    - pypdf 打不开（截断、交叉引用坏、加密且无法解密）→ PdfInvalidError
    - 正常 → 返回页数（≥1）

    **扫描件必须判为有效**：整页是图片、没有文字层的 PDF 能正常打开，
    只是 `extract_text()` 返回空串。那种情况归 OCR 兜底处理，不在这里拦。
    所以这里只看"结构能不能打开"，不看"有没有字"。

    为什么同时信 pypdf 和 pymupdf 两家的结论：实测（2026-11-18）对
    「截断到 1/3 的 PDF」，pypdf 报 `PdfStreamError: Stream has ended
    unexpectedly`，而 pymupdf 仍能打开第 1 页。只信 pymupdf 会把那份截断文件
    当成功放进去，之后阅读页翻到第 2 页才报错，用户看到的是"上传成功但打不开"。
    """
    if not content:
        raise PdfInvalidError("文件是空的（0 字节）")
    head = content[:_PDF_MAGIC_SCAN]
    if _PDF_MAGIC not in head:
        raise PdfInvalidError("文件内容不是 PDF（缺少 %PDF- 文件头）")

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        pages = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - 见 _pdf_error_message 的分类
        raise PdfInvalidError(_pdf_error_message(type(exc).__name__, exc)) from exc

    if pages <= 0:
        raise PdfInvalidError("PDF 里没有任何一页")
    return pages


def _pdf_error_message(name: str, exc: Exception) -> str:
    """把 pypdf 的异常名翻成给用户看的一句话。"""
    if name == "EmptyFileError":
        return "文件是空的（0 字节）"
    if name == "PdfStreamError":
        return "PDF 数据不完整或已被截断，无法解析"
    if name == "InvalidPdfError":
        return f"PDF 文件头异常或已加密无法打开：{exc}"
    return f"PDF 无法解析（{name}）：{exc}"


# ---------------------------------------------------------------- 对外功能


def sync_index() -> list[Paper]:
    """让 index.json 和 data/papers 目录里真实存在的 PDF 一致。

    为什么需要：赛题给的 40 篇样例是直接拷进 data/papers 的，从来没走过上传接口，
    所以 index.json 里一条都没有。结果 `GET /papers` 只回 1 条、删除接口对它们无效，
    而 `/papers/files` 和阅读页却能看到 39 篇——三处数字对不上。
    这里把「磁盘上有、索引里没有」的补进索引，把「索引里有、磁盘上没有」的清掉。
    """
    papers = _load_index()
    known_ids = {p.paper_id for p in papers}
    structures = load_structures()
    changed = False

    for path in sorted(PAPERS_DIR.glob("*.pdf")):
        if path.name.startswith("."):
            # `.download_xxx.pdf` 这类临时文件也会被 glob 匹配到（Python 的 glob 不过滤
            # 隐藏文件）。它在下载中途存在，一旦被当论文登记就成了"幽灵论文"，
            # 还会让真正入库的那份被判成重复。
            continue
        if path.stem in known_ids:
            continue
        meta = structures.get(path.name) or {}
        pages = _page_count(path) or int(meta.get("pages") or 0)
        paper = Paper(
            paper_id=path.stem,
            filename=path.name,
            size=path.stat().st_size,
            pages=pages,
            uploaded_at=time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)
            ),
        )
        # 补登记时顺手标一下重复：同 arXiv 编号的另一份拷贝（样例库里那对 v1/v7）
        arxiv = arxiv_id_of(path.name)
        if arxiv:
            for other in papers:
                if arxiv_id_of(other.filename) == arxiv:
                    paper.duplicate_of = other.paper_id
                    paper.duplicate_reason = "同一 arXiv 编号的不同版本"
                    break
        papers.append(paper)
        known_ids.add(path.stem)
        changed = True
        logger.info("索引里补上磁盘论文 %s", path.name)

    kept = [
        p
        for p in papers
        if (PAPERS_DIR / f"{p.paper_id}.pdf").exists() or (PAPERS_DIR / p.filename).exists()
    ]
    if len(kept) != len(papers):
        changed = True

    # 指向"已经不存在的 paper_id"的重复标记要清掉：历史上下载临时文件曾被登记成论文，
    # 真正入库的那份于是记着 `duplicate_of=".download_xxx"`，界面上就显示成"重复"，
    # 但那个 paper_id 根本不在库里（也删不掉），用户只会觉得是 bug。
    kept_ids = {p.paper_id for p in kept}
    for paper in kept:
        if paper.duplicate_of and paper.duplicate_of not in kept_ids:
            paper.duplicate_of = ""
            paper.duplicate_reason = ""
            changed = True

    # 历史条目可能是在还没有查重逻辑时登记的（样例库那对 v1/v7 就是），
    # 这里按 arXiv 编号补一次标记：同编号里挑一个"正版"（文件名就是 paper_id 的、
    # 或者上传更早的），其余标成重复，前端列表才不会满屏"重复"却不知道重复谁。
    groups: dict[str, list[Paper]] = {}
    for paper in kept:
        arxiv = arxiv_id_of(paper.filename)
        if arxiv:
            groups.setdefault(arxiv, []).append(paper)
    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = min(
            group,
            key=lambda p: (p.paper_id != Path(p.filename).stem, p.uploaded_at),
        )
        for paper in group:
            desired = "" if paper is canonical else canonical.paper_id
            if paper.duplicate_of == desired:
                continue
            paper.duplicate_of = desired
            paper.duplicate_reason = "" if not desired else "同一 arXiv 编号的不同版本"
            changed = True

    if changed:
        _save_index(kept)
    return kept


def list_papers() -> list[Paper]:
    """列出所有论文，新的在前。顺手让索引和磁盘对齐。"""
    return sync_index()


def get_paper(paper_id: str) -> Paper | None:
    for paper in sync_index():
        if paper.paper_id == paper_id:
            return paper
    return None


def paper_path(paper_id: str) -> Path | None:
    """返回论文原件的磁盘路径；索引里有但文件丢了就返回 None。

    两种命名都可能：上传的存成 `<paper_id>.pdf`，样例/手动拷进来的保留原名。
    """
    paper = get_paper(paper_id)
    if paper is None:
        return None
    candidates = [
        PAPERS_DIR / f"{paper_id}{Path(paper.filename).suffix.lower()}",
        PAPERS_DIR / paper.filename,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def structure_path(paper_id: str) -> Path:
    return PAPERS_DIR / f"{paper_id}.structure.json"


# 结构化产物（标题/作者/章节树）放 data/meta/ —— 它是要入库的「数据字典」，
# 不是能随时重下的大文件；data/papers/ 整个目录被 .gitignore 排除，放在那里
# 换台机器就没有章节树了。旧位置保留成兜底，老环境不用手动搬。
META_DIR = DATA_DIR / "meta"
DEFAULT_STRUCTURES_FILE = META_DIR / "structures.json"
LEGACY_STRUCTURES_FILE = PAPERS_DIR / "structures.json"
STRUCTURES_FILE = DEFAULT_STRUCTURES_FILE


def structures_file() -> Path:
    """当前该读/写哪个结构文件：新位置优先，没迁移的老环境读旧位置。

    STRUCTURES_FILE 是模块级变量，测试会 monkeypatch 它；被改过就不是默认路径，
    必须原样用（不能让测试误读写仓库里的真实数据）。
    """
    if STRUCTURES_FILE != DEFAULT_STRUCTURES_FILE or STRUCTURES_FILE.exists():
        return STRUCTURES_FILE
    return LEGACY_STRUCTURES_FILE if LEGACY_STRUCTURES_FILE.exists() else STRUCTURES_FILE


def load_structures() -> dict[str, dict]:
    """按文件名索引的结构化元数据缓存（由 scripts/build_structure.py 生成）。"""
    path = structures_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_structures(data: dict[str, dict]) -> Path:
    """写回结构文件；老环境第一次写就顺手迁到新位置，读和写不会分裂成两份。"""
    path = structures_file()
    if path == LEGACY_STRUCTURES_FILE:
        path = DEFAULT_STRUCTURES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def paper_meta(source: str) -> dict | None:
    """按文件名（如 1706.03762v7.pdf）取标题/作者/章节树。"""
    return load_structures().get(source)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def find_paper_by_title(question: str) -> str | None:
    """问题里点名了某篇论文的标题时，返回该论文文件名。

    为什么需要：多篇混在一个库里时，"第六作者是谁"这种问题本身不含论文名，
    检索会随便命中一篇。只要用户在问句里带了标题（"In Attention Is All You
    Need, who is ..."），就先锁定论文，再在单篇内检索。
    """
    q = set(_norm_words(question))
    q_text = " ".join(_norm_words(question))
    for source, meta in load_structures().items():
        title = (meta.get("title") or "").strip()
        if not title or len(_norm_words(title)) < 2:
            continue
        t_words = _norm_words(title)
        # 标题原样出现，或标题里 ≥80% 的词都出现在问句里，算点名
        if " ".join(t_words) in q_text or sum(1 for w in t_words if w in q) >= max(2, int(len(t_words) * 0.8)):
            return source
    return None


def save_structure(paper_id: str, structure: dict) -> None:
    """把标题/作者/章节树存成旁挂 JSON（不进主索引，老数据无需迁移）。"""
    structure_path(paper_id).write_text(
        json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_structure(paper_id: str) -> dict | None:
    path = structure_path(paper_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def merge_structure(filename: str, structure: dict) -> bool:
    """把一篇论文的结构写进总表（阅读页按文件名查，所以键是文件名）。

    新下载/新上传的论文只有旁挂 JSON，不合并进 structures.json 的话，
    阅读页查不到章节树（``reader_meta`` 就是按文件名查的总表）。
    """
    if not structure:
        return False
    data = load_structures()
    data[filename] = structure
    save_structures(data)
    return True


def save_paper(
    filename: str,
    content: bytes,
    text_extractor=lambda path: _extract_text(path),
    structure_parser=None,
) -> tuple[Paper, Paper | None, float]:
    """保存一篇上传的 PDF。

    返回 (新论文, 若重复则命中已有论文, 相似度分数)。

    流程：校验 → 落盘 → 抽前几页文字 → 查重 → 记索引。
    重复不拦截，而是「照样存、但标记 duplicate_of」，让用户自己决定留哪版
    —— 赛题原文就是这么要求的（提示用户选择保留哪个版本）。

    **校验放在落盘之前**（2026-11-18 修）：以前只查后缀和大小，一份 24 字节的
    假 PDF 也能 200 保存成功、pages=0 入库，之后阅读、检索、图谱全线拿它没辙，
    而用户看到的是"上传成功"。现在先用 `inspect_pdf()` 确认它真是能打开的 PDF，
    不合格直接抛 `PdfInvalidError`（ValueError 子类），一个字节都不落盘。

    ``structure_parser`` 默认是 `app.structure.parse_structure`；测试可以传
    一个假函数，免得为了验"半成品清理"而真的去解析 PDF。
    """
    if not filename.lower().endswith(SUPPORTED):
        raise ValueError("只支持 PDF")
    if len(content) > MAX_SIZE:
        raise ValueError(f"文件超过 {MAX_SIZE // 1024 // 1024}MB")
    # 空文件 / 假 PDF / 截断 / 交叉引用坏：都在这里拦住，不进磁盘
    pages = inspect_pdf(content)

    _ensure_dirs()

    # 用一个短哈希做 id：同内容不同文件名也能得到相同 id（幂等）
    paper_id = hashlib.sha256(content).hexdigest()[:12]
    safe_name = Path(filename).name

    dest = PAPERS_DIR / f"{paper_id}.pdf"
    cached = TEXT_DIR / f"{paper_id}.txt"
    side_json = structure_path(paper_id)
    # 合并进总表之前的**快照**，回滚用它精确还原（见 _rollback_saved）。
    # 记 None 表示"还没碰过总表"，和"碰过、但原来没有这一篇"要区分开。
    structures_before: dict[str, dict] | None = None
    dest.write_bytes(content)
    try:
        text = text_extractor(dest)
        cached.write_text(text, encoding="utf-8")

        # 结构化解析：标题/作者/章节树存成旁挂文件（data/papers/<id>.structure.json）。
        # 失败只记日志，不影响上传和检索——这是"附加信息"，不是主流程。
        try:
            if structure_parser is None:
                from app.structure import parse_structure

                structure_parser = parse_structure
            structure = structure_parser(str(dest))
            if structure.get("title") or structure.get("authors"):
                save_structure(paper_id, structure)
                # 旁挂 JSON 只是备份，阅读页与 /paper/{f}/structure 查的是
                # data/meta/structures.json 那张按文件名索引的总表。不合并进去，
                # 上传进来的论文就永远没有章节树——下载链路（/library/download）
                # 一直是显式合并的，这里以前漏了。
                #
                # 合并前先拍快照（2026-11-18 修）：以前这里没接 merge_structure
                # 的返回值，structure_merged 恒为 False，回滚里的清理分支是死的——
                # 总表写成功、索引写失败时，PDF 和旁挂 JSON 都被删了，
                # data/meta/structures.json 里那条却留了下来，成了指向不存在文件的
                # 幽灵记录。现在整份旧表存下来，回滚时连"这篇新增的键"和
                # "其他论文被动过的值"一起还原。
                structures_before = load_structures()
                merge_structure(safe_name, structure)
        except Exception:  # noqa: BLE001
            logger.exception("结构化解析失败，跳过（不影响上传）")
    except Exception:
        # 走到这里说明文件已经落盘但后续步骤炸了。不清掉就留下一份
        # "磁盘上有、索引里没有"的半成品，`sync_index()` 下次启动还会把它
        # 补登记成一条 pages=0 的幽灵论文（`test_sync_index_picks_up_files_on_disk`
        # 描述的就是这个机制）。所以失败必须原样撤回，不留残余。
        _rollback_saved(paper_id, safe_name, dest, cached, side_json, structures_before)
        raise

    # 查重时把自己排除掉：同一个文件重复上传，不该跟自己比
    best, score, reason = _find_duplicate(text, filename=safe_name, exclude_id=paper_id)

    paper = Paper(
        paper_id=paper_id,
        filename=safe_name,
        size=len(content),
        pages=pages,
        uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        duplicate_of=best.paper_id if best else "",
        duplicate_reason=reason if best else "",
    )

    try:
        others = [p for p in _load_index() if p.paper_id != paper_id]
        _save_index([paper, *others])
    except Exception:
        _rollback_saved(paper_id, safe_name, dest, cached, side_json, structures_before)
        raise
    logger.info(
        "保存论文 %s (%s, %d 页, 重复=%s)",
        paper_id,
        safe_name,
        pages,
        paper.duplicate_of or "否",
    )
    return paper, best, score


def _rollback_saved(
    paper_id: str,
    filename: str,
    dest: Path,
    cached: Path,
    side_json: Path,
    structures_before: dict[str, dict] | None,
) -> None:
    """撤回一篇没保存完的论文：原件、文字缓存、旁挂结构、总表条目一起清。

    只删**这次刚写出来**的东西：原件按 paper_id 命名，天然只可能是本次写的。

    ``structures_before`` 是合并进总表**之前**的整份快照：
    - 为 None 表示这次压根没碰总表，什么都不用还原；
    - 否则把总表写回这份快照。这样"这次新增的键"会消失，"其他论文被动过的值"
      也会回到原样，不会出现只删一半的状态。

    为什么用整份快照而不是只删自己那个键：`merge_structure` 走的是
    `load_structures()` → 改 → `save_structures()` 全量重写，中间任何一步炸掉
    都可能让总表停在半截；只删自己那一条修不了"别人被改坏"的情况。
    还原只在**确实写过**时才做，所以别的论文已有的结构数据一个都不会丢。
    """
    for path in (dest, cached, side_json):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # 删不掉也不能把原始异常盖掉
            logger.warning("清理半成品失败：%s", path, exc_info=True)
    if structures_before is not None:
        try:
            save_structures(structures_before)
        except Exception:  # noqa: BLE001
            logger.warning("回滚 structures.json 失败：%s", filename, exc_info=True)
    logger.info("已撤回未完成保存的论文 %s（%s）", paper_id, filename)


def delete_structure(source: str) -> bool:
    """从 structures.json 里删掉一篇（删论文时同步，避免"三处数据不同步"）。

    structures.json 是按文件名索引的，所以传的是 filename 而不是 paper_id。
    """
    data = load_structures()
    if source not in data:
        return False
    data.pop(source)
    save_structures(data)
    return True


def delete_paper(paper_id: str) -> bool:
    """删除论文原件、文字缓存和索引项。返回是否真的删掉了。

    注意：这里只删磁盘文件与 index.json；向量库和 BM25 的清理在
    app/rag/pipeline.py 的 remove_source() 里，由 /papers 删除接口一起调用。
    """
    papers = _load_index()
    paper = next((p for p in papers if p.paper_id == paper_id), None)
    if paper is None:
        return False

    for path in (
        PAPERS_DIR / f"{paper_id}.pdf",
        PAPERS_DIR / paper.filename,
        TEXT_DIR / f"{paper_id}.txt",
        structure_path(paper_id),
    ):
        path.unlink(missing_ok=True)
    _save_index([p for p in papers if p.paper_id != paper_id])
    return True
