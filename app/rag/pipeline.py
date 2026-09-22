"""RAG 检索增强生成——第 2 周在这里实现。

现在这个文件是**空壳**，它存在的意义是让你知道代码该写在哪，
而不是把所有逻辑堆在 main.py 里。

================================ 第 2 周要做的 4 件事 ================================

[1] 读取文档（loader）
        PDF → 文本。用 pypdf：
            from pypdf import PdfReader
            text = "\\n".join(p.extract_text() for p in PdfReader(path).pages)

[2] 切分（splitter）★ 这里是最需要动脑、也最能体现"技术取舍"的一步
        为什么要切？因为模型一次能读的字数有上限，而且整本书塞进去又慢又贵。
        要试的参数：
            chunk_size   —— 每块多大？试 300 / 500 / 800，看命中率怎么变
            overlap      —— 相邻块重叠多少字？防止答案正好被切断
        把试出来的对比结果记在 README 的技术决策表里。

[3] 存进向量库（indexer）
        Chroma 的基本用法：
            import chromadb
            client = chromadb.PersistentClient(path="./data/chroma")
            col = client.get_or_create_collection("docs")
            col.add(documents=[...], ids=[...])
        "向量"你可以先理解成：把一段话变成一串数字，
        意思相近的两段话，数字也相近。所以能"按意思搜索"而不是"按关键词搜索"。

[4] 检索 + 生成（retriever）
        用户提问 → 把问题也变成向量 → 找出最像的 3~5 块 → 把这几块
        连同问题一起发给模型 → 要求它"只根据这些内容回答并注明来源"

==================================================================================

面试会追问的三个问题（想清楚再动手）：
    1. 为什么不用关键词搜索（比如 SQL 的 LIKE）？—— 想想"怎么申请奖学金"和"助学金流程"
    2. 检索出来的片段不相关时怎么办？—— 这就是"拒答"策略
    3. chunk_size 为什么定这个值？—— 因为你测过，有数字
"""

from dataclasses import dataclass


@dataclass
class Chunk:
    """一个文本块。"""

    text: str
    source: str      # 来自哪个文件
    page: int | None = None
    chunk_id: int = 0


def load_pdf(path: str) -> list[Chunk]:
    """[1] 读取 PDF，返回按页拆分的块。

    第 2 周实现。提示：
        from pypdf import PdfReader
        reader = PdfReader(path)
        for page_no, page in enumerate(reader.pages):
            text = page.extract_text()
    """
    raise NotImplementedError("第 2 周实现：读取 PDF")


def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """[2] 把长文本切成小块。

    第 2 周实现。注意：
        - overlap 必须小于 chunk_size，否则会死循环
        - 尽量在句号/换行处切，别把句子劈成两半
    """
    raise NotImplementedError("第 2 周实现：切分文本")


def index_chunks(chunks: list[Chunk]) -> int:
    """[3] 把块存进 Chroma，返回存入的数量。

    第 2 周实现。
    """
    raise NotImplementedError("第 2 周实现：写入向量库")


def retrieve(question: str, top_k: int = 4) -> list[Chunk]:
    """[4] 根据问题检索最相关的 top_k 个块。

    第 2 周实现。返回的块会被拼进 prompt 里。
    """
    raise NotImplementedError("第 2 周实现：检索")
