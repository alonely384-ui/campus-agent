"""RAG 检索增强生成——把"查资料"塞进"回答问题"之前。

流程（两段）：

    【建库，只做一次】
        PDF/TXT ──load──► 文字 ──split──► 500 字一段 ──index──► 存进 Chroma

    【问答，每次提问】
        问题 ──retrieve──► 找最像的 4 段 ──拼进 prompt──► 大模型回答 + 附来源

关于"向量"（本项目用的是最轻的一种，叫"哈希向量"）：
    把一段文字变成 1024 个数字，做法分三步：
        1. 切词：中文按"相邻两个字"（奖学金 → 奖学 / 学金），
           英文按"整个单词"（training → training，不是 tr/ra/ai）
        2. 每个特征用 crc32 算编号，对 1024 取余，落到某一格，那格 +权重
        3. 整串数字归一化（除以自身长度），保证长文和短文可比
    两段话用词越接近，数字分布就越接近 → 按"用词像不像"检索。
    这不是最强的向量方案（最强的是神经网络模型），但零依赖、零下载、
    结果稳定可复现，适合先把"读取→切分→入库→检索"的闭环跑通。

面试会追问的三个问题：
    1. 为什么不用关键词搜索（SQL 的 LIKE）？—— "怎么申请奖学金"和"助学金流程"
       一个字都不一样，但意思相关；向量按"意思像不像"找，不按"字面有没有"找。
    2. 检索出来的片段不相关时怎么办？—— 这就是"拒答"策略，宁可说不知道。
    3. chunk_size 为什么定这个值？—— 因为你测过，有数字。
"""

import json
import math
import os
import re
import zlib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.logging_config import get_logger
from app.rag.rerank import RerankUnavailable
from app.rag.rerank import rerank as rerank_fn

logger = get_logger(__name__)


@dataclass
class Chunk:
    """一个文本块。"""

    text: str
    source: str      # 来自哪个文件
    page: int | None = None
    chunk_id: int = 0


# 向量的维度：1024 格。格子越多，不同特征撞到同一格的概率越小。
# 注意：Chroma 的集合一旦建成，维度就固定；改 DIM 必须清库重建。
DIM = 1024

# 入库时的最小块长：小于这个字数的块直接丢弃（见 index_chunks 第 0 步）。
_MIN_CHUNK_CHARS = 100

# 默认切块大小：300 字（约 2–3 句）。实测 120/150/200/300 的检索命中率在 150 之后
# 就不再提升（top3 都是 3/3），但 300 的块数只有 150 的一半（180 vs 334），
# 存储和检索成本都更低，块内也更不容易掺进无关句子。
DEFAULT_CHUNK_SIZE = 300

# 英文停用词：每篇论文都有一堆，留着会让所有文本彼此"很像"，淹没真正的关键词。
_EN_STOPWORDS = {
    "the", "a", "an", "of", "and", "to", "in", "is", "are", "was", "were",
    "for", "on", "with", "by", "as", "that", "this", "it", "we", "our",
    "from", "be", "at", "or", "which", "can", "have", "has", "had", "not",
    "but", "their", "they", "them", "all", "these", "those", "than", "then",
    "so", "if", "such", "using", "used", "use", "also", "more", "may",
    "one", "two", "each", "other", "into", "about", "over", "when", "where",
    # 疑问词与提问套话：问句里天天出现，但对"找哪一段"没有区分力
    "who", "what", "how", "why", "many", "much", "does", "did",
    "do", "paper", "please", "tell", "me", "according",
}

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")  # 连续的中文片段
_WORD = re.compile(r"[a-z0-9]+")            # 英文单词与数字（已转小写）

# 论文里的希腊字母要先换成英文名，否则会被 _WORD 直接丢掉：
# "β1 = 0.9" 里的 β 不匹配 [a-z0-9]，整段就丢了；换成 beta 才能和
# 用户提问里的 "beta1" 对上（这是实测 adam 那题 top1 错位的直接原因）。
_GREEK = str.maketrans(
    {
        "β": "beta", "α": "alpha", "γ": "gamma", "δ": "delta",
        "λ": "lambda", "μ": "mu", "σ": "sigma", "η": "eta",
        "θ": "theta", "π": "pi", "ε": "epsilon", "ϵ": "epsilon",
    }
)


# IDF 文件：记录每个特征"在多少块里出现过"，用来给常见词降权。
# 和向量库放在同一目录：库重建，IDF 也必须重建，否则权重对不上。
_IDF_FILE = "idf.json"
_IDF: dict[str, float] = {}


def _idf_path() -> str:
    return os.path.join(settings.chroma_dir, _IDF_FILE)


def _load_idf() -> dict[str, float]:
    """读 IDF 表。没建过库时返回空表，权重全为 1，退化成普通哈希向量。"""
    if not _IDF:
        try:
            with open(_idf_path(), encoding="utf-8") as f:
                _IDF.update(json.load(f))
        except (OSError, ValueError):
            pass
    return _IDF


def _save_idf(df: dict[str, int], total: int) -> dict[str, float]:
    """用文档频率算 IDF：log((N+1)/(df+1)) + 1。

    df = 该特征出现在多少个块里。越少见的词 IDF 越大（区分力越强）；
    像 training 这种几乎每块都有的词，IDF 被压到接近 1，不再抢风头。
    """
    idf = {name: math.log((total + 1) / (count + 1)) + 1.0 for name, count in df.items()}
    os.makedirs(settings.chroma_dir, exist_ok=True)
    with open(_idf_path(), "w", encoding="utf-8") as f:
        json.dump(idf, f, ensure_ascii=False)
    _IDF.clear()
    _IDF.update(idf)
    return idf


