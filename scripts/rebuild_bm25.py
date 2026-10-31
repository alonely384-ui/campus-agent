"""从 Chroma 已有的块反推重建 BM25 索引（不重新切块、不算向量）。

用不上的场景：Chroma 本身就是空的，那就该跑 build_index.py 真正重建。

为什么会需要这个脚本：BM25 倒排表存在 data/chroma/bm25.json，
它被 .gitignore 忽略、没有备份；历史上被测试污染过（test_remove_source_deletes_dense_last
漏了 chroma_dir 的 monkeypatch，把 26596 块的真实索引写成了空索引）。
Chroma 里的块还在，据此反推是最快的恢复路径：约 1 分钟，而 build_index.py 要 30 分钟。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb  # noqa: E402

from app.rag import pipeline  # noqa: E402


def main() -> None:
    t0 = time.time()
    client = chromadb.PersistentClient(path=pipeline.settings.chroma_dir)
    col = client.get_or_create_collection(name="docs")
    total = col.count()
    print(f"Chroma docs 块数: {total}")
    if total == 0:
        print("Chroma 为空，没有可反推的块；请跑 scripts/build_index.py")
        return

    chunks: list[pipeline.Chunk] = []
    step = 5000
    for off in range(0, total, step):
        r = col.get(limit=step, offset=off, include=["documents", "metadatas"])
        for text, meta in zip(r["documents"] or [], r["metadatas"] or [], strict=True):
            page = meta.get("page")
            chunks.append(
                pipeline.Chunk(
                    text=text,
                    source=str(meta.get("source") or ""),
                    page=None if page in (None, -1) else int(page),
                    chunk_id=int(meta.get("chunk_id") or 0),
                )
            )
    print(f"读回 {len(chunks)} 块，耗时 {time.time() - t0:.1f}s")

    t1 = time.time()
    index = pipeline._build_bm25(chunks)
    pipeline._save_idf(pipeline._dfs_from_postings(index["postings"]), len(index["docs"]))
    pipeline._save_bm25(index)

    path = Path(pipeline._bm25_path())
    print(f"BM25 重建完成: docs={len(index['docs'])} postings={len(index['postings'])} "
          f"avg_len={index['avg_len']:.2f} 耗时 {time.time() - t1:.1f}s")
    print(f"落盘: {path}  大小 {path.stat().st_size} 字节")


if __name__ == "__main__":
    main()