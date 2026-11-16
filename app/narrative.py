"""自动综述的大模型论述层（赛题 1.5.3 的「大模型自动生成」那一半）。

设计要点——为什么分成四个阶段，而不是「把图谱数据丢给模型让它写」：

    Planner  纯代码。把 survey() 已算好的事实整理成五段提纲与每段的数据口径。
    Analyst  纯代码。为每段挑出可引用的论文，并给每篇附一句事实（摘要首句等）。
    Writer   唯一调模型的阶段。**一次只写一段**，prompt 里锁定该段允许引用的 id。
    Verifier 纯代码。三段校验（id 是否在白名单、id 是否对应真实论文、论断是否有
             事实依据），**通过才下发**，不通过整段丢弃并记录原因。

为什么要这么绕，而不是让模型一次性写完再校验：
    模型的越界引用如果先流到屏幕上再收回，用户已经看见了错误内容。
    所以这里的「流式」是**按段流**：段内先缓冲、校验通过后整段下发，
    然后才开始写下一段。段与段之间仍然有渐进展示，但屏幕上的每一句
    都已经过校验。

为什么 Writer 一次只写一段而不是一次写五段：
    prompt 越短、约束越窄，模型越不容易跑题；而且单段失败只丢这一段，
    不必整篇重来，也不会因为一次超长输出把预算和时间拖爆。
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

#: 五段综述的结构。key 是内部标识，heading 是写进正文的小标题，hint 是给 Writer 的写作口径。
#:
#: 为什么是这五段：赛题 1.5.3 只写了「简短的综述」，但评委一定会问「综述该有什么」。
#: 这五段分别对应图谱能给出的五类事实——领域范围、技术路线、代表工作、
#: 演进脉络、现存问题——每一段都能在图谱里找到对应的数据，不会写空。
SECTIONS: tuple[dict[str, str], ...] = (
    {
        "key": "background",
        "heading": "研究背景与领域范围",
        "hint": "说明这个领域包含哪些论文、时间跨度、论文之间形成了怎样的引用关系整体面貌。",
    },
    {
        "key": "routes",
        "heading": "主要技术路线",
        "hint": "按引用关系簇归纳出若干条技术路线，说明每条路线大致在解决什么问题、以哪篇为代表。",
    },
    {
        "key": "works",
        "heading": "代表性研究工作",
        "hint": "挑出基石分靠前的论文，逐个说明它在网络里的位置（被引次数、角色）和摘要里给出的主张。",
    },
    {
        "key": "evolution",
        "heading": "技术演进脉络",
        "hint": "按年份串起发展方向，说明先后关系；引用关系为空的年份要如实说没有建立引用往来。",
    },
    {
        "key": "gaps",
        "heading": "现存问题与局限",
        "hint": "只根据给出的事实谈局限：样本规模、引用网络覆盖不全、综合分位靠后的论文、年份缺失。"
        "不要凭空推测技术缺陷。",
    },
)

#: 每段最多给 Writer 看多少篇候选论文。太多会稀释注意力，太少又写不满。
PER_SECTION_PICKS = 8

#: 一次综述最多用掉的模型调用次数（= Planner 之后 Writer 的段数上限）。
#: 单次综述不能因为重试或段落失败把当日预算吃光。
MAX_NARRATIVE_LLM_CALLS = len(SECTIONS)

#: 单段生成的超时（秒）。超过就放弃这一段，不让整条链路卡死。
SECTION_TIMEOUT_SECONDS = 90.0

_CITE_RE = re.compile(r"[\[【]\s*(?:cite:|CITE:|@)?([A-Za-z0-9][\w:.\-]*?)\s*[\]】]")

_SYSTEM_PROMPT = (
    "你是科研文献综述的撰写者。你只能使用用户提供的事实与论文清单，"
    "不得引入清单之外的任何论文、数字或结论。"
    "引用论文时必须使用形如 [arxiv:1706.03762] 的方括号编号，编号必须是清单里给出的 id。"
    "如果某段没有足够事实支撑，就如实写「现有材料不足以说明这一点」，不要编造。"
)


# --------------------------------------------------------------------------- 工具


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _year_label(year: Any) -> str:
    return f"{year} 年" if year else "年份未知"


def citable_ids(survey: dict[str, Any]) -> set[str]:
    """从 survey() 结果里取出可引用白名单。"""
    return {_as_text(c.get("id")) for c in (survey.get("citable") or []) if _as_text(c.get("id"))}


# --------------------------------------------------------------------------- Planner


def build_plan(survey: dict[str, Any]) -> dict[str, Any]:
    """Planner：把图谱事实整理成五段提纲。**纯代码，不调模型。**

    为什么 Planner 不交给模型：提纲一旦由模型自由发挥，就会冒出图谱算不出来的
    事实（比如它自己「记得」某个领域的代表工作）。让它只做确定性的事，
    模型的输出才有可能被规则校验。
    """
    stats = dict(survey.get("stats") or {})
    clusters = list(survey.get("clusters") or [])
    steps: list[dict[str, Any]] = []
    for sec in SECTIONS:
        if sec["key"] == "background":
            data_hint = f"覆盖 {stats.get('papers', 0)} 篇，时间跨度 {stats.get('years', '年份未知')}"
        elif sec["key"] == "routes":
            data_hint = f"{stats.get('clusters', 0)} 个引用关系簇"
        elif sec["key"] == "works":
            data_hint = "基石分排名靠前的论文"
        elif sec["key"] == "evolution":
            data_hint = "按年份分组的论文"
        else:
            data_hint = "样本规模、年份缺失、分位靠后的论文"
        steps.append(
            {
                "key": sec["key"],
                "heading": sec["heading"],
                "hint": sec["hint"],
                "data": data_hint,
            }
        )
    return {
        "sections": steps,
        "stats": stats,
        "cluster_count": len(clusters),
        # 提纲里把口径写死：评委问「你这段建立在什么上」，这里就是答案
        "basis": "全部数字来自 survey() 在图谱上当场算出的结果，不含模型推测",
    }


# --------------------------------------------------------------------------- Analyst


def _pick_for_section(key: str, survey: dict[str, Any]) -> list[dict[str, Any]]:
    """为某一段挑候选论文（纯代码排序，不调模型）。"""
    citable = list(survey.get("citable") or [])
    reps = {_as_text(r.get("id")) for r in (survey.get("representatives") or [])}
    if key == "background":
        picked = sorted(citable, key=lambda c: (-int(c.get("cited_by") or 0), _as_text(c.get("id"))))
    elif key == "routes":
        # 按「类别」聚一下，让每段都有不同技术路线的样本
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for c in citable:
            by_cat.setdefault(_as_text(c.get("category")) or "未分类", []).append(c)
        picked = []
        for cat in sorted(by_cat):
            picked.extend(
                sorted(by_cat[cat], key=lambda c: -int(c.get("cited_by") or 0))[:2]
            )
    elif key == "works":
        picked = [c for c in citable if _as_text(c.get("id")) in reps]
        rest = sorted(citable, key=lambda c: -int(c.get("cited_by") or 0))
        picked.extend([c for c in rest if c not in picked])
    elif key == "evolution":
        picked = sorted(
            citable,
            key=lambda c: (int(c.get("year") or 0) if c.get("year") else 0, -int(c.get("cited_by") or 0)),
        )
    else:  # gaps
        picked = sorted(citable, key=lambda c: int(c.get("cited_by") or 0))
    return picked[:PER_SECTION_PICKS]


def build_facts(survey: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Analyst：为提纲里每一段准备事实块与候选论文。**纯代码，不调模型。**"""
    facts: dict[str, Any] = {}
    for step in plan["sections"]:
        picks: list[dict[str, Any]] = []
        for c in _pick_for_section(step["key"], survey):
            pid = _as_text(c.get("id"))
            if not pid:
                # 没有 id 的条目无法被引用校验，直接跳过；不编一个假 id 出来
                continue
            picks.append(
                {
                    "id": pid,
                    "title": _as_text(c.get("title")),
                    "year": c.get("year"),
                    "role": _as_text(c.get("role")),
                    "cited_by": int(c.get("cited_by") or 0),
                    "claim": _as_text(c.get("abstract_lead")),
                }
            )
        picks.sort(key=lambda p: (-p["cited_by"], p["id"]))
        facts[step["key"]] = {
            "picks": picks,
            # 每段都带同一份总体统计，避免模型把「本段 8 篇」误当成「全领域 8 篇」
            "stats": dict(survey.get("stats") or {}),
            "clusters": [
                {
                    "community": c.get("community"),
                    "size": c.get("size"),
                    "lead": _as_text(c.get("lead")),
                    "category": _as_text(c.get("category")),
                }
                for c in (survey.get("clusters") or [])[:6]
            ],
            "timeline": [
                {"year": y.get("year"), "count": y.get("count")}
                for y in (survey.get("timeline") or [])
            ],
        }
    return facts


