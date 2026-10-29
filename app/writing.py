"""写作助手（赛题 1.6）：论文框架生成 / 架构图生成 / 数据可视化 / 中译英。

## 这个模块的组织方式

四个子项各是一个函数，都接收"用户给的东西"，返回"可以直接塞给前端的 dict"。
不在这里写 HTTP，接口在 ``app/main.py``。

## 为什么 1.6.1 要这么绕（核心设计）

赛题原文：「给定一个 idea，让论文自动生成论文的框架，包括：Abstract、Introduction、
Related Work 和 References 部分。**确保 References 是真实没有幻觉**。」

最直觉的做法是 `给模型一个 idea → 让模型写完包括参考文献 → 完事`。
**这个做法一定会出现幻觉引用**，因为模型写参考文献时是在"续写文本"，
它会把格式模仿得很像，但 DOI / 作者 / 年份大概率是编的。

这里用的做法叫 **候选集约束生成**：

    1. 用 idea 去检索真实文献，得到候选池（每篇都有 key：C1、C2…）
    2. 把候选池**连 key 一起**塞进 prompt，明确告诉模型「只能引用池里的 key」
    3. 模型输出时只写 ``[C3]`` 这种标记，不写完整的参考文献条目
    4. 参考文献列表由**我们的代码**按 key 从候选池里取——模型碰不到
    5. 事后校验：抓出输出里所有 ``[Cn]``，凡是不在池里的标记一律标红

这样「出现一篇不存在的论文」在结构上就不可能，而不是靠模型自觉。

## 为什么摘要要从库里取而不是让模型编

Related Work 里说「BERT 通过掩码语言建模预训练」这种话，如果是从真实摘要里
摘出来的，评委会更信；如果模型自己写，容易把 A 论文的贡献安到 B 论文头上。
候选池里本库论文带摘要的，优先给模型看摘要。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app import literature as lit
from app.llm import chat
from app.logging_config import get_logger

logger = get_logger(__name__)

# 引用标记：模型应输出 [C3] 这种形式，也可能写成 [C3, C5] / [C3;C5]
# 只用一个正则，按组内逗号切开，避免两个正则互相打架漏标
_MARK_GROUP_RE = re.compile(r"\[\s*(C\d{1,3}(?:\s*[,;，、]\s*C\d{1,3})*)\s*\]")

# 参考文献里最多列几位作者，超出加 et al.。IEEE 用 6，这里跟 IEEE 对齐。
MAX_REF_AUTHORS = 6


# --------------------------------------------------------------------------- 候选池 → prompt


def render_pool(pool: list[lit.Candidate], with_abstract: bool = True) -> str:
    """把候选池渲染成给模型看的文本。

    刻意用固定格式的纯文本而不是 JSON：模型对「编号. 标题 (作者, 年份)」这种
    列表格式的遵循度明显更高，JSON 有时会漏读字段。
    """
    lines: list[str] = []
    for c in pool:
        year = c.year if c.year else "年份不详"
        authors = ", ".join(c.authors[:3]) if c.authors else ""
        if len(c.authors) > 3:
            authors += " et al."
        head = f"[{c.key}] {c.title} ({authors}, {year})"
        if c.venue:
            head += f" — {c.venue}"
        lines.append(head)
        if with_abstract and c.abstract:
            # 摘要只截前 240 字，够模型判断这篇讲什么，又不至于把 prompt 撑爆
            lines.append(f"    摘要：{c.abstract[:240]}")
    return "\n".join(lines)


FRAMEWORK_SYSTEM = """你是一位资深的学术论文写作助手，帮助研究者从零起草论文框架。

**最重要的规则（违反即作废）：**
你只能引用下面「候选文献」里给出的编号（如 C1、C2）。绝对不允许：
- 编造不存在于候选列表中的论文
- 编造作者、年份、期刊名、DOI
- 写完整的参考文献条目（参考文献由系统根据你给的编号自动生成，你只管写编号）

