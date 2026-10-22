"""重建 RAG 索引：data/papers/*.pdf → 切块 → 入库。

用法（在 campus-agent 目录下执行）：
    .\\.venv\\Scripts\\python.exe scripts\\build_index.py        # 全部论文
    .\\.venv\\Scripts\\python.exe scripts\\build_index.py 3      # 只跑前 3 篇（调试）

可以反复执行：id 由「文件名 + 页码 + 块号」决定，upsert 有则更新、无则插入，
不会重复入库。
"""

import sys
import time
from pathlib import Path

# 让脚本能 import 到 app 包（直接 python scripts/build_index.py 也能跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.pipeline import (  # noqa: E402
    DEFAULT_CHUNK_SIZE,
    Chunk,
    index_chunks,
    load_pdf,
    split_text,
)


def main(limit: int | None = None) -> None:
    pdfs = sorted(Path("data/papers").glob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]
    print(f"论文数: {len(pdfs)}，切块大小: {DEFAULT_CHUNK_SIZE}")

    chunks: list[Chunk] = []
    for pdf in pdfs:
        for page in load_pdf(str(pdf)):
            assert page.page is not None  # load_pdf 一定带页码
            for i, text in enumerate(split_text(page.text, DEFAULT_CHUNK_SIZE)):
                # 页码 × 1000 + 页内块号：全局唯一，同时保留可读性
                chunks.append(
                    Chunk(
                        text=text,
                        source=page.source,
                        page=page.page,
                        chunk_id=page.page * 1000 + i,
                    )
                )
    print(f"切块: {len(chunks)}")

    t0 = time.time()
    n = index_chunks(chunks)
    print(f"入库: {n} / {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