def _stem(word: str) -> str:
    """极简词形还原：training/trained→train，gpus→gpu，models→model。

    为什么需要：提问说 "training"，原文写 "We trained our models on ...
    GPUs"，不做这一步，关键词永远对不上，BM25 就找不到真正那一句。
    只处理常见后缀，且去掉后缀后至少剩 3 个字母（避免 us/use 这类误伤）。
    """
    if len(word) <= 3:
        return word
    for suffix in ("ations", "ation", "ings", "ing", "ers", "er", "ies", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            # studies → studi → study 这类，补回 y
            if suffix in ("ies", "es") and base.endswith("i"):
                base = base[:-1] + "y"
            return base
    return word


def _tokens(text: str) -> list[tuple[str, float]]:
    """把文本拆成 (特征名, 权重) 列表。

    三种特征：
        c:xx    中文 2-gram（"奖学金" → 奖学 / 学金）
        w:word  英文单词或数字（小写、去停用词、长度 ≥ 2）
        b:a_b   英文相邻两词（"nvidia_p100"），权重 0.5，用来抓专有名词短语
    """
    lowered = text.lower().translate(_GREEK)
    features: list[tuple[str, float]] = []

    # 中文：只对连续中文段做 2-gram，避免和英文混着切
    for run in _CJK_RUN.findall(lowered):
        grams = [run[i : i + 2] for i in range(len(run) - 1)] or [run]
        features.extend((f"c:{g}", 1.0) for g in grams)

    # 英文与数字
    words = [
        _stem(w)
        for w in _WORD.findall(lowered)
        if len(w) >= 2 and w not in _EN_STOPWORDS
    ]
    features.extend((f"w:{w}", 1.0) for w in words)

    # 英文词二元组：区分 "nvidia p100" 这种固定搭配
    features.extend((f"b:{a}_{b}", 0.5) for a, b in zip(words, words[1:], strict=False))

    return features


_BM25_FILE = "bm25.json"
_BM25: dict | None = None
# 三路检索的 RRF 权重（可用环境变量覆盖，做消融实验用）：
#   RAG_W_DENSE / RAG_W_HASH / RAG_W_BM25
#
# 取值优先用 config.settings（由 pydantic-settings 从 .env 读），
# 读不到才退回环境变量、最后退回代码默认值。原因见 app/config.py 顶部
# 「为什么 RAG 参数走 settings 而不直接 os.getenv」：.env 里写的值
# 进程启动时并不会自动进 os.environ，只 getenv 等于忽略用户在设置页做的修改。
def _env_setting(name: str, default: str) -> str:
    """按 环境变量 → settings 字段 → 默认值 的顺序取一个字符串配置。

    顺序必须和 pydantic-settings 一致（环境变量 > .env > 默认值）。

    为什么不能只读 os.getenv：pydantic-settings 读 .env 只是把值填进
    Settings 对象，**不会**把 .env 的键写进 os.environ。所以用户在 .env 里
    写 RAG_W_DENSE=3.25（或在设置页保存），os.getenv 是拿不到的，
    参数会静默退回代码默认值，重启后用户的修改就白改了。
    两个来源都要看，且环境变量在前。
    """
    env = os.getenv(name)
    if env is not None and env != "":
        return env

    field = name.lower()
    if field in type(settings).model_fields:
        value = getattr(settings, field, None)
        if value is not None and str(value) != "":
            return str(value)
    return default


_W_DENSE = float(_env_setting("RAG_W_DENSE", "2.0"))
_W_HASH = float(_env_setting("RAG_W_HASH", "0.5"))
_W_BM25 = float(_env_setting("RAG_W_BM25", "1.0"))
# RRF 的 k 常数（可用 RAG_RRF_K 覆盖）：越大名次差异被抹得越平，
# 越小头部名次越吃重。常见取值 60；短查询、库小的时候 20–30 有时更好。
_RRF_K = int(_env_setting("RAG_RRF_K", "60"))

# 交叉编码器重排：RAG_RERANK=0 关闭；RAG_RERANK_POOL 决定拿多少候选去重排。
# 实测 pool=10/15/20 的 R@1 分别是 28/31/30（38 题），差异在噪声内；
# 取 15 是"质量不吃亏、候选少 1/4"的折中。
_RERANK_ENABLED = _env_setting("RAG_RERANK", "1").strip().lower() not in ("0", "false", "off", "no")
_RERANK_POOL = int(_env_setting("RAG_RERANK_POOL", "15"))
# 每路召回取多深：太浅（12）会把"排第 15 的正确块"直接漏掉，重排也无从救；
# 太深受重排成本限制。50 是 38 题集上把 GPT-3 参数题捞回来的最小代价点。
_RECALL_DEPTH = int(_env_setting("RAG_RECALL_DEPTH", "50"))

# 参考文献列表片段：形如 "Author et al. Title. arXiv:2312.14925, 2023." 或 "In Proceedings of ACL"。
# 这类片段是"某论文引用过它"，不是论文正文；实测它们会抢到强化学习/对比类问题的 top1，
# 所以命中 >=2 个特征时降权，并在上下文里加提示（不是硬删，保留可追溯）。
_CITATION_RE = re.compile(
    r"\bet al\.|\barXiv:\s*\d{4}\.\d{4}|https?://|doi:|Proceedings of|NeurIPS|ICLR|ICML"
    r"|EMNLP|NAACL|ACL|CVPR|ICCV|AAAI|Advances in Neural",
    re.IGNORECASE,
)
_CITATION_PENALTY = float(_env_setting("RAG_CITATION_PENALTY", "0.6"))


def _looks_like_citation(text: str) -> bool:
    """短块 + 多个引用格式特征 → 判为参考文献条目。"""
    if len(text) > 700:
        return False
    return len(_CITATION_RE.findall(text)) >= 2

_BM25_K1 = 1.2   # 词频饱和速度：越大，重复出现的词加分越多
_BM25_B = 0.6    # 长度归一化强度：1 = 完全按长度惩罚长块


def _bm25_path() -> str:
    return os.path.join(settings.chroma_dir, _BM25_FILE)


def _build_bm25(chunks: list[Chunk]) -> dict:
    """建倒排索引：term -> [[块序号, 该词出现次数], ...]。

    只存索引不存原文（原文在 Chroma 里），文件小、检索快。
    """
    postings: dict[str, list[list[int]]] = {}
    lens: list[int] = []
    docs = [{"source": c.source, "page": c.page, "chunk_id": c.chunk_id} for c in chunks]

    for i, c in enumerate(chunks):
        counts = Counter(name for name, _w in _tokens(c.text))
        lens.append(sum(counts.values()) or 1)
        for term, tf in counts.items():
            postings.setdefault(term, []).append([i, tf])

    avg = (sum(lens) / len(lens)) if lens else 1.0
    return {"avg_len": avg, "lens": lens, "postings": postings, "docs": docs}


def _save_bm25(index: dict) -> None:
    """原子写 BM25 索引。

    为什么不能直接 open(..., "w")：那会先把原文件截断成 0 字节再写。
    写到一半进程被杀 / 磁盘满，磁盘上就只剩半截 JSON；下次启动
    _load_bm25 的 json.load 抛 ValueError，被 except 吞掉后返回 None，
    整条 BM25 召回通道就此静默消失（RAG 变成两路，且没有任何报错）。
    写临时文件再 os.replace 才能保证"要么旧索引、要么新索引"。
    """
    global _BM25
    os.makedirs(settings.chroma_dir, exist_ok=True)
    target = _bm25_path()
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        # 失败时清理半截临时文件，磁盘上的正式索引保持原样
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            logger.warning("清理 BM25 临时文件失败：%s", tmp, exc_info=True)
        raise
    _BM25 = index


def _load_bm25() -> dict | None:
    global _BM25
    if _BM25 is None:
        try:
            with open(_bm25_path(), encoding="utf-8") as f:
                _BM25 = json.load(f)
        except (OSError, ValueError):
            return None
    return _BM25


def _bm25_ids(question: str, top_k: int, source: str | None = None) -> list[str]:
    """BM25 打分，返回最相关的块 id（文本之后再从 Chroma 取）。

    BM25 = 词频 + 逆文档频率 + 长度归一化。它比"哈希向量"精确的地方在于：
    只认真正出现过的词，不会被哈希碰撞带偏。
    传 source 时只在该篇论文内部排序（单篇问答必须这么用）。
    """
    index = _load_bm25()
    if not index:
        return []

    postings = index["postings"]
    lens = index["lens"]
    n = len(lens)
    avg = index["avg_len"] or 1.0
    scores: dict[int, float] = {}

    for term in {name for name, _w in _tokens(question)}:
        entries = postings.get(term)
        if not entries:
            continue
        df = len(entries)
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for doc_id, tf in entries:
            denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * lens[doc_id] / avg)
            scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (_BM25_K1 + 1) / denom

    docs = index["docs"]
    if source:
        scores = {i: s for i, s in scores.items() if docs[i]["source"] == source}

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [f"{docs[i]['source']}:{docs[i]['chunk_id']}" for i, _s in ranked]


