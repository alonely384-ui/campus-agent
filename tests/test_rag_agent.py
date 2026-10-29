"""RAG 接入 Agent 的测试：检索结果要真的进入 messages，且带来源页码。"""

from types import SimpleNamespace

import pytest

import app.agent as agent_module
from app.agent import _rag_context, _rag_context_with_evidence, run_agent
from app.rag.pipeline import Chunk


async def test_rag_context_has_source_and_page(monkeypatch):
    def fake_retrieve(question, top_k=4, source=None):
        assert "GPUs" in question
        return [
            (
                Chunk(text="8 NVIDIA P100 GPUs", source="1706.03762v7.pdf", page=7),
                0.057,
                ["dense", "bm25"],
            )
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    context = await _rag_context("How many GPUs were used for training?")

    assert "1706.03762v7.pdf 第7页" in context
    assert "8 NVIDIA P100 GPUs" in context
    # 没检索到时必须拒答，这句提示要在
    assert "论文库中没有找到相关信息" in context


async def test_metadata_block_carries_page_1(monkeypatch):
    """元数据（标题/作者）也要能标页码，否则模型会写出"本篇元数据"这种非法引用。"""

    def fake_retrieve(question, top_k=4, source=None):
        return [(Chunk(text="title page", source="v7.pdf", page=1), 0.05, ["hash"])]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    monkeypatch.setattr(
        agent_module,
        "paper_meta",
        lambda source: {"title": "Attention Is All You Need", "authors": ["A", "B", "C"]},
    )

    context = await _rag_context("第六作者是谁？")
    assert "【本篇元数据｜第1页】" in context
    assert "第1页" in context


async def test_evidence_card_fields(monkeypatch):
    """证据卡片要有：排名、页码、融合分、命中通道、原文。"""

    def fake_retrieve(question, top_k=4, source=None):
        return [
            (Chunk(text="8 NVIDIA P100 GPUs", source="p.pdf", page=7), 0.057, ["dense"]),
            (Chunk(text="28.4 BLEU", source="p.pdf", page=8), 0.055, ["bm25"]),
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    _context, evidence = await _rag_context_with_evidence("训练用了多少 GPU？")

    assert len(evidence) == 2
    first = evidence[0].to_dict()
    assert first["rank"] == 1
    assert first["page"] == 7
    assert first["score_type"] == "rrf"
    assert first["channels"] == ["dense"]
    assert "NVIDIA P100" in first["text"]


async def test_rag_context_survives_retrieve_error(monkeypatch):
    def boom(question, top_k=4, source=None):
        raise RuntimeError("chroma 没建好")

    monkeypatch.setattr(agent_module, "retrieve_scored", boom)
    assert await _rag_context("任何问题") == ""


async def test_rag_context_empty_when_no_hits(monkeypatch):
    monkeypatch.setattr(agent_module, "retrieve_scored", lambda q, top_k=4, source=None: [])
    assert await _rag_context("库里没有的问题") == ""


@pytest.mark.asyncio
async def test_run_agent_passes_context_to_model(monkeypatch):
    captured: list[list[dict]] = []

    def fake_retrieve(question, top_k=4, source=None):
        return [
            (Chunk(text="We trained on 8 NVIDIA P100 GPUs", source="p.pdf", page=7), 0.06, ["dense"])
        ]

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs["messages"])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="答案 [来源: p.pdf 第7页]", tool_calls=None)
                    )
                ]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    monkeypatch.setattr(agent_module, "get_client", lambda: fake_client)

    result = await run_agent("训练用了多少 GPU？")

    assert result.answer.startswith("答案")
    messages = captured[0]
    # 第 0 条是系统人设，第 1 条必须是检索到的参考资料
    assert messages[0]["role"] == "system"
    assert "p.pdf 第7页" in messages[1]["content"]
    assert messages[-1]["role"] == "user"


