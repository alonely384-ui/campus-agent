"""1.6.1 检索词英译的回归测试。

背景（实测数据）：arXiv 和 Crossref 的索引都不做中文分词——

    search_crossref("引用关系图谱")   -> Application of Retail Linkage Map ...   ]
    search_crossref("论文推荐系统")   -> Application of Retail Linkage Map ...   } 三个查询
    search_crossref("文献推荐")       -> Application of Retail Linkage Map ...   ] 结果完全相同
    search_arxiv("引用关系图谱")      -> 0 条
    search_arxiv("citation graph")    -> Oignon / Refcat / CitationIE ...（高相关）

所以中文 idea 必须补一条英文检索路径，否则候选池里只有标题带「图」字的数学论文，
模型照着引用就是引用错文献——比不引用更糟。
"""

import asyncio

import pytest

from app import writing as w


class _FakeChat:
    """替身：记录被调用的事实，按预设返回。"""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict], temperature: float = 0.3) -> str:
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _run(monkeypatch: pytest.MonkeyPatch, reply: str | Exception, queries: list[str]) -> list[str]:
    fake = _FakeChat(reply)
    monkeypatch.setattr(w, "chat", fake)
    out = asyncio.run(w._english_queries(queries))
    monkeypatch.setattr(w, "chat", w.chat)
    return out


def test_english_queries_translates_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        monkeypatch,
        "citation graph, paper recommendation system, reference chain",
        ["引用关系图谱", "论文推荐系统"],
    )
    assert out == ["citation graph", "paper recommendation system", "reference chain"]


def test_english_queries_skips_when_all_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """全英文检索词不需要翻译，不该白调一次模型。"""
    fake = _FakeChat("should not be called")
    monkeypatch.setattr(w, "chat", fake)
    out = asyncio.run(w._english_queries(["LoRA", "attention mechanism"]))
    assert out == []
    assert fake.calls == []


def test_english_queries_survives_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型挂了要退回纯中文检索，而不是让整个框架生成失败。"""
    out = _run(monkeypatch, RuntimeError("upstream 502"), ["引用关系图谱"])
    assert out == []


def test_english_queries_drops_non_english_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型没按要求输出、混了中文时，只保留纯英文词。"""
    out = _run(
        monkeypatch,
        "1. citation graph\n2. 论文推荐\n3. recommendation system",
        ["引用关系图谱"],
    )
    assert out == ["citation graph", "recommendation system"]


def test_english_queries_dedupes_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        monkeypatch,
        "a1, b2, c3, d4, e5, f6, g7, citation graph, citation graph",
        ["引用关系图谱"],
    )
    assert len(out) <= 6
    assert len(set(out)) == len(out)


def test_arxiv_expr_uses_title_for_first_word() -> None:
    """第一个实词限定标题，保证主题不跑偏。"""
    expr = w.lit._arxiv_expr("citation graph recommendation")
    assert expr.startswith('ti:"citation"')
    assert 'all:"graph"' in expr


def test_arxiv_expr_drops_stopwords() -> None:
    """停用词留在表达式里会把命中打成 0（实测 abs:"based" AND abs:"paper" 起了反作用）。"""
    expr = w.lit._arxiv_expr("citation graph based paper recommendation")
    assert 'based' not in expr
    assert 'paper' not in expr
    assert 'ti:"citation"' in expr


def test_arxiv_expr_falls_back_without_quoting() -> None:
    """全是短词/停用词时退回裸词——加引号在 arXiv 上必空。"""
    expr = w.lit._arxiv_expr("a b c")
    assert expr
    assert 'all:"' not in expr