def embed(text: str) -> list[float]:
    """把一段文字变成 1024 维向量（哈希向量）。

    原理三步：
        1. 切词：中文按"相邻两个字"，英文按"整个单词"（都转小写、去停用词）
        2. 落格：每个特征用 crc32 算出编号，对 1024 取余，落到某一格，那格 +权重
        3. 归一化：整串数字除以自身长度，让长文和短文能直接比
    比较两段文字时用余弦相似度：夹角越小，用词越接近。

    为什么英文不能按"两个字"切：
        training 会被切成 tr/ra/ai/in/ni/in/ng，全是无意义碎片；
        GPU 和 P100 在向量里永远碰不到一起，检索只能靠虚词瞎撞。

    为什么用 crc32 而不是内置 hash()：
        hash() 每个进程换随机种子，同一句话今天和明天算出的向量不同，
        检索结果无法复现；crc32 是确定性的，同样的输入永远同样的数字。
    """
    vec = [0.0] * DIM
    idf = _load_idf()
    for name, weight in _tokens(text):
        # 次线性 TF：同一特征出现 4 次，不该是出现 1 次的 4 倍权重
        tf = 1.0 + math.log(weight)
        vec[zlib.crc32(name.encode("utf-8")) % DIM] += tf * idf.get(name, 1.0)

    length = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / length for v in vec]


def load_pdf(path: str) -> list[Chunk]:
    """[1] 读取 PDF，按页返回文本块（一页 = 一个 Chunk）。

    为什么"一页一块"而不是"一篇一块"：
        检索要定位到"哪一页哪一段"，一页一块刚好能当"引用来源"用。
    """
    from pypdf import PdfReader  # 延迟导入：只有真要读 PDF 时才需要这个库

    reader = PdfReader(path)
    source = os.path.basename(path)   # 只要文件名，别把整个路径写进来源
    chunks: list[Chunk] = []

    for page_no, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            # 扫描版 PDF（整页是图片）没有文字层：读 OCR 缓存（scripts/ocr_scanned.py 生成）
            text = _ocr_cached(str(path), page_no).strip()
        if not text:
            continue
        chunks.append(
            Chunk(text=text, source=source, page=page_no, chunk_id=len(chunks))
        )

    return chunks