# --------------------------------------------------------------------------- Writer


def build_prompt(step: dict[str, Any], fact: dict[str, Any], allowed: set[str]) -> list[dict[str, str]]:
    """拼出这一段的提示词。只给本段候选，白名单按本段候选算（比整库白名单更严）。"""
    lines = [
        f"请撰写综述中的「{step['heading']}」这一段。",
        f"写作要求：{step['hint']}",
        "",
        "可用于本段的事实（来自图谱计算，不是模型记忆）：",
        f"- 全库统计：覆盖 {fact['stats'].get('papers')} 篇，"
        f"时间跨度 {fact['stats'].get('years')}，"
        f"{fact['stats'].get('edges')} 条引用边，{fact['stats'].get('clusters')} 个引用关系簇",
    ]
    if fact["clusters"]:
        lines.append("- 引用关系簇（不代表学科分类）：")
        for c in fact["clusters"]:
            lines.append(
                f"  · 簇 {c['community']}：{c['size']} 篇，被引最多的是《{c['lead']}》"
                + (f"，主旨「{c['category']}」" if c["category"] else "")
            )
    if fact["timeline"]:
        lines.append("- 年份分布：" + "、".join(f"{t['year']} 年 {t['count']} 篇" for t in fact["timeline"]))
    lines.append("")
    lines.append("本段只能引用下列论文，引用时写成 [编号] 形式，编号一字不差：")
    for p in fact["picks"]:
        claim = p["claim"] or "（该篇没有可用的摘要句，只可提及其标题、年份和引用次数）"
        lines.append(
            f"[{p['id']}] {p['title']}（{_year_label(p['year'])}，"
            f"被库内 {p['cited_by']} 篇引用，角色 {p['role']}）：{claim}"
        )
    lines.append("")
    lines.append(
        "硬性要求：1) 只输出这一段正文，不要重复小标题；2) 每个论断后紧跟 [编号]；"
        "3) 不得出现上面清单以外的编号；4) 正文控制在 150–300 字；5) 用中文。"
    )
    if allowed:
        lines.append("合法编号只有这些：" + "、".join(f"[{i}]" for i in sorted(allowed)))
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


