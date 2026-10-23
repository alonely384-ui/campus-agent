"""交叉编码器重排（Cross-Encoder Re-ranker）。

为什么需要它？
    第一阶段的召回（向量 + BM25 + RRF）只保证"答案块出现在候选里"，
    但不保证它排第一：一个 300 字的块里只要出现几个数字，就可能被别的
    表格块压住（比如 Transformer 的 GPU 题，答案在第 7 页，第 8 页的
    Table 3 却常年排第一）。

它和召回阶段有什么区别？
    召回用双塔（bi-encoder）：问题和文档分开编码，各自算向量再比余弦。
        优点是文档向量可以离线算好、检索快；缺点是"问题和文档从没见过面"，
        细粒度的匹配关系（数字、否定、限定词）表达不出来。
    重排用交叉编码器：把「问题 + 文档」拼成一个序列一起送进模型，
        模型在每一层注意力里都能看到两边的词。
        缺点是每个候选都要现算一遍，慢；所以只用它重排前 20 个候选，
        而不是拿来检索整个库。

打分怎么读？
    onnx 输出的是 logits（可正可负的实数），不是 0~1 的概率；
    它只在同一批候选内部有意义（谁比谁高），不能跨问题比较，
    也不能说"0.8 就是 80% 正确"。

模型文件（离线，约 23MB）：
    data/models/ms-marco-MiniLM-L-6-v2/model.onnx        交叉编码器（量化版）
    data/models/ms-marco-MiniLM-L-6-v2/tokenizer.json    分词器
    可用 RAG_RERANK_DIR 覆盖目录；文件缺失时抛 RerankUnavailable，
    调用方回退到融合排序，绝不让检索整体失败。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from app.logging_config import get_logger

logger = get_logger(__name__)

_MODEL_DIR = Path(os.getenv("RAG_RERANK_DIR", "data/models/ms-marco-MiniLM-L-6-v2"))
# 文档截断长度：cross-encoder 的输入是「问题 + 文档」，512 是 BERT 类模型的上限
_MAX_LEN = 512

_session = None
_tokenizer = None
_load_failed = False


class RerankUnavailable(RuntimeError):
    """重排模型未就绪（缺文件/加载失败），调用方应回退到融合排序。"""


def _load():
    """懒加载 ONNX 会话与分词器；同一进程只加载一次。"""
    global _session, _tokenizer, _load_failed

    if _session is not None:
        return _session, _tokenizer
    if _load_failed:
        raise RerankUnavailable("重排模型此前加载失败，本进程不再重试")

    model_path = _MODEL_DIR / "model.onnx"
    tok_path = _MODEL_DIR / "tokenizer.json"
    if not model_path.exists() or not tok_path.exists():
        _load_failed = True
        raise RerankUnavailable(f"找不到重排模型：{model_path} 或 {tok_path}")

    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(str(tok_path))
        tokenizer.enable_truncation(max_length=_MAX_LEN)
        _session, _tokenizer = session, tokenizer
        logger.info("重排模型已加载：%s", model_path)
        return session, tokenizer
    except Exception as exc:  # noqa: BLE001 —— 加载失败一律降级，不向上抛原始异常
        _load_failed = True
        raise RerankUnavailable(f"重排模型加载失败：{exc}") from exc


def score_pairs(question: str, passages: Sequence[str]) -> list[float]:
    """给每个 (问题, 文档) 打分，返回 logits（越大越相关，只在批内可比）。"""
    if not passages:
        return []

    import numpy as np

    session, tokenizer = _load()
    encodings = tokenizer.encode_batch([(question, p) for p in passages])
    width = max(len(enc.ids) for enc in encodings)

    ids = np.zeros((len(encodings), width), dtype=np.int64)
    mask = np.zeros((len(encodings), width), dtype=np.int64)
    types = np.zeros((len(encodings), width), dtype=np.int64)
    for row, enc in enumerate(encodings):
        n = len(enc.ids)
        ids[row, :n] = enc.ids
        mask[row, :n] = 1
        types[row, :n] = enc.type_ids

    wanted = {inp.name for inp in session.get_inputs()}
    feeds: dict[str, object] = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in wanted:
        feeds["token_type_ids"] = types

    logits = session.run(None, feeds)[0]
    return [float(x) for x in np.asarray(logits).reshape(-1)]


def rerank(question: str, items: list, top_k: int) -> list:
    """按交叉编码器得分重排候选。

    items: [(Chunk, 融合分, 命中通道), ...]
    返回同结构，顺序按重排分降序；分数替换为重排 logits，通道里追加 "rerank"。
    """
    if not items:
        return []

    scores = score_pairs(question, [chunk.text for chunk, _s, _ch in items])
    ranked = sorted(zip(items, scores, strict=True), key=lambda pair: pair[1], reverse=True)

    out = []
    for (chunk, _old_score, channels), score in ranked[:top_k]:
        out.append((chunk, score, [*channels, "rerank"]))
    return out