def load_text(path: str) -> list[Chunk]:
    """读取 .txt / .md，按"空行"分成段落块（先拿纯文本跑通闭环）。"""
    source = os.path.basename(path)
    raw = open(path, encoding="utf-8").read()

    # \n\s*\n 匹配"一个或多个空行"：空行就是段落分隔
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]

    return [
        Chunk(text=p, source=source, page=None, chunk_id=i)
        for i, p in enumerate(paragraphs)
    ]


# 递归切分的分隔符优先级：从"最大的语义单位"排到"最小的语义单位"。
# 先用段落切，切不动就用句子，再不行用分句，最后按空格/硬切兜底。
# 顺序本身就是策略：越靠前切出来的块越完整，检索时越不容易丢上下文。
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " "]


def _recursive_split(text: str, chunk_size: int, separators: list[str]) -> list[str]:
    """按分隔符优先级把长文本拆成"不超过 chunk_size 的碎片"（这一层不做合并）。

    某一段仍然超长，就换更细的分隔符再拆（递归）。
    所有分隔符都试完还超长，才按字数硬切——这是兜底，保证不会死循环。
    """
    if len(text) <= chunk_size:
        return [text] if text else []

    for i, sep in enumerate(separators):
        if sep not in text:
            continue

        parts = text.split(sep)
        # 把分隔符粘回前一段，避免句子被拆开后丢掉标点
        pieces = [p + sep for p in parts[:-1]]
        if parts[-1]:
            pieces.append(parts[-1])

        out: list[str] = []
        for piece in pieces:
            if len(piece) <= chunk_size:
                out.append(piece)
            else:
                out.extend(_recursive_split(piece, chunk_size, separators[i + 1 :]))
        return out

    # 兜底：一个分隔符都没有（比如整页是公式或英文没空格），按字数硬切
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


_TABLE_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _is_table_row(line: str) -> bool:
    """判断一行是否表格数据行：至少两个数字，且数字字符占比够高。"""
    s = line.strip()
    if not (8 <= len(s) <= 160):
        return False
    nums = _TABLE_NUM.findall(s)
    if len(nums) < 2:
        return False
    return sum(len(n) for n in nums) / len(s) >= 0.15


def _looks_like_header(line: str) -> bool:
    """判断一行是否像表头：短、不以句号结尾、多数词首字母大写（或数字/符号）。"""
    s = line.strip()
    if not s or len(s) > 140 or s.endswith(("。", ".", "；", ";", "：", ":", ",")):
        return False
    words = s.split()
    if len(words) < 2:
        return False
    upper = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return upper / len(words) >= 0.5