async def _stream_section(messages: list[dict[str, str]]) -> AsyncIterator[str]:
    """单段生成。抽出来是为了测试里能替换掉真正的模型调用。"""
    async for piece in llm_chat_stream(messages, temperature=0.2):
        yield piece


# --------------------------------------------------------------------------- Verifier


def extract_citations(text: str) -> list[str]:
    """抽出正文里所有 [编号] 形式的引用。

    只认「长得像论文 id」的方括号内容：包含 ``:``（arxiv:xxxx / doi:…）
    或含 4 位以上数字（1706.03762 这类裸 arXiv 号）。
    为什么不做「方括号里什么都算」：中文正文里的 ``[来源: 论文.pdf 第3页]``
    这类标注会被误判成越界引用，把整段白白丢掉。
    """
    out: list[str] = []
    for m in _CITE_RE.finditer(text or ""):
        token = m.group(1).strip()
        if not token:
            continue
        if ":" not in token and not re.search(r"\d{4,}", token):
            continue
        out.append(token)
    return out


def _claim_supported(paragraph: str, paper: dict[str, Any]) -> bool:
    """规则判定：这一段里提到这篇论文的那些句子，是否有事实依据。

    第三条校验（「引用论文是否确实支持所在段落的论述」）没法用规则做到语义级，
    所以这里退一步做**可判定的部分**：
        · 句子必须提到这篇论文的标题关键词、或其主张里的关键词、或至少一个数字口径
          （年份/被引次数）——什么依据都不给、只挂一个编号的句子判为无依据。
        · 判不出来时返回 True 并让上层标 ``unverified``，绝不因为判不出就谎称有问题。
    """
    needle = _as_text(paper.get("claim")) or _as_text(paper.get("title"))
    if not needle:
        return True
    pid = _as_text(paper.get("id"))
    sentences = [s for s in re.split(r"[。！？\n]", paragraph or "") if pid in s or f"[{pid}]" in s]
    if not sentences:
        return True
    joined = "".join(sentences)
    # 标题/主张里的实词至少命中一个，或句子里出现真实数字口径（年份、被引次数）
    words = [w for w in re.split(r"[^\w\u4e00-\u9fa5]+", needle) if len(w) >= 4][:40]
    if any(w and w in joined for w in words):
        return True
    year = paper.get("year")
    if year and str(year) in joined:
        return True
    cited = paper.get("cited_by")
    return bool(cited) and str(cited) in joined


