"""从索引里彻底移除一篇论文：向量集合 + 词表(IDF) + BM25 倒排。

为什么删文件不够：库里有三份"派生数据"都指向这篇论文——
  · docs / docs_dense 两个向量集合里的块
  · data/chroma/idf.json（词表，决定每个词的分量）
  · data/chroma/bm25.json（倒排索引，决定关键字召回）
只删 PDF 文件，检索仍会命中这些块，回答就会引用一个"已经不存在的论文"。

用法（在 campus-agent 目录）：
    .\\.venv\\Scripts\\python.exe scripts\\remove_paper.py 0a9bff1be357.pdf
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.rag.pipeline import (  # noqa: E402
    Chunk,
    _build_bm25,
    _save_bm25,
    _save_idf,
    _tokens,
)

PAPERS_DIR = Path("data/papers")


def main(filename: str) -> None:
    import chromadb

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    docs = client.get_or_create_collection("docs")
    dense = client.get_or_create_collection("docs_dense")

    print(f"删除前: docs={docs.count()} dense={dense.count()}")
    for col in (docs, dense):
        try:
            col.delete(where={"source": filename})
        except Exception as e:  # noqa: BLE001
            print(f"  {col.name} 删除失败: {e}")
    print(f"删除后: docs={docs.count()} dense={dense.count()}")

    # 重建词表与倒排：从剩下的块重新分词一遍
    t0 = time.time()
    all_docs = docs.get(include=["documents", "metadatas"], limit=100000)
    chunks = [
        Chunk(
            text=text or "",
            source=(meta or {}).get("source", ""),
            page=(meta or {}).get("page", -1),
            chunk_id=(meta or {}).get("chunk_id", 0),
        )
        for text, meta in zip(all_docs["documents"], all_docs["metadatas"], strict=False)
    ]
    df: dict[str, int] = {}
    for c in chunks:
        for term in {name for name, _w in _tokens(c.text)}:
            df[term] = df.get(term, 0) + 1
    _save_idf(df, len(chunks))
    _save_bm25(_build_bm25(chunks))
    print(f"重建索引: {len(chunks)} 块, 词表 {len(df)} 词, 耗时 {time.time()-t0:.0f}s")

    # 清掉磁盘文件与结构化缓存里的条目
    p = PAPERS_DIR / filename
    if p.exists():
        p.unlink()
        print(f"已删除文件: {p}")

    import json

    sf = PAPERS_DIR / "structures.json"
    if sf.exists():
        data = json.loads(sf.read_text(encoding="utf-8"))
        data.pop(filename, None)
        sf.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已清理 structures.json 条目")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python scripts/remove_paper.py <文件名>")
        raise SystemExit(2)
    main(sys.argv[1])