def _split_table_blocks(text: str) -> list[tuple[bool, str]]:
    """把文本切成 (是否表格, 内容) 块；表格块会尽量带上紧邻的表头行。

    为什么要单独认表格：论文里的关键数字（BLEU、参数量、GPU 数）常常在表格里，
    普通句子切分会把表头和数据行拆到两块，"28.4 是哪一列"就丢了。
    """
    lines = text.split("\n")
    blocks: list[tuple[bool, str]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append((False, "\n".join(buf)))
            buf.clear()

    i = 0
    while i < len(lines):
        if not _is_table_row(lines[i]):
            buf.append(lines[i])
            i += 1
            continue

        flush()
        start = i
        while i < len(lines) and _is_table_row(lines[i]):
            i += 1

        header_lines: list[str] = []
        # 表头通常紧挨在表格上方，有时还是两行（如 "Model BLEU EN-DE ..." + "EN-DE EN-FR ..."）：
        # 从上一个文本块末尾最多取 2 行像表头的行，保证数字不脱列、不丢列名
        if blocks and not blocks[-1][0]:
            prev = blocks[-1][1].rstrip("\n").split("\n")
            while prev and len(header_lines) < 2 and _looks_like_header(prev[-1]):
                header_lines.insert(0, prev.pop())
            if header_lines:
                if prev:
                    blocks[-1] = (False, "\n".join(prev))
                else:
                    blocks.pop()
        header = "".join(h + "\n" for h in header_lines)
        blocks.append((True, header + "\n".join(lines[start:i])))

    flush()
    return blocks


def _table_chunks(content: str, chunk_size: int) -> list[str]:
    """把表格块按行分组，每块都重复表头，保证数字不脱列。"""
    lines = [ln for ln in content.split("\n") if ln.strip()]
    if not lines:
        return []
    header = ""
    if not _is_table_row(lines[0]) and _looks_like_header(lines[0]):
        header = lines[0] + "\n"
        lines = lines[1:]
    if not lines:
        return [header.strip()] if header.strip() else []

    out: list[str] = []
    current = header
    for line in lines:
        if len(header) + len(line) > chunk_size:
            # 单行就超长：交给普通递归切分兜底
            if current.strip():
                out.append(current.strip())
            current = header
            out.extend(_recursive_split(line, max(chunk_size - len(header), 1), _SEPARATORS))
            continue
        if len(current) + len(line) + 1 > chunk_size:
            out.append(current.strip())
            current = header
        current += line + "\n"
    if current.strip():
        out.append(current.strip())
    return [c for c in out if c]


def _pack_pieces(pieces: list[str], chunk_size: int, overlap: int) -> list[str]:
    """把碎片按块长合并，并在相邻块之间保留约 overlap 字的重叠。"""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
            continue
        if len(current) + len(piece) <= chunk_size:
            current += piece
            continue

        chunks.append(current.strip())

        tail = _overlap_tail(current, overlap)
        room = chunk_size - len(piece)
        if room <= 0:
            tail = ""
        elif len(tail) > room:
            tail = tail[-room:]
        current = tail + piece

    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _overlap_tail(text: str, overlap: int) -> str:
    """从上一块末尾取 overlap 个字当重叠，尽量从一个句子开头开始。"""
    if overlap <= 0 or not text:
        return ""

    tail = text[-overlap:]
    # 英文/数字场景：切点落在单词中间（如 "language" 被切成 "nguage"）时，
    # 往后挪到下一个空格 —— 带着半截单词的块会让向量和重排都误判。
    before = text[-overlap - 1 : -overlap] if len(text) > overlap else ""
    if before.isascii() and before.isalnum() and tail[:1].isascii() and tail[:1].isalnum():
        space = tail.find(" ")
        if 0 < space < len(tail) - 1:
            tail = tail[space + 1 :]

    for sep in ("。", "！", "？", "；", ". ", "\n"):
        pos = tail.find(sep)
        # 只在分隔符后面还有内容时才从那里开始，否则等于整个 tail
        if 0 <= pos < len(tail) - 1:
            return tail[pos + len(sep) :]
    return tail


def split_text(text: str, chunk_size: int = 500, overlap: int | None = None) -> list[str]:
    """[2] 递归切分：段落 → 句子 → 分句 → 空格 → 硬切，相邻块重叠约 10%。

    为什么不用"每 500 字切一刀"：
        硬切会把一句话劈成两半，检索回来的是半句，模型只能猜。
        所以先按段落/句子切，实在没有句界才硬切。

    overlap（重叠）解决的是"跨块的问题"：
        答案刚好落在两块交界处时，只看其中一块会缺半句，重叠一段就能兜住。
        默认取 10%（500 字 → 50 字）。

    返回的每一块长度都 ≤ chunk_size（重叠部分占用块内预算，不额外加长）。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    if overlap is None:
        overlap = max(1, int(chunk_size * 0.1))  # 默认 10%
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size，否则会死循环")

    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    for is_table, block in _split_table_blocks(text):
        if not block.strip():
            continue
        if is_table:
            chunks.extend(_table_chunks(block, chunk_size))
        else:
            pieces = _recursive_split(block, chunk_size, _SEPARATORS)
            chunks.extend(_pack_pieces(pieces, chunk_size, overlap))
    return [c for c in chunks if c]


# 稠密向量模型：Chroma 内置的 ONNX MiniLM（384 维，模型约 80MB，首次用自动下载）。
# 它按"语义"比较（"GPUs used for training" ≈ "8 NVIDIA P100 GPUs"），
# 和上面的哈希特征（按字面、精确）互补，两路一起用才是真正的混合检索。
_dense_ef = None


def _dense():
    """懒加载 + 单例：ONNX 会话很重，每次新建会慢十倍。"""
    global _dense_ef
    if _dense_ef is None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        _dense_ef = ONNXMiniLM_L6_V2()
    return _dense_ef


def index_chunks(chunks: list[Chunk]) -> int:
    """[3] 把块存进 Chroma（带向量），返回存入的数量。

    upsert = "有则更新、无则插入"：同一个 id 反复写不会产生重复记录，
    这样重复建库也安全。
    先统计全量的 IDF 再向量化（顺序不能反：没有统计就不知道谁常见）；
    写入时按批提交：Chroma 单次 upsert 上限 5461 条，全量 39 篇会超。
    """
    import chromadb  # 延迟导入：只有真要写库时才加载

    # 0) 丢掉"碎片块"：少于 _MIN_CHUNK_CHARS 字的块（页眉页脚、公式残渣、参考文献尾巴）
    #    它们 BM25 分数虚高（块短 → 长度归一化后分数放大），实测会抢走 GPU 题的 top1。
    chunks = [c for c in chunks if len(c.text.strip()) >= _MIN_CHUNK_CHARS]

    if not chunks:
        return 0

    # 1) 统计文档频率：每个特征出现在多少个块里
    df: dict[str, int] = {}
    for c in chunks:
        for name in {name for name, _ in _tokens(c.text)}:
            df[name] = df.get(name, 0) + 1
    _save_idf(df, len(chunks))
    _save_bm25(_build_bm25(chunks))

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name="docs")

    ids = [f"{c.source}:{c.chunk_id}" for c in chunks]
    documents = [c.text for c in chunks]
    embeddings = [embed(c.text) for c in chunks]
    metadatas = [
        {
            "source": c.source,
            "chunk_id": c.chunk_id,
            # Chroma 的元数据不接受 None，用 -1 表示"没有页码"
            "page": c.page if c.page is not None else -1,
        }
        for c in chunks
    ]

    batch = 2000  # 留余量，不贴着 5461 上限
    for start in range(0, len(ids), batch):
        collection.upsert(
            ids=ids[start : start + batch],
            documents=documents[start : start + batch],
            embeddings=embeddings[start : start + batch],
            metadatas=metadatas[start : start + batch],
        )

    # 再写一份"稠密向量"集合：只存原文，384 维向量交给 ONNX 模型现算。
    # 两份集合的 id 完全一致，检索时按 id 对齐、分数分开算，最后 RRF 融合。
    dense = client.get_or_create_collection(name="docs_dense", embedding_function=_dense())
    for start in range(0, len(ids), batch):
        dense.upsert(
            ids=ids[start : start + batch],
            documents=documents[start : start + batch],
            metadatas=metadatas[start : start + batch],
        )
    return len(ids)


def _dfs_from_postings(postings: dict[str, list[list[int]]]) -> dict[str, int]:
    """从倒排表反推 df（文档频率）。

    每篇论文的每个块对同一特征只记一条 [doc_id, tf]，
    所以 len(entries) 就是"有多少块含这个词"，不需要额外维护 df 表。
    """
    return {term: len(entries) for term, entries in postings.items()}


def _merge_bm25(index: dict | None, chunks: list[Chunk]) -> dict:
    """把新块追加到已有倒排表末尾（doc_id 继续往后排）。

    为什么不整库重建：全量重建 26000 块要 20 多分钟，
    上传一篇论文只该多花"这篇的时间"（几秒）。
    """
    if not index:
        return _build_bm25(chunks)

    docs = index["docs"]
    lens = list(index["lens"])
    postings = index["postings"]
    start = len(docs)

    for offset, c in enumerate(chunks):
        counts = Counter(name for name, _w in _tokens(c.text))
        lens.append(sum(counts.values()) or 1)
        for term, tf in counts.items():
            postings.setdefault(term, []).append([start + offset, tf])
        docs.append({"source": c.source, "page": c.page, "chunk_id": c.chunk_id})

    avg = (sum(lens) / len(lens)) if lens else 1.0
    return {"avg_len": avg, "lens": lens, "postings": postings, "docs": docs}


def _prune_bm25(index: dict, source: str) -> tuple[dict, int]:
    """删掉某篇论文的全部块，并把 doc_id 重新编号成连续的。

    重新编号是必须的：lens 是按 doc_id 下标的数组，
    删掉中间一段却不压缩编号，之后 BM25 的 |D| 长度就会错位。
    """
    docs = index["docs"]
    lens = index["lens"]
    keep = [i for i, d in enumerate(docs) if d.get("source") != source]
    removed = len(docs) - len(keep)
    if removed == 0:
        return index, 0

    remap = {old: new for new, old in enumerate(keep)}
    postings: dict[str, list[list[int]]] = {}
    for term, entries in index["postings"].items():
        kept = [[remap[i], tf] for i, tf in entries if i in remap]
        if kept:
            postings[term] = kept

    new_lens = [lens[i] for i in keep]
    avg = (sum(new_lens) / len(new_lens)) if new_lens else 1.0
    return (
        {
            "avg_len": avg,
            "lens": new_lens,
            "postings": postings,
            "docs": [docs[i] for i in keep],
        },
        removed,
    )


def add_chunks(chunks: list[Chunk]) -> int:
    """[3b] 增量入库：把新块并进现有索引，返回入库块数。

    与 index_chunks 的区别：
        index_chunks 用"这一批块"重建 IDF 和 BM25（会覆盖全库统计）；
        add_chunks 在旧统计上做增量合并，适合"上传一篇新论文"。

    已知取舍：已入库块的向量是用当时的 IDF 算的，新块用更新后的 IDF，
    向量之间会有细微不同步。这是增量的固有代价，全量重建可消除。
    """
    import chromadb

    chunks = [c for c in chunks if len(c.text.strip()) >= _MIN_CHUNK_CHARS]
    if not chunks:
        return 0

    index = _merge_bm25(_load_bm25(), chunks)
    _save_idf(_dfs_from_postings(index["postings"]), len(index["docs"]))
    _save_bm25(index)

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name="docs")

    ids = [f"{c.source}:{c.chunk_id}" for c in chunks]
    documents = [c.text for c in chunks]
    embeddings = [embed(c.text) for c in chunks]
    metadatas = [
        {
            "source": c.source,
            "chunk_id": c.chunk_id,
            "page": c.page if c.page is not None else -1,
        }
        for c in chunks
    ]

    batch = 2000
    for start in range(0, len(ids), batch):
        collection.upsert(
            ids=ids[start : start + batch],
            documents=documents[start : start + batch],
            embeddings=embeddings[start : start + batch],
            metadatas=metadatas[start : start + batch],
        )

    dense = client.get_or_create_collection(name="docs_dense", embedding_function=_dense())
    for start in range(0, len(ids), batch):
        dense.upsert(
            ids=ids[start : start + batch],
            documents=documents[start : start + batch],
            metadatas=metadatas[start : start + batch],
        )
    return len(ids)


def remove_source(source: str) -> int:
    """[3c] 从三路索引里彻底删掉一篇论文，返回删掉的块数。

    只删文件不删索引，会出现"论文已删除、AI 还能引用它"（P0-2 实测确认过）：
    PDF 和文本缓存没了，但向量库和 BM25 里的块还在，检索照样命中。

    删除顺序是**故意**安排的：最容易失败的一步（docs_dense）放最后。
        BM25 是本地 JSON 文件，前两步（读 docs、删 docs）已完成后再算，失败概率最低；
        docs_dense 是外部服务，失败概率最高，放最后一步，
        这样一旦抛异常，已经改动的范围最小，残留块最少。
    注意 docs 与 BM25 都已经删了、docs_dense 才失败时，三路仍会不一致：
    这是无法真正回滚的（docs 的原始块已被删除），只能如实抛错、记日志，
    由设置页的「健康检查」再次暴露出来，允许重试。
    """
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name="docs")
    where = {"source": source}
    ids = (collection.get(where=where, include=[]) or {}).get("ids") or []
    removed = len(ids)
    if ids:
        collection.delete(where=where)

    # BM25 放在 docs_dense 之前：本地文件操作，比外部向量库更容易成功
    index = _load_bm25()
    if index:
        pruned, bm_removed = _prune_bm25(index, source)
        if bm_removed:
            _save_idf(_dfs_from_postings(pruned["postings"]), len(pruned["docs"]))
            _save_bm25(pruned)
        removed = max(removed, bm_removed)

    try:
        dense = client.get_or_create_collection(name="docs_dense", embedding_function=_dense())
        dense.delete(where=where)
    except Exception as exc:  # noqa: BLE001 —— 稠密集合失败不能当成删干净了，必须让人知道
        logger.warning("删除 docs_dense 中 %s 的块失败（docs 与 BM25 已删，此处残留）", source, exc_info=True)
        raise RuntimeError(f"删除 docs_dense 失败: {type(exc).__name__}: {exc}") from exc

    return removed


def index_paper(path: str, source: str | None = None) -> int:
    """[3a] 把一篇 PDF 切块后增量入库（上传接口调用）。

    chunk_id 用"页码 × 1000 + 页内序号"，和 scripts/build_index.py 的编号规则一致，
    这样上传的论文和全量建库的论文在索引里长得一样。
    """
    pages = load_pdf(path)
    name = source or os.path.basename(path)

    chunks: list[Chunk] = []
    for page_chunk in pages:
        for i, piece in enumerate(split_text(page_chunk.text, DEFAULT_CHUNK_SIZE)):
            chunks.append(
                Chunk(
                    text=piece,
                    source=name,
                    page=page_chunk.page,
                    chunk_id=(page_chunk.page or 0) * 1000 + i,
                )
            )
    return add_chunks(chunks)


def _fuse_ids(
    dense_ids: list[str],
    hash_ids: list[str],
    bm25_ids: list[str],
    top_k: int,
) -> tuple[list[str], dict[str, float], dict[str, list[str]]]:
    """RRF 融合三路召回结果，返回 (排序后的 id, 融合分, 命中通道)。

    权重策略：稠密可用时 dense=2.0 / bm25=1.0 / hash=0.5；
    稠密不可用时 hash 接管 dense 的 2.0（哈希当离线兜底，不让检索整体失效）。
    """
    dense_w = _W_DENSE if dense_ids else 0.0
    hash_w = _W_HASH if dense_ids else _W_DENSE
    fused: dict[str, float] = {}
    channels: dict[str, list[str]] = {}

    def _add(cid: str, weight: float, rank: int, channel: str) -> None:
        fused[cid] = fused.get(cid, 0.0) + weight / (_RRF_K + rank + 1)
        channels.setdefault(cid, []).append(channel)

    for rank, cid in enumerate(dense_ids):
        _add(cid, dense_w, rank, "dense")
    for rank, cid in enumerate(hash_ids):
        _add(cid, hash_w, rank, "hash")
    for rank, cid in enumerate(bm25_ids):
        _add(cid, _W_BM25, rank, "bm25")

    order = sorted(fused, key=lambda k: fused[k], reverse=True)[:top_k]
    return order, fused, channels


_MAX_PER_SOURCE = 2


def _diversify_by_source(
    items: list[tuple[Chunk, float, list[str]]],
    top_k: int,
    source: str | None = None,
) -> list[tuple[Chunk, float, list[str]]]:
    """按来源限流：同一篇论文最多贡献 _MAX_PER_SOURCE 条证据。

    为什么需要：论文库里同一篇存在多个版本副本（1706.03762v7 / v1 / v1_img /
    重传副本）。它们内容几乎一样，向量和 BM25 分数也几乎一样，纯按分数截断时
    会把这 4 条全挤进 4 个证据位——实测提问「Transformer 的注意力机制怎么算」，
    返回的 4 条证据全部来自 1706.03762 的 4 个副本，其他论文一条都没进来。
    这既浪费证据位，也让「覆盖论文数」这个展示指标失去意义。

    规则：高分优先，但同一 source 计数到上限就跳过，把位置让给后面的论文。
    被跳过的块不丢，留在候选池里——若后面的来源不够填满 top_k，再回补它们，
    避免为了多样性把证据条数做少。
    单篇问答（source 已指定）不需要限流，全部来源本来就相同，直接返回。
    """
    if source or len(items) <= top_k:
        return items

    picked: list[tuple[Chunk, float, list[str]]] = []
    overflow: list[tuple[Chunk, float, list[str]]] = []
    per_source: dict[str, int] = {}

    for item in items:
        src = item[0].source
        if per_source.get(src, 0) < _MAX_PER_SOURCE:
            per_source[src] = per_source.get(src, 0) + 1
            picked.append(item)
        else:
            overflow.append(item)
        if len(picked) >= top_k:
            break

    # 来源不够用时回补，保证证据条数不被多样性拖少
    if len(picked) < top_k:
        picked.extend(overflow[: top_k - len(picked)])
    return picked


def retrieve_scored(
    question: str, top_k: int = 4, source: str | None = None, use_dense: bool = True
) -> list[tuple[Chunk, float, list[str]]]:
    """[4] 混合检索，返回 (块, RRF 分数, 命中通道)。

    和 retrieve() 的区别：多带"为什么排在这"的信息，供证据卡片和调试脚本用。
    RRF 分数只反映"在几路榜单里排第几"，不是概率，不能当正确率看。

    容错策略：三路召回各自 try/except——哪一路挂了就跳过这一路并记日志，
    其余通道继续融合；全挂才返回 []，绝不让检索异常把整轮问答打崩。

    use_dense=False 时跳过稠密向量那一路（前端「智能搜索」关掉就是这个意思）：
    只剩哈希 + BM25。少了语义相似召回，同义改写的问题会差一些，但不用加载
    ONNX embedding 模型，响应更快。这不是降级容错，是用户显式选择，所以记 info 而非 warning。
    """
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    try:
        collection = client.get_collection(name="docs")
    except Exception:  # noqa: BLE001 —— 索引可能还没建，按"空库"处理
        logger.warning("向量集合 docs 不存在，本次检索返回空")
        return []

    where = {"source": source} if source else None
    try:
        if where:
            total = len((collection.get(where=where, include=[]) or {}).get("ids") or [])
        else:
            total = collection.count()
    except Exception:  # noqa: BLE001
        logger.exception("读取向量集合规模失败，本次检索返回空")
        return []
    if total == 0:
        return []

    n = min(max(top_k * 3, _RECALL_DEPTH), total)

    # 通道一：哈希向量（离线兜底，也是稠密不可用时的替补）
    hash_ids: list[str] = []
    try:
        result = collection.query(query_embeddings=[embed(question)], n_results=n, where=where)
        hash_ids = (result.get("ids") or [[]])[0]
    except Exception:  # noqa: BLE001
        logger.exception("哈希召回失败，跳过该通道")

    # 通道二：稠密向量（ONNX MiniLM，语义相似）
    dense_ids: list[str] = []
    if use_dense:
        try:
            dense_col = client.get_or_create_collection(name="docs_dense", embedding_function=_dense())
            dense_res = dense_col.query(query_texts=[question], n_results=n, where=where)
            dense_ids = (dense_res.get("ids") or [[]])[0]
        except Exception:  # noqa: BLE001
            logger.exception("稠密召回失败，降级为哈希 + BM25")
    else:
        logger.info("用户关闭了智能搜索，本次跳过稠密召回，只用哈希 + BM25")

    # 通道三：BM25（关键词精确匹配）
    bm25_ids: list[str] = []
    try:
        bm25_ids = _bm25_ids(question, n, source=source)
    except Exception:  # noqa: BLE001
        logger.exception("BM25 召回失败，跳过该通道")

    # 重排只在候选池内部做：池子要比重排后取的数量大，否则等于没重排
    pool = max(top_k, _RERANK_POOL) if _RERANK_ENABLED else top_k
    order, fused, channels = _fuse_ids(dense_ids, hash_ids, bm25_ids, pool)
    if not order:
        logger.warning("三路召回均为空，返回空结果")
        return []

    try:
        fetched = collection.get(ids=order)
    except Exception:  # noqa: BLE001
        logger.exception("按 id 取回原文失败，返回空结果")
        return []
    by_id = {
        cid: (text, meta)
        for cid, text, meta in zip(
            fetched.get("ids") or [],
            fetched.get("documents") or [],
            fetched.get("metadatas") or [],
            strict=False,
        )
    }

    out: list[tuple[Chunk, float, list[str]]] = []
    for cid in order:
        item = by_id.get(cid)
        if not item:
            continue
        text, meta = item
        page = (meta or {}).get("page", -1)
        chunk = Chunk(
            text=text,
            source=(meta or {}).get("source", "unknown"),
            page=None if page == -1 else page,
            chunk_id=(meta or {}).get("chunk_id", 0),
        )
        out.append((chunk, fused[cid], channels.get(cid, [])))

    out = _diversify_by_source(out, top_k, source=source)

    if _RERANK_ENABLED:
        try:
            out = rerank_fn(question, out, top_k)
        except RerankUnavailable as exc:
            logger.warning("重排不可用，回退融合排序：%s", exc)
            out = out[:top_k]
    else:
        out = out[:top_k]

    # 参考文献片段降权：放在重排之后，否则重排分会把惩罚覆盖掉
    penalized: list[tuple[Chunk, float, list[str]]] = []
    for chunk, score, chans in out:
        if _looks_like_citation(chunk.text):
            score = score * _CITATION_PENALTY
            chans = [*chans, "citation"]
        penalized.append((chunk, score, chans))
    penalized.sort(key=lambda item: item[1], reverse=True)
    return penalized[:top_k]


def retrieve(
    question: str, top_k: int = 4, source: str | None = None, use_dense: bool = True
) -> list[Chunk]:
    """[4] 混合检索：向量召回 + BM25 精排，RRF 融合后取前 top_k（只返回块）。"""
    return [
        chunk for chunk, _score, _ch in retrieve_scored(question, top_k, source, use_dense=use_dense)
    ]


# 页级兜底用：数值类问题的答案常在表格里，块内可能只剩一个孤零零的数字，
# 所以命中页整页文本也补一份给模型（带缓存，同一页只解析一次）。
_PAPERS_DIR = Path(__file__).resolve().parents[2] / "data" / "papers"


@lru_cache(maxsize=128)
def _page_text_cached(source: str, page: int) -> str:
    path = _PAPERS_DIR / source
    if not path.exists():
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if not 1 <= page <= len(reader.pages):
            return ""
        text = (reader.pages[page - 1].extract_text() or "").strip()
        if not text:                      # 扫描件：退回 OCR 缓存
            text = _ocr_cached(str(path), page).strip()
        return text
    except Exception:
        return ""


def _ocr_cached(path: str, page_no: int) -> str:
    """OCR 缓存（0 基页号，由 scripts/ocr_scanned.py 生成）；没有缓存返回空串。"""
    try:
        from app.ocr import cached_page

        return cached_page(path, page_no - 1)
    except Exception:  # noqa: BLE001 - OCR 不可用不能影响主流程
        return ""


def page_text(source: str, page: int) -> str:
    """取某篇论文第 page 页的全文（带缓存）；取不到返回空串。"""
    return _page_text_cached(source, page)
