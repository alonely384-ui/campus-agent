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

from app.config import settings


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
    global _BM25
    os.makedirs(settings.chroma_dir, exist_ok=True)
    with open(_bm25_path(), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
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
            # 扫描版 PDF（整页是图片）没有文字层，extract_text 返回空，跳过。
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


def _overlap_tail(text: str, overlap: int) -> str:
    """从上一块末尾取 overlap 个字当重叠，尽量从一个句子开头开始。"""
    if overlap <= 0 or not text:
        return ""

    tail = text[-overlap:]
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

    pieces = _recursive_split(text, chunk_size, _SEPARATORS)

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

        # 新块 = 上一块的尾巴（重叠）+ 当前碎片；超长就从尾巴里截短
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
    return len(ids)


def retrieve(question: str, top_k: int = 4, source: str | None = None) -> list[Chunk]:
    """[4] 混合检索：向量召回 + BM25 精排，融合后取前 top_k。

    为什么两路都要：
        向量擅长"意思接近但用词不同"，但哈希碰撞会带进无关块；
        BM25 只认真正出现过的词，精确但换个说法就找不到。
        两路结果用 RRF（倒数排名融合）合并：谁在各自榜单里靠前就加分，
        不比较两路的绝对分数（它们量纲不同，没法直接相加）。

    source：只在这一篇论文里检索（单篇问答必须传，否则会串到别的论文）。
    """
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name="docs")

    where = {"source": source} if source else None
    if where:
        total = len((collection.get(where=where, include=[]) or {}).get("ids") or [])
    else:
        total = collection.count()
    if total == 0:
        return []          # 库里没东西，直接返回空（上层可以据此拒答）

    n = min(max(top_k * 3, top_k), total)
    result = collection.query(
        query_embeddings=[embed(question)],
        n_results=n,
        where=where,
    )
    vector_ids = (result.get("ids") or [[]])[0]

    # RRF：名次越靠前得分越高；常数 60 是经验值，压制"断崖式"领先
    fused: dict[str, float] = {}
    for rank, cid in enumerate(vector_ids):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    for rank, cid in enumerate(_bm25_ids(question, n, source=source)):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank + 1)

    order = sorted(fused, key=lambda k: fused[k], reverse=True)[:top_k]
    if not order:
        return []

    fetched = collection.get(ids=order)
    by_id = {
        cid: (text, meta)
        for cid, text, meta in zip(
            fetched.get("ids") or [],
            fetched.get("documents") or [],
            fetched.get("metadatas") or [],
            strict=False,
        )
    }

    chunks: list[Chunk] = []
    for cid in order:
        item = by_id.get(cid)
        if not item:
            continue
        text, meta = item
        page = (meta or {}).get("page", -1)
        chunks.append(
            Chunk(
                text=text,
                source=(meta or {}).get("source", "unknown"),
                page=None if page == -1 else page,
                chunk_id=(meta or {}).get("chunk_id", 0),
            )
        )
    return chunks