引用写法：在正文里要引用某篇时写 `[候选编号]`，例如 `[C3]` 或 `[C1, C4]`。
如果某句话找不到合适的候选文献支撑，就**不要写这句话的引用**，也不要把编号硬凑上去。

输出格式严格按下面四节，用 Markdown 标题：

## Abstract
（150-220 词，说明问题、方法、预期贡献。不要引用文献。）

## Introduction
（3-5 段。交代研究背景与问题动机，末尾点明本文的贡献。可以引用候选文献。）

## Related Work
（按研究脉络归类，每组 2-4 句。必须大量引用候选文献，且引用的编号必须来自候选列表。）

## References
（这一节**只写你用到的编号列表**，每行一个，格式如 `[C3]`。不要展开成完整条目。）
"""


def build_framework_prompt(idea: str, pool: list[lit.Candidate]) -> list[dict]:
    """组装 1.6.1 的对话消息。抽出来是为了测试时能单独检查 prompt 内容。"""
    pool_text = render_pool(pool) or "（没有检索到候选文献，请如实说明无法给出有依据的 Related Work）"
    user = (
        f"【研究 idea】\n{idea.strip()}\n\n"
        f"【候选文献】（共 {len(pool)} 篇，全部经过真实检索核实，你只能引用这些编号）\n"
        f"{pool_text}\n\n"
        "请按上面的格式要求，生成 Abstract / Introduction / Related Work / References 四节。"
    )
    return [
        {"role": "system", "content": FRAMEWORK_SYSTEM},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------- 校验


@dataclass
class CitationCheck:
    """引用校验结果。前端据此把悬空引用标红。"""

    cited_keys: list[str]
    unknown_keys: list[str]
    unused_candidates: list[str]

    @property
    def ok(self) -> bool:
        return not self.unknown_keys


def check_citations(text: str, pool: list[lit.Candidate]) -> CitationCheck:
    """抓出正文里所有引用标记，分类成「池内」和「池外」。

    池外的 key 意味着模型编了候选池里没有的编号——这是我们要暴露出来的问题，
    不能悄悄删掉当作没发生。
    """
    known = {c.key for c in pool}
    found: list[str] = []
    for group in _MARK_GROUP_RE.findall(text or ""):
        for k in re.split(r"[,;，、]", group):
            k = k.strip()
            if k and k not in found:
                found.append(k)

    cited = [k for k in found if k in known]
    unknown = [k for k in found if k not in known]
    unused = [c.key for c in pool if c.key not in cited]
    return CitationCheck(cited_keys=cited, unknown_keys=unknown, unused_candidates=unused)


def _format_authors(authors: list[str]) -> str:
    """参考文献里的作者串。超过 MAX_REF_AUTHORS 位截断加 et al.。"""
    names = [a.strip() for a in (authors or []) if a and a.strip()]
    if not names:
        return "作者信息缺失"
    if len(names) <= MAX_REF_AUTHORS:
        return ", ".join(names)
    return ", ".join(names[:MAX_REF_AUTHORS]) + ", et al."


def format_reference(c: lit.Candidate) -> str:
    """按候选的真实元数据生成一条参考文献。**只有这里能产生条目**。

    作者按学术惯例截断：超过 MAX_REF_AUTHORS 位就只留前几位加 et al.。
    T5 有 12 位作者、RWKV 有十几位，全列出来一条参考文献能占三行，
    既不像论文也不便读；截断是 IEEE/ACM 都接受的做法。
    """
    authors = _format_authors(c.authors)
    year = c.year if c.year else "n.d."
    parts = [f"{authors}", f"{c.title}", str(year)]
    if c.venue:
        parts.append(c.venue)
    if c.source == lit.SOURCE_ARXIV and c.arxiv_id:
        parts.append(f"arXiv:{c.arxiv_id}")
    elif c.doi:
        parts.append(f"https://doi.org/{c.doi}")
    # 片段自己可能已以句点结尾（"et al." / "n.d."），再拼 "." 会成 "et al.."，
    # 所以先把末尾句点去掉再统一按「. 」连接，最后补一个句点收尾。
    cleaned = [p.rstrip(". ") for p in parts if p]
    return ". ".join(cleaned) + "."


def build_reference_list(cited: list[str], pool: list[lit.Candidate]) -> list[dict]:
    """按模型引用的顺序生成参考文献条目，每条附来源标记。"""
    by_key = {c.key: c for c in pool}
    out: list[dict] = []
    for k in cited:
        c = by_key.get(k)
        if c is None:
            continue
        out.append(
            {
                "key": k,
                "text": format_reference(c),
                "source": c.source,
                "title": c.title,
                "year": c.year,
                "arxiv_id": c.arxiv_id,
                "doi": c.doi,
                "url": c.url,
                "local_file": c.local_file,
            }
        )
    return out


# --------------------------------------------------------------------------- 主流程


def _tokens_of(idea: str, limit: int = 3) -> list[str]:
    """从 idea 里粗切几个检索词。

    不做真正的关键词抽取（那又要一次 LLM 调用，慢且不稳）。
    做法：保留英文连续词组与中文 3 字以上的片段，取前几个。

    实测「attention mechanism transformer」这类组合检索效果够用；
    真要更准，后面可以让 LLM 出查询词，但那是可选优化。
    """
    idea = idea or ""
    q: list[str] = []
    # 英文词组（含连字符），取长度 >= 3 的
    for m in re.findall(r"[A-Za-z][A-Za-z\-]{2,}(?:\s+[A-Za-z][A-Za-z\-]{2,}){0,3}", idea):
        q.append(m.strip())
    # 中文片段：整句去掉标点后按 4-8 字切，取最长几段
    han = re.findall(r"[\u4e00-\u9fff]{4,12}", idea)
    q.extend(han)
    # 去重保序
    seen: list[str] = []
    for x in q:
        if x and x not in seen:
            seen.append(x)
    if not seen and idea.strip():
        seen = [idea.strip()[:40]]
    return seen[:limit]


def extract_idea_queries(idea: str) -> list[str]:
    """对外暴露的查询词抽取（测试用）。"""
    return _tokens_of(idea)


async def generate_framework(idea: str, total_candidates: int = 16) -> dict[str, Any]:
    """1.6.1 论文框架生成：检索 → 约束生成 → 校验 → 拼装参考文献。

    返回结构直接给前端：
        {
          "idea": ...,
          "queries": [...],          # 实际用来检索的词
          "candidates": [...],       # 候选池（每篇真实）
          "markdown": "...",         # 模型正文（可能含悬空引用，原样保留）
          "references": [...],       # 由代码生成，模型碰不到
          "check": {...},            # 校验结果
        }
    """
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("idea 不能为空")

    queries = _tokens_of(idea)
    logger.info("1.6.1 检索词: %s", queries)
    pool = lit.build_candidates(queries, total_limit=total_candidates)

    if not pool:
        # 一个候选都没有时不能硬生成——那样必然编造。如实返回。
        return {
            "idea": idea,
            "queries": queries,
            "candidates": [],
            "markdown": "",
            "references": [],
            "check": {"cited_keys": [], "unknown_keys": [], "unused_candidates": [], "ok": False},
            "warning": "没有检索到任何真实文献，为避免编造引用，未生成框架。请换个更具体的 idea。",
        }

    messages = build_framework_prompt(idea, pool)
    markdown = await chat(messages, temperature=0.4)

    check = check_citations(markdown, pool)
    references = build_reference_list(check.cited_keys, pool)

    return {
        "idea": idea,
        "queries": queries,
        "candidates": [c.to_dict() for c in pool],
        "markdown": markdown,
        "references": references,
        "check": {
            "cited_keys": check.cited_keys,
            "unknown_keys": check.unknown_keys,
            "unused_candidates": check.unused_candidates,
            "ok": check.ok,
        },
    }


# --------------------------------------------------------------------------- 导出给测试


def _dump_pool(pool: list[lit.Candidate]) -> str:
    """调试用：把候选池打成一行 JSON，方便人在日志里看。"""
    return json.dumps([c.to_dict() for c in pool], ensure_ascii=False)[:2000]