def test_system_prompt_has_four_sections():
    """L1：回答必须按【结论/依据/局限/下一步】四段输出，否则前端和文档口径就变了。"""
    prompt = agent_module.SYSTEM_PROMPT
    for section in ["【结论】", "【依据】", "【局限】", "【下一步】"]:
        assert section in prompt
    # 依据段必须要求带来源，否则会退化成"没有出处的分析"
    assert "句末标注 [来源: 文件名 第N页]" in prompt


async def test_multi_source_retrieval_merges_per_paper(monkeypatch):
    """多篇对比：每篇各查一次（各取 3 块），证据里两篇都在，且提示模型出对比表。"""
    calls: list[tuple[int, str | None]] = []

    def fake_retrieve(question, top_k=4, source=None):
        calls.append((top_k, source))
        if source == "a.pdf":
            return [(Chunk(text="A 的方法", source="a.pdf", page=1), 0.05, ["dense"])]
        return [(Chunk(text="B 的方法", source="b.pdf", page=2), 0.06, ["bm25"])]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    context, evidence = await _rag_context_with_evidence(
        "这两篇的方法有什么区别？", sources=["a.pdf", "b.pdf"]
    )

    assert [c[1] for c in calls] == ["a.pdf", "b.pdf"]
    assert all(c[0] == agent_module.PER_PAPER_TOP_K for c in calls)
    assert {e.source for e in evidence} == {"a.pdf", "b.pdf"}
    assert "多篇对比" in context


