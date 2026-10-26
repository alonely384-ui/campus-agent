"""类别名模糊匹配测试。

背景：用户会把"能力评测与规模"写成"能力测评与规模"，一个字的差别
导致锁不到篇、卡片也不注入，回答变成"资料不足"。修法是用最长公共
子串兜底，同时保留"提到无关内容时返回空"的护栏。
"""

from app import agent as agent_module


def test_category_tolerates_typo():
    files = agent_module._category_sources("能力测评与规模四篇论文的主旨分别是什么")
    # 该类原有 4 篇，2026-10-27 删掉重复的 209_Emergent_Abilities_of_Larg.pdf 后剩 3 篇
    assert len(files) == 3
    assert "2001.08361v1.pdf" in files


def test_category_exact_still_works():
    files = agent_module._category_sources("模型架构是哪几篇")
    assert files and "1706.03762v7.pdf" in files


def test_category_no_false_positive():
    # 既没类目也没文件名，不应该乱锁
    assert agent_module._category_sources("这三篇中第一篇的主旨") == []
