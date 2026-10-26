"""章节定位测试。

问题背景：用户问「2305.18290v3.pdf 第 3 节讲了什么」时，检索按字面/向量
都找不到「第 3 节」这种版式信息，只能命中参考文献页。修法是查
structures.json 的章节表，把该节所在页正文直接补进上下文。
"""

from app import agent as agent_module


def test_section_page_finds_preliminaries():
    page, title = agent_module._section_page("2305.18290v3.pdf", "第 3 节讲了什么")
    # structures.json 里第 3 节是 Preliminaries，0 基页码 2 → PDF 第 3 页
    assert page == 3
    assert title == "Preliminaries"


def test_section_page_unknown_source_returns_none():
    assert agent_module._section_page("不存在.pdf", "第 3 节") is None


def test_section_page_without_number_returns_none():
    assert agent_module._section_page("2305.18290v3.pdf", "这篇讲了什么") is None