async def test_multi_source_survives_one_failure_and_caps_evidence(monkeypatch):
    """一篇检索炸了不影响另一篇；合并后证据不超过上限。"""

    def fake_retrieve(question, top_k=4, source=None):
        if source == "bad.pdf":
            raise RuntimeError("索引坏了")
        return [
            (Chunk(text=f"片段{i}", source="good.pdf", page=i), 1 / (i + 1), ["hash"])
            for i in range(10)
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    context, evidence = await _rag_context_with_evidence(
        "对比一下", sources=["bad.pdf", "good.pdf"]
    )

    assert context  # 好的一篇仍然进了上下文
    assert {e.source for e in evidence} == {"good.pdf"}
    assert len(evidence) <= agent_module.MULTI_MAX_EVIDENCE


@pytest.fixture(autouse=True)
def _no_translate(monkeypatch):
    """单测里不要真的调 LLM 做查询翻译：直接原样返回。"""

    async def _fake(q):
        return q

    monkeypatch.setattr(agent_module, "_search_query", _fake)


def test_numeric_excerpt_keeps_numbers_beyond_first_1600_chars():
    """按字符截前 1600 字会切掉摘要里的数字（实测 61.4% 在第 1980 个字符）。

    所以整页补充要按"含数字的行"取样，既保数字也不把无关正文塞进上下文。
    """
    filler = "\n".join(["无数字的说明行 " * 30] * 20)  # 约 3000 字，把数字挤到 1600 之后
    text = filler + "\non the Path-X challenge (seq. length 16K, 61.4% accuracy)"

    excerpt = agent_module._numeric_excerpt(text)

    assert "61.4% accuracy" in excerpt
    assert "无数字" not in excerpt
    assert len(excerpt) <= 1604


# ---------------- 多智能体角色时间线 ----------------
# 官方赛题「AI 技术深度」点名要「多智能体协同」。这里不新增 LLM 调用，
# 只是把本来就在跑的四个阶段显式命名并留下可核验的产物。


async def test_rag_context_records_four_roles_in_order(monkeypatch):
    """Planner → Retriever → Extractor → Verifier，顺序固定且每条都有摘要。"""

    def fake_retrieve(question, top_k=4, source=None):
        return [
            (Chunk(text="8 NVIDIA P100 GPUs", source="p.pdf", page=7), 0.057, ["dense"]),
            (Chunk(text="28.4 BLEU", source="p.pdf", page=8), 0.055, ["bm25"]),
        ]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    roles: list = []
    _context, evidence = await _rag_context_with_evidence(
        "训练用了多少 GPU？", roles=roles
    )

    assert [r.role for r in roles] == [
        agent_module.ROLE_PLANNER,
        agent_module.ROLE_RETRIEVER,
        agent_module.ROLE_EXTRACTOR,
        agent_module.ROLE_VERIFIER,
    ]
    assert all(r.summary for r in roles)
    assert all(r.ok for r in roles)
    # Retriever 的摘要要能看出召回了几段、覆盖几篇
    retriever = roles[1]
    assert "召回 2 段" in retriever.summary
    assert retriever.detail["chunks"] == 2
    # Verifier 只做能自动判定的部分：证据都有页码就算通过
    assert "2 条可定位到具体页码" in roles[3].summary
    assert evidence  # 证据照样产出


async def test_verifier_flags_evidence_without_page(monkeypatch):
    """证据全缺页码时 Verifier 必须标 ok=False——引用无法溯源是硬伤。"""

    def fake_retrieve(question, top_k=4, source=None):
        return [(Chunk(text="没有页码的片段", source="p.pdf", page=None), 0.05, ["hash"])]

    monkeypatch.setattr(agent_module, "retrieve_scored", fake_retrieve)
    roles: list = []
    await _rag_context_with_evidence("随便问问", roles=roles)

    verifier = [r for r in roles if r.role == agent_module.ROLE_VERIFIER][0]
    assert verifier.ok is False
    assert "缺页码" in verifier.summary


async def test_verifier_flags_empty_evidence(monkeypatch):
    """一条证据都没有时，Verifier 要明说会按资料不足作答。"""
    monkeypatch.setattr(agent_module, "retrieve_scored", lambda q, top_k=4, source=None: [])
    roles: list = []
    await _rag_context_with_evidence("库里没有的问题", roles=roles)

    verifier = [r for r in roles if r.role == agent_module.ROLE_VERIFIER][0]
    assert verifier.ok is False
    assert "资料不足" in verifier.summary


async def test_retriever_role_marked_failed_when_index_breaks(monkeypatch):
    """检索炸了要留下失败记录，不能悄悄降级成"没找到"。"""

    def boom(question, top_k=4, source=None):
        raise RuntimeError("chroma 没建好")

    monkeypatch.setattr(agent_module, "retrieve_scored", boom)
    roles: list = []
    await _rag_context_with_evidence("任何问题", roles=roles)

    failed = [r for r in roles if not r.ok]
    assert failed, "检索失败必须有 ok=False 的角色记录"
    assert failed[0].role == agent_module.ROLE_RETRIEVER


def test_role_labels_cover_every_role():
    """前端直接显示 label，漏一个就会出现英文裸 id。"""
    for role in [
        agent_module.ROLE_PLANNER,
        agent_module.ROLE_RETRIEVER,
        agent_module.ROLE_EXTRACTOR,
        agent_module.ROLE_VERIFIER,
        agent_module.ROLE_WRITER,
    ]:
        assert role in agent_module.ROLE_LABELS
        assert agent_module.ROLE_LABELS[role].strip()


def test_role_run_to_dict_shape():
    """前端 renderRoles 读的是 role/label/summary/duration_ms/detail/ok 这几个键。"""
    run = agent_module.RoleRun(
        role=agent_module.ROLE_PLANNER, summary="全库检索", duration_ms=12
    )
    d = run.to_dict()

    assert set(d) == {"role", "label", "summary", "duration_ms", "detail", "ok"}
    assert d["label"] == agent_module.ROLE_LABELS[agent_module.ROLE_PLANNER]
    assert d["duration_ms"] == 12
    assert d["ok"] is True


def test_make_status_reports_counts():
    """状态回显：证据数 / 覆盖论文 / 页码。"""
    ev1 = agent_module.Evidence(
        rank=1, source="a.pdf", page=2, score=1.0, channels=["dense"], text="x"
    )
    ev2 = agent_module.Evidence(
        rank=2, source="b.pdf", page=11, score=0.9, channels=["bm25"], text="y"
    )

    status = agent_module._make_status([ev1, ev2], [])

    assert "证据=2" in status
    assert "覆盖论文=2" in status
    assert "页码=2,11" in status


def test_make_status_says_no_evidence():
    status = agent_module._make_status([], [])
    assert "证据=0" in status
    assert "资料不足" in status