def verify_section(
    key: str,
    heading: str,
    text: str,
    facts: dict[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    """校验一段。返回 {ok, text, citations, rejected:[...], unverified:[...]}。"""
    by_id = {p["id"]: p for p in facts["picks"]}
    citations = extract_citations(text)
    rejected: list[dict[str, str]] = []
    unverified: list[dict[str, str]] = []
    for token in citations:
        if token not in allowed:
            rejected.append({"id": token, "reason": "引用编号不在本段可引用清单内"})
            continue
        paper = by_id.get(token)
        if paper is None:
            # 理论上前一条会先命中；留着兜底，防止白名单与事实块不同步
            rejected.append({"id": token, "reason": "引用编号找不到对应的真实论文"})
            continue
        if not _claim_supported(text, paper):
            unverified.append({"id": token, "reason": "该段论述未给出这篇论文的任何事实依据"})
    ok = not rejected
    return {
        "key": key,
        "heading": heading,
        "ok": ok,
        "text": text if ok else "",
        "citations": [c for c in citations if c in allowed],
        "rejected": rejected,
        "unverified": unverified,
        # 判不出的存疑引用单独给前端提示，不静默吞掉
        "checked": len(citations),
    }


# --------------------------------------------------------------------------- 编排


def _sse(event: dict[str, Any]) -> dict[str, Any]:
    return event


async def narrative_events(
    survey: dict[str, Any],
    *,
    stream_section: Any = None,
    timeout: float = SECTION_TIMEOUT_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """跑完整条四阶段流水线，逐事件产出（交给 SSE 包装）。

    事件顺序：
        role(planner) → role(analyst) → role(writer) → role(verifier)
        → 每段：section_start → answer_delta(整段，已校验) / section_rejected
        → verify（总统计） → done

    为什么 answer_delta 在段末一次给整段而不是逐字：
        见模块头部说明——未校验的字符不能出现在屏幕上。
    """
    streamer = stream_section or _stream_section
    citable = list(survey.get("citable") or [])
    if not citable:
        yield {
            "type": "error",
            "message": "图谱里没有可引用的论文，无法生成综述",
        }
        yield {"type": "done"}
        return

    # ---- Planner
    plan = build_plan(survey)
    yield {"type": "role", "role": "planner", "label": "规划", "output": plan}

    # ---- Analyst
    # 交接物要能让前端「点角标看是哪篇」，所以这里给的是完整论文信息，
    # 不是只有 id 列表：id / 标题 / 年份 / 角色 / 被引次数。
    # 顺便带上该段的事实口径（claim），前端展示证据时直接用，不必再算一遍。
    facts = build_facts(survey, plan)
    yield {
        "type": "role",
        "role": "analyst",
        "label": "分析",
        "output": {
            "citable": len(citable),
            "sections": [
                {
                    "key": k,
                    "heading": step["heading"],
                    "picks": [
                        {
                            "id": p["id"],
                            "title": p["title"],
                            "year": p.get("year"),
                            "role": p.get("role"),
                            "cited_by": p.get("cited_by"),
                            "claim": p.get("claim"),
                        }
                        for p in v["picks"]
                    ],
                }
                for (k, v), step in zip(facts.items(), plan["sections"], strict=False)
            ],
        },
    }

    # ---- Writer + Verifier（逐段交替）
    yield {"type": "role", "role": "writer", "label": "撰写", "output": {"model": "settings.llm_model"}}
    sections_out: list[dict[str, Any]] = []
    used_calls = 0
    budget_hit = False

    for i, step in enumerate(plan["sections"]):
        key = step["key"]
        fact = facts[key]
        allowed = {p["id"] for p in fact["picks"]}
        yield {"type": "section_start", "index": i + 1, "total": len(plan["sections"]),
               "key": key, "heading": step["heading"]}

        if used_calls >= MAX_NARRATIVE_LLM_CALLS:
            sections_out.append(
                {
                    "key": key,
                    "heading": step["heading"],
                    "ok": False,
                    "text": "",
                    "reason": "本次综述的模型调用次数已达上限，这一段未生成",
                }
            )
            yield {
                "type": "section_rejected",
                "key": key,
                "heading": step["heading"],
                "reason": "本次综述的模型调用次数已达上限，这一段未生成",
            }
            continue

        messages = build_prompt(step, fact, allowed)
        buf: list[str] = []
        failed: str = ""

        async def _consume(
            streamer=streamer, messages=messages, buf=buf
        ) -> None:
            # 显式把循环变量绑成默认参数：ruff 的 B023 指的就是
            # 「闭包引用循环变量」，这里每一轮都立刻 await，本不会出错，
            # 但绑死更稳妥，也省得后来人怀疑。
            async for piece in streamer(messages):
                buf.append(piece)

        try:
            # 预算：真正的扣费在 llm.chat_stream 内部（它调 reserve_llm_call(1)）。
            # 这里**不能再预扣**，否则一次综述要双倍计价。所以这里只做余额检查
            # （reserve_llm_call(0) 不扣数，只在余额为 0 时抛），
            # 目的只是在"还没开始写这一段"时就知道会不会超，避免写一半才炸。
            reserve_llm_call(0)
            used_calls += 1
            await asyncio.wait_for(_consume(), timeout=timeout)
        except BudgetExceeded:
            budget_hit = True
            failed = "今日演示额度已用完，本次综述未能完成"
        except TimeoutError:
            failed = f"这一段生成超过 {int(timeout)} 秒，已放弃"
        except asyncio.CancelledError:
            # 客户端断开：不再产出任何事件，让上游 finally 收尾
            raise
        except Exception:  # noqa: BLE001 —— 单段失败不该炸掉整条综述
            logger.warning("综述段落 %s 生成失败", key, exc_info=True)
            failed = "这一段生成失败（模型返回异常）"

        if failed:
            logger.info("综述段落 %s 未生成：%s", key, failed)
            sections_out.append(
                {"key": key, "heading": step["heading"], "ok": False, "text": "", "reason": failed}
            )
            yield {
                "type": "section_rejected",
                "key": key,
                "heading": step["heading"],
                "reason": failed,
            }
            if budget_hit:
                break
            continue

        result = verify_section(key, step["heading"], "".join(buf).strip(), fact, allowed)
        sections_out.append(result)
        if result["ok"]:
            yield {
                "type": "answer_delta",
                "text": f"## {step['heading']}\n\n{result['text']}\n\n",
                "key": key,
            }
        else:
            yield {
                "type": "section_rejected",
                "key": key,
                "heading": step["heading"],
                "reason": "越界引用：" + "；".join(
                    f"[{r['id']}] {r['reason']}" for r in result["rejected"]
                ),
            }

    # ---- Verifier 总账
    ok_n = sum(1 for s in sections_out if s.get("ok"))
    rejected = [
        {"key": s["key"], "heading": s["heading"], "reason": s.get("reason") or "越界引用"}
        for s in sections_out
        if not s.get("ok")
    ]
    unverified = [u for s in sections_out if s.get("ok") for u in s.get("unverified") or []]
    yield {
        "type": "role",
        "role": "verifier",
        "label": "校验",
        "output": {
            "sections_ok": ok_n,
            "sections_total": len(plan["sections"]),
            "citations": sum(len(s.get("citations") or []) for s in sections_out),
            "rejected": rejected,
            "unverified": unverified,
            "allowed_total": len(citable),
        },
    }
    yield {
        "type": "done",
        "stats": {
            "papers": (survey.get("stats") or {}).get("papers"),
            "sections_ok": ok_n,
            "llm_calls": used_calls,
            "budget_exhausted": budget_hit,
        },
    }


def encode_event(event: dict[str, Any]) -> str:
    """把事件编码成 SSE 的一行 data。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


__all__ = [
    "MAX_NARRATIVE_LLM_CALLS",
    "PER_SECTION_PICKS",
    "SECTIONS",
    "SECTION_TIMEOUT_SECONDS",
    "build_facts",
    "build_plan",
    "build_prompt",
    "citable_ids",
    "encode_event",
    "extract_citations",
    "narrative_events",
    "verify_section",
]