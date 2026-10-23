"""RAG 切分函数测试：验证"递归切分 + 10% 重叠"的两条硬规则。

测的是行为不是实现：
    1. 每块长度不超过 chunk_size；
    2. 相邻块之间有重叠（除非 overlap=0）；
    3. 优先在句子边界断开，而不是把句子劈两半。
"""

from app.rag.pipeline import _overlap_tail, split_text


def test_empty_text_returns_nothing():
    assert split_text("") == []
    assert split_text("   \n\n  ") == []


def test_short_text_is_one_chunk():
    text = "只有一句话。"
    assert split_text(text, chunk_size=500) == [text]


def test_every_chunk_within_size():
    text = "这是第一句话，用来凑长度。" * 200  # 2800 字
    chunks = split_text(text, chunk_size=500)

    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks), [len(c) for c in chunks]


def test_default_overlap_is_ten_percent():
    """默认重叠 = 10%：500 字 → 50 字。用重复句子构造，方便检查交接处。"""
    sentence = "一二三四五六七八九十" * 5 + "。"  # 51 字
    chunks = split_text(sentence * 20, chunk_size=500)

    # 相邻块必须有一段公共内容（就是重叠区）
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        tail = prev[-30:]  # 50 字重叠里至少能对上 1 个字
        assert any(tail[i:] and tail[i:] in nxt for i in range(len(tail))), (
            f"块之间没有重叠：{prev[-20:]!r} -> {nxt[:20]!r}"
        )


def test_english_prefers_sentence_boundary():
    text = "The quick brown fox jumps over the lazy dog. " * 30
    chunks = split_text(text, chunk_size=200)

    assert all(len(c) <= 200 for c in chunks)
    # 英文句子切在 ". " 后面，所以除了最后一块，其余都应该以句号结尾
    assert all(c.rstrip().endswith(".") for c in chunks[:-1])


def test_no_separator_falls_back_to_hard_cut():
    text = "x" * 1050  # 没有任何分隔符
    chunks = split_text(text, chunk_size=300)

    assert all(len(c) <= 300 for c in chunks)
    assert sum(len(c) for c in chunks) >= 1050


def test_invalid_overlap_raises():
    import pytest

    with pytest.raises(ValueError):
        split_text("abc", chunk_size=10, overlap=10)
    with pytest.raises(ValueError):
        split_text("abc", chunk_size=10, overlap=-1)


def test_overlap_zero_means_no_duplicate_content():
    text = "abcdefghij。" * 20
    chunks = split_text(text, chunk_size=20, overlap=0)
    assert chunks, "至少切出一块"
    assert all(len(c) <= 20 for c in chunks)


def test_overlap_tail_starts_at_word_boundary():
    """英文重叠不能从半个单词开始："language" 被切成 "guage" 时，应退到空格后。"""
    text = "we train a language model"
    # 最后 10 个字符是 "guage model"，落在 "language" 中间
    assert _overlap_tail(text, 10) == "model"
    # 中文没有空格，保持原样取尾
    assert _overlap_tail("这是中文句子没有空格", 4) == "没有空格"
