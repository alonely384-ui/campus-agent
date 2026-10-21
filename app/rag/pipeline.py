"""RAG 检索增强生成——把"查资料"塞进"回答问题"之前。

流程（两段）：

    【建库，只做一次】
        PDF/TXT ──load──► 文字 ──split──► 500 字一段 ──index──► 存进 Chroma

    【问答，每次提问】
        问题 ──retrieve──► 找最像的 4 段 ──拼进 prompt──► 大模型回答 + 附来源

关于"向量"（本项目用的是最轻的一种）：
    把一段文字变成 256 个数字。做法是"字 2-gram 哈希"：
        1. 把文字切成相邻两个字一组："校园"、"园智"、"智能"……
        2. 每组用 crc32 算出一个整数，对 256 取余，落到某一格，那格 +1
        3. 最后把整串数字"归一化"（除以长度），保证长文和短文可比
    两段话用词越接近，数字分布就越接近 → 按"意思像不像"检索。
    这不是最强的向量方案（最强的是神经网络模型），但零依赖、零下载、
    结果稳定可复现，适合先把"读取→切分→入库→检索"的闭环跑通。

面试会追问的三个问题：
    1. 为什么不用关键词搜索（SQL 的 LIKE）？—— "怎么申请奖学金"和"助学金流程"
       一个字都不一样，但意思相关；向量按"意思像不像"找，不按"字面有没有"找。
    2. 检索出来的片段不相关时怎么办？—— 这就是"拒答"策略，宁可说不知道。
    3. chunk_size 为什么定这个值？—— 因为你测过，有数字。
"""

import math
import os
import re
import zlib
from dataclasses import dataclass

from app.config import settings


@dataclass
class Chunk:
    """一个文本块。"""

    text: str
    source: str      # 来自哪个文件
    page: int | None = None
    chunk_id: int = 0


# 向量的维度：256 格。格子越多越精细，但也越稀疏（很多格子是 0）。
DIM = 256


def embed(text: str) -> list[float]:
    """把一段文字变成 256 维向量（本项目的轻量向量化）。

    为什么用 zlib.crc32 而不是内置的 hash()：
        Python 的 hash() 每个进程启动时都会换一个随机种子，
        同一个词今天算出 3、明天算出 90012 —— 检索结果天天变，无法复现。
        crc32 是确定性的：同样的输入永远同样的数字。
    """
    vec = [0.0] * DIM

    # 去掉所有空白字符（空格/换行/制表符），避免它们干扰"两个字一组"
    clean = re.sub(r"\s+", "", text)
    if not clean:
        return vec

    # 相邻两个字一组；只有一个字时，就把这个字本身当一个组
    grams = [clean[i : i + 2] for i in range(len(clean) - 1)] or [clean]

    for gram in grams:
        index = zlib.crc32(gram.encode("utf-8")) % DIM
        vec[index] += 1.0

    # 归一化：让每个向量的长度都变成 1，这样"长短不同的文本"才能公平比较
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


def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """[2] 把长文本切成小块，尽量在句子边界切。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size，否则会死循环")

    chunks: list[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + chunk_size, n)          # 先按"字数"切一刀

        if end < n:                               # 不是最后一块，才找句子边界
            window = text[start:end]
            cut = max(window.rfind("。"), window.rfind("？"), window.rfind("\n"))
            if cut > chunk_size // 2:             # 只在后半段切，否则会切得太碎
                end = start + cut + 1             # +1 把"。"留在本块末尾

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= n:                              # 到最后了，收工
            break
        start = end - overlap                     # 回退 overlap 个字 = 重叠

    return chunks


def index_chunks(chunks: list[Chunk]) -> int:
    """[3] 把块存进 Chroma（带向量），返回存入的数量。

    upsert = "有则更新、无则插入"：同一个 id 反复写不会产生重复记录，
    这样重复建库也安全。
    """
    import chromadb  # 延迟导入：只有真要写库时才加载

    if not chunks:
        return 0

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

    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    return len(ids)


def retrieve(question: str, top_k: int = 4) -> list[Chunk]:
    """[4] 把问题也变成向量，在库里找最像的 top_k 个块。"""
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name="docs")

    total = collection.count()
    if total == 0:
        return []          # 库里没东西，直接返回空（上层可以据此拒答）

    result = collection.query(
        query_embeddings=[embed(question)],
        n_results=min(top_k, total),
    )

    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]

    chunks: list[Chunk] = []
    for i, text in enumerate(documents):
        meta = metadatas[i] if i < len(metadatas) else {}
        page = meta.get("page", -1)
        chunks.append(
            Chunk(
                text=text,
                source=meta.get("source", "unknown"),
                page=None if page == -1 else page,
                chunk_id=meta.get("chunk_id", i),
            )
        )
    return chunks
