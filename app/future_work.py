"""未来工作探索（赛题 1.5.4）：从作者自述的出路上找方向，再去库外查有没有人做过。

设计要点——为什么不全交给模型：

    Extractor  纯代码。先用 structures.json 的章节树定位 Conclusion / Future Work /
               Limitations 这些节，切不到再退回全文扫描。每条结论都带
               ``extraction_status``，**抽不到就如实记为 no_section，不编**。
    Mapper     纯代码。方向骨架由图谱事实决定（桥接、孤岛、簇间空隙、被引为 0），
               模型只在给定方向的范围内挑值得写的，不凭空发明方向。
    Searcher   纯代码。对每个方向复用 literature.search_arxiv() 去库外检索最新论文。
    Judge      唯一调模型的阶段。比较研究问题 / 方法 / 实验，给保守结论：
               「可能已被做过」/「没检索到高度相似的工作」。
               **任何情况下都不得输出「原创」这类断言。**

为什么不能宣称「原创」：
    我们只查了 arXiv 一个来源，且检索词由方向描述生成，召回必然不全。
    「没搜到」只说明这一次查询没命中，不构成新颖性证明。赛题原文也只要求
    「不要提出已被最新论文尝试过的 idea」，所以这一步的职责是**排雷**，不是**认证**。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from app.llm import chat_stream as llm_chat_stream
from app.logging_config import get_logger
from app.security import BudgetExceeded, reserve_llm_call

logger = get_logger(__name__)

# --------------------------------------------------------------------------- 常量

#: Future Work 的抽取结果状态。答辩时要能展示「处理不了的情况」，所以状态是显式的。
#:
#: - success            切到章节且抽到了有实质内容的句子
#: - no_section         章节树里没有 Future Work / Conclusion 这类节
#: - no_valid_sentence  切到了节，但里面没有可用的句子（只有图表、纯公式、过短）
#: - parse_error        PDF 打不开或抽取过程抛错
EXTRACTION_STATUS = ("success", "no_section", "no_valid_sentence", "parse_error")

#: 章节标题的正则。按「明确指向未来工作」→「弱指向」排优先级匹配。
SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("future_work", re.compile(r"future\s*(work|works|research|direction|directions)", re.I)),
    ("limitations", re.compile(r"limitation|limitations", re.I)),
    ("discussion", re.compile(r"discussion|concluding\s+remarks|broader\s+impact", re.I)),
    ("conclusion", re.compile(r"conclusion|conclusions|summary\s+and\s+conclusion", re.I)),
)

#: 全文扫描时的句子特征。只在章节定位失败时用。
#:
#: 这里只收「作者在说自己接下来要做什么 / 做不到什么」的实词。
#: 不要加 we hope / we believe / we expect 这类期许——实测它们抽出来的是
#: 「我们希望开放发布能促进社区创新」这种价值观表态，不是研究方向，
#: 塞进「作者原话」里会让答辩显得很空。
SENTENCE_CUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(future work|future research|future direction|further work|future studies)\b", re.I),
    re.compile(r"\b(we (plan to|intend to|will extend|will explore|leave .{0,30}to future|propose to))\b", re.I),
    re.compile(r"\b(remain(s)? (an? )?(open|unresolved|unexplored)|open question|not yet)\b", re.I),
    re.compile(r"\b(limitation|limited to|suffer from|cannot|fails? to|could not)\b", re.I),
    re.compile(r"(未来工作|进一步研究|后续工作|有待|尚未|局限性|不足之处)"),
)

#: 一个方向最少要有多长的原文证据，太短的句子没有信息量。
MIN_SENTENCE_CHARS = 30
#: 每篇论文最多留几句（避免一篇长文把方向撑爆）。
MAX_SENTENCES_PER_PAPER = 3
#: 每个方向最多带几篇原文证据。
MAX_EVIDENCE_PER_DIRECTION = 5
#: 一次探索最多产出几个方向（赛题口径是「一些方向」，3~5 个合适）。
MAX_DIRECTIONS = 5
#: Judge 阶段的模型调用上限（一个方向一次）。
MAX_JUDGE_CALLS = MAX_DIRECTIONS
#: 单次 Judge 的超时。
JUDGE_TIMEOUT_SECONDS = 60.0
#: 每个方向去 arXiv 检索几篇。
ARXIV_PER_DIRECTION = 5
#: 每类图谱缺口最多占几个方向。
#:
#: 不设这个上限时，桥接论文会因为中介中心性天然偏高而把名额全占满——实测 5 个方向
#: 里 3 个都是「跨簇交界」，剩下的孤岛/低被引一个都进不来，看着像只做了一种线索。
#: 取四分之一（5 → 1），保证三类缺口各露一次脸；第一轮配额没凑满时，
#: 第二轮补位会拿走剩余名额——那是有意的：宁可有 3 个桥接，也不要只有 2 个方向。
MAX_PER_GAP_KIND = max(1, MAX_DIRECTIONS // 4)

_JUDGE_SYSTEM = (
    "你在帮研究者判断一个研究方向是否已经被最新论文做过。"
    "只根据给出的论文标题与摘要判断，不要臆测。"
    "你的职责是排雷，不是认证：即使没有找到高度相似的工作，也只能说「没检索到」，"
    "绝对不要使用『原创』『首次』『没人做过』这类断言。"
    "必须输出 JSON，不要输出任何其他文字。"
)

#: 禁止出现在判断理由里的断言词。
#:
#: prompt 里已经写了「不要用」，但模型的服从性不能当保证：实测同一段 prompt，
#: 十次里有几次仍会冒出「这是完全原创的工作」。赛题原文要求是「不要提出已被最新
#: 论文尝试过的 idea」——我们只能证明「搜到了相似的」，永远无法证明「没人做过」。
#: 所以要做服务端硬替换，而不是只靠一句叮嘱。前端 graph.html 里还有一道同名兜底
#: （DIR_FORBIDDEN），两道都要在，因为事件流可能被别的客户端消费。
_ORIGINALITY_WORDS = ("原创", "首创", "首次提出", "首次做", "没人做过", "无人涉足", "独一无二")

#: 命中禁用词时替换成的话。
_NEUTRAL_REASON = "尚待核实"


# --------------------------------------------------------------------------- 工具


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _clean_sentence(text: str) -> str:
    """压空白、去页眉页码残留。"""
    out = re.sub(r"\s+", " ", _as_text(text))
    return out.strip()


#: 明显不是正文的东西：邮箱、URL、纯引用编号、图表残渣。
_NOISE_RES = (
    re.compile(r"[\w.+-]+@[\w.-]+\.\w+"),          # 邮箱
    re.compile(r"https?://\S+"),                     # URL
    re.compile(r"^\s*\[\d+(,\s*\d+)*\]"),            # 开头的参考文献编号
    re.compile(r"\b(Fig\.|Figure|Table|Eq\.)\s*\d+", re.I),  # 图表引用
)


def _looks_like_prose(sent: str) -> bool:
    """判一句话像不像论文正文，而不是邮箱串、公式行、参考文献行。

    之前只有「字母数字占比」一条，结果 ``hinton@cs.toronto.edu tion cannot be
    applied to...`` 这种页眉残留被判成了有效句子。这里再加几道：
        · 含邮箱 / URL 的整句丢掉（多半是页面头部）
        · 以参考文献编号开头的丢掉
        · 词数少于 8 的丢掉（Our code is available. 这类没有方向信息）
        · 「Table 3」这类图表引用出现在句首的丢掉
    """
    if not sent:
        return False
    if any(p.search(sent) for p in _NOISE_RES[:2]):
        return False
    if _NOISE_RES[2].search(sent):
        return False
    words = re.findall(r"[A-Za-z]{2,}", sent)
    if len(words) < 8:
        return False
    if re.match(r"^(\s*)(Fig\.|Figure|Table|Eq\.)", sent, re.I):
        return False
    return True


def _split_sentences(text: str) -> list[str]:
    """按句切分。英文句号要求后面跟空格，避免把 3.5 这类数字切断。"""
    parts = re.split(r"(?<=[.!?])\s+|(?<=[。！？])", text or "")
    return [_clean_sentence(p) for p in parts if _clean_sentence(p)]


# --------------------------------------------------------------------------- Extractor


def _match_section(title: str) -> str:
    """章节标题 → 命中的类别名，没命中返回空串。"""
    for name, pattern in SECTION_PATTERNS:
        if pattern.search(title or ""):
            return name
    return ""


def _section_body(
    pages: dict[int, str],
    title: str,
    start_page: int,
    end_page: int,
) -> str:
    """从起始页起找到标题出现的位置，切到下一个标题或页尾。

    章节树只给了页码和顺序，没有正文偏移，所以这里用标题文本在页内定位。
    页码口径：structures.json 是 0 基，page_texts() 是 1 基，故 +1。
    """
    first = start_page + 1
    last = max(first, end_page + 1)
    collected: list[str] = []
    started = False
    for pno in range(first, last + 1):
        page = pages.get(pno) or ""
        if not started:
            pos = page.find(title)
            if pos < 0:
                # 标题可能跨行或带编号，退一步找前 20 个字符
                key = title[:20]
                pos = page.find(key) if key else -1
            if pos < 0:
                continue
            started = True
            collected.append(page[pos + len(title) :])
        else:
            collected.append(page)
        if pno == last:
            break
    return "\n".join(collected)


def _harvest_sentences(body: str) -> list[str]:
    """从一节正文里挑出像「下一步要做什么」的句子。"""
    picked: list[str] = []
    for sent in _split_sentences(body):
        if len(sent) < MIN_SENTENCE_CHARS:
            continue
        # 图表残留：数字、符号占比过高
        alnum = sum(ch.isalnum() or ch.isspace() for ch in sent)
        if alnum / max(len(sent), 1) < 0.75:
            continue
        # 页眉邮箱、参考文献编号、图表引用这类噪声先剔掉，再看好不好用
        if not _looks_like_prose(sent):
            continue
        if any(cue.search(sent) for cue in SENTENCE_CUES):
            picked.append(sent)
        if len(picked) >= MAX_SENTENCES_PER_PAPER:
            break
    return picked


def extract_future_work(
    filename: str,
    structure: dict[str, Any],
    pages: dict[int, str],
) -> dict[str, Any]:
    """从单篇论文抽 Future Work。返回带 extraction_status 的字典。

    不抛异常：任何失败都变成一个 status，好让上层统计「多少篇抽不到」。
    """
    out: dict[str, Any] = {
        "source": filename,
        "title": _as_text(structure.get("title")),
        "status": "no_section",
        "kind": "",
        "section": "",
        "sentences": [],
    }
    try:
        sections = structure.get("sections") or []
        best: tuple[int, str, str] | None = None  # (优先级, 类别, 标题)
        for sec in sections:
            title = _as_text(sec.get("title"))
            kind = _match_section(title)
            if not kind:
                continue
            prio = [n for n, _ in SECTION_PATTERNS].index(kind)
            if best is None or prio < best[0]:
                best = (prio, kind, title)
            # 明确指向 future work 的节直接定了，不再往后找
            if kind == "future_work":
                break
        if best is None:
            return out

        kind, title = best[1], best[2]
        sec_index = next(
            i for i, s in enumerate(sections) if _as_text(s.get("title")) == title
        )
        start = int(sections[sec_index].get("page") or 0)
        end = start
        for later in sections[sec_index + 1 :]:
            end = max(end, int(later.get("page") or 0))
            if end > start:
                break

        body = _section_body(pages, title, start, max(start, end - 1))
        sentences = _harvest_sentences(body)
        if not sentences:
            # 章节定位失败时退回全文扫描（用户选定的口径：章节优先，失败退全文）
            full = "\n".join(pages.get(p, "") for p in sorted(pages))
            sentences = _harvest_sentences(full)
            if sentences:
                out.update(
                    status="success", kind=kind, section=f"{title}（全文兜底）", sentences=sentences
                )
                return out
            out.update(status="no_valid_sentence", kind=kind, section=title)
            return out

        out.update(status="success", kind=kind, section=title, sentences=sentences)
        return out
    except Exception:  # noqa: BLE001 —— 单篇解析失败不该中断整批
        logger.warning("抽取 Future Work 失败：%s", filename, exc_info=True)
        out.update(status="parse_error", sentences=[])
        return out


def extraction_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """统计各状态的篇数。答辩时要展示「处理不了的情况」，这个数就是证据。"""
    counts = {s: 0 for s in EXTRACTION_STATUS}
    for item in items:
        status = item.get("status") or "parse_error"
        counts[status] = counts.get(status, 0) + 1
    total = len(items)
    got = counts.get("success", 0)
    return {
        "total": total,
        "counts": counts,
        "coverage": round(got / total, 3) if total else 0.0,
        "sentence_total": sum(len(i.get("sentences") or []) for i in items),
    }


# --------------------------------------------------------------------------- Mapper


def _graph_gaps(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """从图谱里挑出「结构上像缺口」的位置，作为方向的候选骨架。

    四类：
        bridge   跨簇桥接——连接两个引用簇、但不属于基石也不属于枢纽的论文，
                 它们指出去的地方往往是两个方向的交界。
        island   未被接入引用网——出入边都为 0，说明库里没人和它建立关系。
        lowcite  被引为 0 或极低——处在网络边缘，方向可能还没成型。

    字段口径（踩过坑，记下来）：
        · 节点的 id 是 ``arxiv:1706.03762`` / ``hash:88bdceb3c19a`` 这种规范 id，
          而 extractions 的键是**文件名**（``1706.03762v7.pdf``）。两者对不上，
          必须走 ``copies``——那是图谱里存的真实文件名列表，第一个就是跳阅读页用的那个。
          之前直接拿 ``n["id"]`` 去查，结果 5 个方向全是「证据 0 条」。
        · 簇号字段叫 ``community``，不是 ``cluster``。读错键会退化成 ``?``。
    """
    nodes = {n["id"]: n for n in graph.get("nodes") or [] if n.get("id")}
    gaps: list[dict[str, Any]] = []

    # 主题（catalog 的 category）只在汇总的那个模块里算，这里按需补一次；
    # 它既是「主题自选」的过滤依据，也让每个方向能报出自己属于哪个主题。
    from app.graph import node_meta

    theme_of: dict[str, str] = {}
    for nid, node in nodes.items():
        meta = node_meta(nid, _as_text(node.get("title")), _as_text(node.get("arxiv_id")))
        theme_of[nid] = _as_text(meta.get("category"))

    for n in nodes.values():
        role = _as_text(n.get("role"))
        cite_in = n.get("cited_by") or 0
        cite_out = n.get("cites") or 0
        title = _as_text(n.get("title")) or n["id"]
        copies = [c for c in (n.get("copies") or []) if c]
        # 证据要按文件名去 extractions 里查，所以把副本文件名一起带出去
        community = n.get("community")
        wheel = "" if community is None or int(community) < 0 else str(int(community))
        theme = theme_of.get(n["id"], "")
        if role == "bridge":
            gaps.append(
                {
                    "kind": "bridge",
                    "label": "跨簇交界",
                    "paper": n["id"],
                    "copies": copies,
                    "title": title,
                    "theme": theme,
                    "wheel": wheel,
                    "basis": f"中介中心性 {n.get('betweenness', '?')}，"
                    f"位于引用关系簇 {wheel or '未划分'}，出 {cite_out} / 入 {cite_in} 条引用",
                }
            )
        if cite_in == 0 and cite_out == 0:
            gaps.append(
                {
                    "kind": "island",
                    "label": "未接入引用网",
                    "paper": n["id"],
                    "copies": copies,
                    "title": title,
                    "theme": theme,
                    "wheel": wheel,
                    "basis": "库内既没有引用它、也没有它引用别的论文的边",
                }
            )
        elif cite_in == 0:
            gaps.append(
                {
                    "kind": "lowcite",
                    "label": "库内无人引用",
                    "paper": n["id"],
                    "copies": copies,
                    "title": title,
                    "theme": theme,
                    "wheel": wheel,
                    "basis": f"出 {cite_out} 条引用，但库内没有任何论文引用它",
                }
            )
    # 桥接优先，其次孤岛，再次低被引；同类按标题稳定排序，保证可复现
    order = {"bridge": 0, "island": 1, "lowcite": 2}
    gaps.sort(key=lambda g: (order.get(g["kind"], 9), g["title"]))
    return gaps


def build_directions(
    graph: dict[str, Any],
    extractions: list[dict[str, Any]],
    limit: int = MAX_DIRECTIONS,
    themes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """把图谱缺口与作者自述拼成方向候选。纯代码，不调模型。

    排序分两轮，先保证「有作者原话的方向」排在前面：
        第一轮只收 evidence 非空且标了 future_work 节的缺口——作者自己写明的
        「下一步要做什么」，这是赛题要的东西，不能因为图谱分数低就被挤掉；
        第二轮用剩下的缺口补足到 limit，避免方向数不够。
    这样一个方向都没有时也不会空手而归，但更不会出现「5 个方向全没有原文」的情况。

    ``themes`` 是按主题自选：只保留锚点论文属于这些主题（catalog 的 category）的缺口。
    空列表 / None 表示全部主题——与「不勾论文 = 全库」同一口径，不表示「一个都不要」。
    """
    by_paper = {e["source"]: e for e in extractions if e.get("status") == "success"}
    gaps = _graph_gaps(graph)

    wanted = {_as_text(t).strip() for t in (themes or []) if _as_text(t).strip()}
    if wanted:
        gaps = [g for g in gaps if _as_text(g.get("theme")) in wanted]
        if not gaps:
            # 勾的主题里一个缺口都没有：如实返回空，让调用方去说清楚，
            # 而不是悄悄放宽成全库——那会让用户以为筛选没生效。
            return []

    def lookup(gap: dict[str, Any]) -> dict[str, Any] | None:
        # 一个节点可能有多份副本文件，挨个试，命中即用
        for name in gap.get("copies") or []:
            hit = by_paper.get(name)
            if hit:
                return hit
        return None

    scored: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
    for gap in gaps:
        evidence = lookup(gap)
        # 证据来自这篇论文自己写的 future work / limitations；抽不到就如实留空
        sentences = (evidence.get("sentences") if evidence else []) or []
        # 作者明确写的 Future Work / Limitations 优先，普通 Conclusion 次之
        kind = _as_text((evidence or {}).get("kind"))
        rank = 0 if (sentences and kind in ("future_work", "limitations")) else (1 if sentences else 2)
        scored.append((rank, gap, evidence))
    scored.sort(key=lambda t: t[0])  # sort 稳定，同档保留桥接→孤岛→低被引的原顺序

    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    used_per_kind: dict[str, int] = {}

    def take(gap: dict[str, Any], evidence: dict[str, Any] | None) -> None:
        seen.add(gap["title"])
        used_per_kind[gap["kind"]] = used_per_kind.get(gap["kind"], 0) + 1
        sentences = (evidence.get("sentences") if evidence else []) or []
        picked.append(
            {
                "id": f"dir-{len(picked) + 1}",
                "kind": gap["kind"],
                "kind_label": gap["label"],
                "theme": gap.get("theme") or "",
                "anchor": gap["paper"],
                "anchor_title": gap["title"],
                "anchor_file": (gap.get("copies") or [""])[0],
                "basis": gap["basis"],
                "evidence_status": evidence.get("status") if evidence else "no_section",
                "evidence_section": (evidence or {}).get("section", ""),
                "evidence_kind": (evidence or {}).get("kind", ""),
                "evidence": sentences[:MAX_EVIDENCE_PER_DIRECTION],
            }
        )

    # 第一轮：受配额约束，让三类缺口都有机会露脸
    for _rank, gap, evidence in scored:
        if len(picked) >= limit:
            break
        if gap["title"] in seen:
            continue
        # 同类缺口有配额：满了先跳过，等下面补位那一轮再收。
        # 没有这个配额，3 个桥接会把 5 个名额吃掉（实测如此），
        # 孤岛/低被引这两条线索就永远进不了结果。
        if used_per_kind.get(gap["kind"], 0) >= MAX_PER_GAP_KIND:
            continue
        take(gap, evidence)

    # 第二轮：配额导致没凑满时按原顺序补齐——宁可多给一个桥接，也不要少于 3 个方向
    for _rank, gap, evidence in scored:
        if len(picked) >= limit:
            break
        if gap["title"] in seen:
            continue
        take(gap, evidence)
    return picked


def build_topic_query(direction: dict[str, Any]) -> str:
    """把方向变成一条 arXiv 检索词。

    只用标题里的实词，不把中文标签混进去（arXiv 检索接口对中文不友好）。
    """
    title = direction.get("anchor_title") or ""
    words = [w for w in re.split(r"[^\w\-]+", title) if len(w) >= 3]
    return " ".join(words[:8]) or title


# --------------------------------------------------------------------------- 缺口矩阵
# 为什么是「主题 × 技术手段」而不是 Consensus 的「topic × population」：
# population 是医学/社科的概念，计算机论文里没有对应物。真正能说「这个主题下
# 没人用这个手段」的，是「主题（研究什么）」与「手段（用模型/训练/推理/评测）」
# 这两条轴——空格子就是一个具体、可核对的组合空缺。
TECH_AXES: tuple[tuple[str, str], ...] = (
    ("model", "模型结构"),
    ("train", "训练与数据"),
    ("infer", "推理与效率"),
    ("eval", "评测与基准"),
    ("apply", "应用与落地"),
)

# 归类用的关键词（按命中强度分两档）。
#
# 为什么要分档：摘要里几乎每篇都会出现 "model"，只按固定顺序取第一个命中，
# 14 篇基础模型论文会有 13 篇全落进「模型结构」，矩阵就没信息量了（实测如此）。
# 规则：强词（专门描述这条手段的说法）优先；弱词（architecture/neural 这类
# 到处都有的）只在标题里命中才算数，不扫摘要。
_TECH_STRONG: dict[str, tuple[str, ...]] = {
    # 这类说法只在「论文主体讲模型怎么搭」时才出现，光有 "model" 不算。
    "model": (
        "encoder-decoder", "decoder-only", "encoder-only", "multi-head attention",
        "self-attention", "mixture of experts", "architecture design",
        "we introduce a new architecture", "model architecture",
    ),
    "infer": (
        "inference cost", "inference time", "inference latency", "latency",
        "throughput", "quantiz", "distill", "prun", "speedup", "compression",
        "kv cache", "memory footprint", "serving", "inference efficien",
        "reduc the inference", "compute cost",
    ),
    "eval": (
        # "benchmark" 不能单独算强信号：LLaMA/Mistral/GPT-4 的摘要里都会出现
        # "we evaluate on benchmarks"，那是自评，不是「论文主题是评测」。
        # 只有连「提出/建立/系统评测」这类动宾一起出现，才算真的在做评测。
        "we propose a benchmark", "new benchmark", "introduce a benchmark",
        "evaluation suite", "systematically evaluate", "leaderboard",
        "robustness", "fairness", "safety", "alignment", "hallucinat",
        "human evaluation", "metric",
    ),
    "train": (
        "pre-train", "pretrain", "fine-tun", "finetun", "reinforcement learning",
        "training data", "training set", "optimiz", "scaling law", "rlhf",
        "distributed training", "instruction tuning", "pretraining corpus",
    ),
    "apply": (
        "we apply", "deploy", "in production", "product", "clinical", "medical",
        "enterprise", "customer", "real-world",
    ),
}
_TECH_WEAK_TITLE: dict[str, tuple[str, ...]] = {
    "model": ("transformer", "attention", "network", "layer", "gpt", "bert", "neur", "architecture"),
    "infer": ("efficient", "fast", "cheap", "small", "compression", "quantiz", "distill"),
    "eval": ("benchmark", "test", "study", "analy", "survey", "evaluat"),
    "train": ("train", "pre-train", "finetun", "dataset", "data", "scaling"),
    "apply": ("agent", "app", "tool", "code", "search", "recommend", "dialog", "system"),
}
_TECH_KEYWORDS = _TECH_STRONG  # 兼容旧引用
OTHER_AXIS = ("other", "其它")


def _axis_of(text: str, title: str = "") -> tuple[str, str]:
    """把一篇论文归到某个技术手段轴。

    先扫强词（摘要也算），再扫弱词（只看标题），都不中归 other。
    宁可归 other 也不硬塞：矩阵的意义是「这个主题下确实没人这么做过」，
    靠模糊匹配凑出来的非零格子会让缺口消失，那比多出来一个 other 列糟糕得多。
    """
    low = (text or "").lower()
    for key, label in TECH_AXES:
        if any(kw in low for kw in _TECH_STRONG.get(key, ())):
            return key, label
    low_title = (title or text or "").lower()
    for key, label in TECH_AXES:
        if any(kw in low_title for kw in _TECH_WEAK_TITLE.get(key, ())):
            return key, label
    return OTHER_AXIS


def build_gap_matrix(
    graph: dict[str, Any],
    directions: list[dict[str, Any]],
) -> dict[str, Any]:
    """把方向摊成「主题 × 技术手段」的矩阵。

    行 = 归纳出的方向主题（锚点论文在 catalog 里的 category），列 = TECH_AXES 五类技术手段。
    每格给出库内该主题下命中该手段的论文篇数与标题——**空格子就是可能的缺口**，
    且这个 0 是可核对的（扫的是图谱里同一主题的原型论文）。

    主题只有 graph_view 的节点里没有，得走 node_meta() 从 catalog 补（survey() 也是这么补的）。
    补不到主题的论文自成一个主题行，避免把年份未知/元数据缺失的论文硬塞进别人的主题。
    """
    from app.graph import node_meta

    by_paper: dict[str, dict[str, Any]] = {}
    meta_of: dict[str, dict[str, Any]] = {}

    def meta_for(node: dict[str, Any]) -> dict[str, Any]:
        key = str(node.get("id") or "")
        if key not in meta_of:
            meta_of[key] = node_meta(key, str(node.get("title") or ""), str(node.get("arxiv_id") or ""))
        return meta_of[key]

    for node in (graph.get("nodes") or []):
        for copy in (node.get("copies") or []):
            by_paper.setdefault(str(copy), node)

    # ---- 行：方向主题
    themes: list[dict[str, Any]] = []
    seen_theme: set[str] = set()
    for d in directions:
        anchor = by_paper.get(str(d.get("anchor_file") or ""))
        if not anchor:
            anchor = by_paper.get(str(d.get("anchor") or ""))
        title = str(d.get("title") or d.get("anchor_title") or "")
        category = str((meta_for(anchor) if anchor else {}).get("category") or "").strip()
        key = category or title
        if key in seen_theme:
            continue
        seen_theme.add(key)
        themes.append({
            "key": key,
            "theme": category or title,
            "theme_has_category": bool(category),
            "from_direction": d.get("id"),
            "anchor_file": d.get("anchor_file"),
        })
    if not themes:
        return {"ready": False, "detail": "还没有归纳出方向，无法生成矩阵"}

    # ---- 库内原型论文按「主题 -> [论文]」分组，用于逐格核对
    # 关键词匹配用 标题 + 摘要首句：光看标题，像 "Attention Is All You Need"
    # 这种既不带 training 也不带 inference 的标题会全落到 other，矩阵就没信息量了。
    pool_by_theme: dict[str, list[tuple[str, str, str]]] = {}
    for node in (graph.get("nodes") or []):
        meta = meta_for(node)
        cat = str(meta.get("category") or "").strip()
        if not cat:
            continue
        text = str(node.get("title") or "") + " " + str(meta.get("abstract") or "")[:400]
        pool_by_theme.setdefault(cat, []).append(
            (str(node.get("title") or node.get("id") or ""), text, str(node.get("title") or ""))
        )

    rows: list[dict[str, Any]] = []
    for theme in themes:
        members = pool_by_theme.get(theme["theme"]) or []
        cells: list[dict[str, Any]] = []
        for axis_key, axis_label in TECH_AXES:
            hits = [t for t, text, title in members if _axis_of(text, title)[0] == axis_key]
            cells.append({
                "axis": axis_key,
                "axis_label": axis_label,
                "count": len(hits),
                "titles": hits[:5],
                # 空格子 = 本主题在库内没有任何一篇走这条手段
                "gap": not hits,
                "note": "库内 0 篇" if not hits else "",
            })
        rows.append({
            "theme": theme["theme"],
            "theme_has_category": theme["theme_has_category"],
            "from_direction": theme["from_direction"],
            "pool": len(members),
            "cells": cells,
        })

    gaps = sum(1 for r in rows for c in r["cells"] if c["gap"])
    return {
        "ready": True,
        "axes": [{"axis": k, "label": v} for k, v in TECH_AXES],
        "rows": rows,
        "gap_cells": gaps,
        "legend": "行是方向所在的主题（取自 catalog 的 category），列是技术手段。"
                  "格子里是库内该主题下走这条手段的论文篇数，「库内 0 篇」的空格子是可能的缺口——"
                  "它只说明我们库里这批论文没有覆盖到这条组合，不等于文献里也没有，"
                  "要下判断还得看每个方向检索到的库外论文。",
    }


# --------------------------------------------------------------------------- Searcher


def search_latest(query: str, limit: int = ARXIV_PER_DIRECTION) -> list[dict[str, Any]]:
    """库外检索。失败一律返回空列表，由上层如实记为「未检索到」。"""
    try:
        from app.literature import search_arxiv

        candidates = search_arxiv(query, limit=limit)
    except Exception:  # noqa: BLE001 —— 检索失败不该炸掉整个探索流程
        logger.warning("arXiv 检索失败：%s", query, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for c in candidates or []:
        out.append(
            {
                "title": _as_text(getattr(c, "title", "")),
                "year": getattr(c, "year", None),
                "source": _as_text(getattr(c, "source", "")) or "arXiv",
                "url": _as_text(getattr(c, "url", "")),
                "arxiv_id": _as_text(getattr(c, "arxiv_id", "")),
                "abstract": _as_text(getattr(c, "abstract", ""))[:600],
            }
        )
    return out


# --------------------------------------------------------------------------- Judge


def build_judge_prompt(direction: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    lines = [
        f"研究方向：{direction.get('anchor_title', '')}",
        f"图谱依据：{direction.get('basis', '')}",
    ]
    if direction.get("evidence"):
        lines.append("作者本人在原文里给出的线索：")
        lines.extend(f"- {s}" for s in direction["evidence"])
    if not candidates:
        lines.append("库外检索结果：无（本次查询没有命中任何论文）。")
        lines.append(
            '请输出 {"verdict":"unchecked","reason":"..."}。'
            "原因里要说明「本次只检索了 arXiv 且未命中」，不要写原告没做过。"
        )
    else:
        lines.append("库外检索到的最新论文：")
        for c in candidates:
            lines.append(
                f"- {c['title']}（{c.get('year') or '年份未知'}，{c.get('source')}）"
                f"摘要：{(c.get('abstract') or '')[:300]}"
            )
        lines.append(
            "请判断这些论文是否已经做了上面的方向。输出 JSON："
            '{"verdict":"likely_done|not_found|unclear","similar":[{"title":"...","reason":"..."}],"reason":"..."}。'
            "没有高度相似的论文时 verdict 用 not_found，reason 里必须说明"
            "「只检索了 arXiv，不能据此认为没人做过」。"
        )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


#: 否定语境：这些词出现在「原创」类断言前面时，句子本身是在说「不能这么说」。
#:
#: 踩过的坑：prompt 里要求模型写「只检索了 arXiv，不能据此认为没人做过」，
#: 结果一刀切替换把这句话洗成了「不能据此认为尚待核实」——意思反了。
#: 否定句恰好是我们要鼓励的表述，必须放过。
_NEGATION_BEFORE = re.compile(r"(不能|无法|不足以|不可以|不应|不可|无法据此|不能据此)[^。；;\n]{0,12}$")


def _sanitize_claim(text: str) -> str:
    """把「原创」类断言洗掉。

    只在判断理由和相似论文理由上跑，不碰作者原文（`evidence` 是论文里抄出来的
    原话，改了就是篡改引文）。命中就整词替换成「尚待核实」，保留句子其余部分，
    免得答辩时看到一句读不通的话；但**否定语境放过**，因为那正是在说
    「不能据此认为没人做过」——这句话必须原样留着。
    """
    out = _as_text(text)
    for word in _ORIGINALITY_WORDS:
        if word not in out:
            continue
        pieces: list[str] = []
        start = 0
        while True:
            at = out.find(word, start)
            if at < 0:
                pieces.append(out[start:])
                break
            head = out[start:at]
            if _NEGATION_BEFORE.search(head):
                pieces.append(out[start : at + len(word)])  # 否定句，原样保留
            else:
                pieces.append(head + _NEUTRAL_REASON)
            start = at + len(word)
        out = "".join(pieces)
    return out


def parse_judge(text: str) -> dict[str, Any]:
    """解析 Judge 的 JSON。解析不出来就退化成 unclear，绝不升级成「原创」。"""
    raw = _as_text(text)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"verdict": "unclear", "similar": [], "reason": "模型没有返回可解析的判断"}
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {"verdict": "unclear", "similar": [], "reason": "模型返回的判断不是合法 JSON"}
    verdict = _as_text(data.get("verdict")) or "unclear"
    if verdict not in ("likely_done", "not_found", "unclear", "unchecked"):
        verdict = "unclear"
    similar = data.get("similar") if isinstance(data.get("similar"), list) else []
    return {
        "verdict": verdict,
        "similar": [
            {"title": _as_text(s.get("title")), "reason": _sanitize_claim(s.get("reason"))}
            for s in similar
            if isinstance(s, dict)
        ],
        "reason": _sanitize_claim(data.get("reason")),
    }


async def _stream_judge(messages: list[dict[str, str]]) -> AsyncIterator[str]:
    async for piece in llm_chat_stream(messages, temperature=0.1):
        yield piece


# --------------------------------------------------------------------------- 编排


def encode_event(event: dict[str, Any]) -> str:
    """一条 SSE 帧。"""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


async def direction_events(
    graph: dict[str, Any],
    extractions: list[dict[str, Any]],
    *,
    stream_judge: Any = None,
    search: Any = None,
    timeout: float = JUDGE_TIMEOUT_SECONDS,
    themes: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """跑完整条未来工作探索流水线。

    事件顺序：
        role(extractor) → role(mapper) → 每个方向 direction_start → role(searcher)
        → role(judge) → direction 结果 → done

    ``themes`` 是用户在界面上勾选的主题（catalog 的 category）；空表示全部主题。
    """
    judge_stream = stream_judge or _stream_judge
    searcher = search or search_latest
    wanted_themes = [_as_text(t).strip() for t in (themes or []) if _as_text(t).strip()]

    # ---- Extractor
    summary = extraction_summary(extractions)
    yield {
        "type": "role",
        "role": "extractor",
        "label": "抽取",
        "output": {
            "summary": summary,
            "statuses": {k: v for k, v in summary["counts"].items() if v},
        },
    }

    # ---- Mapper
    directions = build_directions(graph, extractions, themes=wanted_themes)
    yield {
        "type": "role",
        "role": "mapper",
        "label": "归纳",
        "output": {
            "directions": len(directions),
            "candidates": directions,
            "themes": wanted_themes,
        },
    }
    if not directions:
        if wanted_themes:
            yield {
                "type": "error",
                "message": f"勾选的 {len(wanted_themes)} 个主题（{'、'.join(wanted_themes)}）"
                           "里没有可用于归纳的研究缺口。放宽主题或多勾几篇论文再试。",
            }
        else:
            yield {"type": "error", "message": "图谱里没有可用于归纳的研究缺口，无法提出方向"}
        yield {"type": "done", "stats": {"directions": 0}}
        return

    used_calls = 0
    budget_hit = False
    results: list[dict[str, Any]] = []

    for i, direction in enumerate(directions):
        yield {
            "type": "direction_start",
            "index": i + 1,
            "total": len(directions),
            "id": direction["id"],
            "title": direction["anchor_title"],
        }

        # ---- Searcher（纯代码，失败也如实记为空）
        # arXiv 是外网，断网、503、限流都会发生；这一步失败不该炸掉整条流，
        # 只会让这个方向的候选为空——前端照常显示「没检索到」，理由里会标明来源。
        query = build_topic_query(direction)
        candidates: list[dict[str, Any]] = []
        if searcher:
            try:
                candidates = searcher(query) or []
                if asyncio.iscoroutine(candidates):
                    candidates = await candidates
            except Exception as exc:  # noqa: BLE001 — 外网失败一律降级为空候选
                logger.warning("方向 %s 的库外检索失败：%s", direction["id"], exc)
                candidates = []
        candidates = list(candidates or [])
        yield {
            "type": "role",
            "role": "searcher",
            "label": "检索",
            "output": {"query": query, "found": len(candidates)},
        }

        # ---- Judge
        judged: dict[str, Any] = {
            "verdict": "unchecked",
            "similar": [],
            "reason": "未做相似性判断",
        }
        if used_calls < MAX_JUDGE_CALLS:
            messages = build_judge_prompt(direction, candidates)
            buf: list[str] = []

            async def _consume(
                streamer=judge_stream, messages=messages, buf=buf
            ) -> None:
                async for piece in streamer(messages):
                    buf.append(piece)

            try:
                reserve_llm_call(0)
                used_calls += 1
                await asyncio.wait_for(_consume(), timeout=timeout)
                judged = parse_judge("".join(buf))
            except BudgetExceeded:
                budget_hit = True
                judged = {
                    "verdict": "unchecked",
                    "similar": [],
                    "reason": "今日演示额度已用完，这一方向没有做相似性判断",
                }
            except TimeoutError:
                judged = {
                    "verdict": "unchecked",
                    "similar": [],
                    "reason": f"判断超过 {int(timeout)} 秒，已放弃",
                }
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("方向 %s 判断失败", direction["id"], exc_info=True)
                judged = {
                    "verdict": "unclear",
                    "similar": [],
                    "reason": "判断过程中模型返回异常",
                }

        item = {
            "id": direction["id"],
            "kind": direction["kind"],
            "kind_label": direction["kind_label"],
            "theme": direction.get("theme") or "",
            "title": direction["anchor_title"],
            "anchor": direction["anchor"],
            "basis": direction["basis"],
            "evidence_status": direction["evidence_status"],
            "evidence_section": direction["evidence_section"],
            "evidence": direction["evidence"],
            "query": query,
            "candidates": candidates,
            "verdict": judged["verdict"],
            "similar": judged["similar"],
            "reason": judged["reason"],
        }
        results.append(item)
        yield {"type": "direction", "index": i + 1, "total": len(directions), "item": item}

        if budget_hit:
            break

    yield {
        "type": "done",
        "stats": {
            "extraction": summary,
            "directions": len(results),
            "judge_calls": used_calls,
            "budget_exhausted": budget_hit,
            # 缺口矩阵：拿已经跑出来的方向摊成 主题 × 技术手段，空格子是可能的缺口。
            # 放 done 里而不是单独发一帧：它不依赖库外检索结果，前端收尾时一次性渲染即可。
            "matrix": build_gap_matrix(graph, directions),
        },
    }