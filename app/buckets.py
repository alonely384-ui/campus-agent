"""粗粒度大类（review bucket）的判定与持久化。

为什么推倒重来
--------------
上一版把论文分成「领域 → 方向」两级，一共 8 个领域、20 个方向。粒度太细，
规则越写越长，判错的代价也越大：归错一个二级方向，一级领域跟着错，
界面上还要多两条筛选条横向滚动。

现在的口径
----------
只有**一层大类**，7 个初始类 + 「待分类」兜底：

   基座模型        论文本身就说「我们提出了 / 发布了 X 模型」
   模型技术        讲某个组件或结构怎么改（注意力、归一化、MoE、状态空间）
   训练与微调      讲训练或微调怎么更省、更有效
   对齐与推理      讲怎么让它听指令、怎么让它想得更好
   多模态          输入不只是文本（视觉、语音）
   能力与规模      研究规模与能力的关系、能力评测
   综合报告与其它  教材、综述、判不出具体方向的报告
   待分类          自动判不出、也没人手动指定

判定仍是关键词规则，但**只判一层**，所以每条规则只需要少量判据；
拿不准就进「待分类」，不硬分。

可维护性
--------
大类名单存在 `data/meta/review_buckets.json`，网页上能改名、能新建、能删
（内置 7 类只允许改名，不允许删——删了新论文就没地方可归）。文件丢了
就用内置默认值重建，不会因为一个 JSON 坏掉而整个功能不可用。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BUCKET_FILE = DATA_DIR / "meta" / "review_buckets.json"

# ---------------------------------------------------------------- 内置大类

BUCKET_MODELS = "基座模型"
BUCKET_TECH = "模型技术"
BUCKET_TRAIN = "训练与微调"
BUCKET_ALIGN = "对齐与推理"
BUCKET_MULTI = "多模态"
BUCKET_SCALE = "能力与规模"
BUCKET_OTHER = "综合报告与其它"
BUCKET_PENDING = "待分类"

#: 内置大类（顺序即前端展示顺序）。这些**不能删**，只能改名。
BUILTIN_BUCKETS: list[str] = [
    BUCKET_MODELS,
    BUCKET_TECH,
    BUCKET_TRAIN,
    BUCKET_ALIGN,
    BUCKET_MULTI,
    BUCKET_SCALE,
    BUCKET_OTHER,
]

#: 兜底桶。不在 `BUILTIN_BUCKETS` 里，永远存在、不能删不能改名。
PENDING_BUCKET = BUCKET_PENDING

# ---------------------------------------------------------------- 规则

# 每条规则：`bucket` + `title`/`abstract` 关键词（小写子串或词边界匹配）
# + `requires_any` 门槛词（至少命中一个，否则这条规则不参与打分）。
#
# 打分沿用上一版的比例：标题命中 2 分、摘要命中 1 分、标题摘要同时命中再 +0.5，
# 但**只有一层**，所以门槛直接用 2 分（标题命中）或 1.75 分（两个以上摘要词）。
_TITLE_SCORE = 2.0
_ABSTRACT_SCORE = 1.0
_BOTH_BONUS = 0.5
_MIN_TITLE_SCORE = 2.0
_MIN_ABSTRACT_ONLY_SCORE = 1.75

#
# 顺序有讲究：越靠前的规则越"专"，越靠后越"泛"。判分取最高分，同分时按这个顺序
# 拿第一个（`scored.sort(key=(-score, bucket))` 之后取 `scored[0]`，规则顺序即优先级）。
# 实测教训：`Instruction Pre-Training` 标题里既有 `language models are`（基座模型）
# 又有 `pre-training`（训练与微调），若基座模型排在前面就被抢走——它讲的是训练方法，
# 不是提出模型，所以训练与微调必须排在基座模型之前。
_RULES: list[dict[str, Any]] = [
    {
        "bucket": BUCKET_MULTI,
        "requires_any": ["vision", "visual", "image", "speech", "audio", "multimodal", "multi-modal"],
        "title": [
            "image is worth",
            "vision transformer",
            "linear-probe",
            "natural language supervision",
            "speech recognition",
            "whisper",
            "clip",
        ],
        "abstract": ["image", "visual", "speech", "audio"],
    },
    {
        "bucket": BUCKET_SCALE,
        # 门槛要宽：标题里出现 code evaluation / benchmark / 能力评测的说法都算
        "requires_any": [
            "scaling law",
            "scaling laws",
            "emergent",
            "emergence",
            "evaluating",
            "evaluation",
            "benchmark",
            "trained on code",
        ],
        "title": [
            "scaling laws",
            "emergent abilities",
            "evaluating large language models",
            "trained on code",
        ],
        "abstract": ["scaling law", "emergent abilit", "power-law", "evaluation set"],
    },
    {
        "bucket": BUCKET_OTHER,
        # 教材/综述：标题就是书名，摘要里往往是版权声明
        "requires_any": ["textbook", "understanding deep learning", "survey", "review of"],
        "title": ["understanding deep learning"],
        "abstract": ["this work", "textbook"],
    },
    {
        "bucket": BUCKET_TRAIN,
        # 门槛词用不带尾部截断的完整说法，否则 `memory optim` 匹配不上 `optimizations`
        "requires_any": [
            "low-rank",
            "low rank",
            "quantiz",
            "finetun",
            "pre-training",
            "memory optimization",
            "parameter-efficient",
            "optimizer",
        ],
        "title": [
            "lora",
            "qlora",
            "quantiz",
            "instruction pre-training",
            "memory optimizations",
            "redunancy optimizer",
            "zero:",
        ],
        "abstract": [
            "lora",
            "quantiz",
            "low-rank adaptation",
            "memory footprint",
            "optimize memory",
        ],
    },
    {
        "bucket": BUCKET_ALIGN,
        "requires_any": [
            "human feedback",
            "preference",
            "reinforcement learning",
            "instruction",
            "chain of thought",
            "chain-of-thought",
            "reasoning",
            "policy optimization",
        ],
        "title": [
            "follow instructions",
            "human feedback",
            "preference optimization",
            "chain-of-thought",
            "proximal policy",
            "instructgpt",
            "visual instruction tuning",
            "incentivizing reasoning",
        ],
        "abstract": ["human feedback", "preference optimization", "chain of thought", "chain-of-thought"],
    },
    {
        "bucket": BUCKET_TECH,
        "requires_any": [
            "attention",
            "normalization",
            "normalisation",
            "mixture of experts",
            "state space",
            "architecture",
            "transformer",
        ],
        "title": [
            "attention is all you need",
            "layer normalization",
            "flash attention",
            "flashattention",
            "transformer architecture",
            "switch transformers",
            "mixture of experts",
            "rwkv",
            "reinventing rnns",
            "mamba",
            "selective state spaces",
            "active memory",
        ],
        "abstract": ["self-attention", "layer normalization", "mixture of experts", "state space model"],
    },
    {
        "bucket": BUCKET_MODELS,
        "requires_any": [
            "language model",
            "language models",
            "foundation model",
            "foundation models",
            "pretrained",
            "pre-trained",
            "parameters",
            "billion",
        ],
        "title": [
            "few-shot learners",
            "technical report",
            "language models are",
            "open foundation",
            "open and efficient foundation",
            "open-access multilingual",
            "open bilingual",
            "mistral",
            "qwen",
            "deepseek",
            "llama",
            "glm",
            "bloom",
            "bert",
            "generative pre-training",
            "unified text-to-text",
        ],
        "abstract": ["foundation language models", "pre-trained language model", "billion parameters"],
    },
]


def _hit(text: str, phrase: str) -> bool:
    """短语是否在文本里出现。英文按词边界、允许复数；中文按子串。"""
    if not text or not phrase:
        return False
    if re.search(r"[\u4e00-\u9fff]", phrase):
        return phrase in text
    escaped = re.escape(phrase)
    pattern = rf"(?<![a-z]){escaped}(?:s|es)?(?![a-z])"
    if re.search(pattern, text):
        return True
    if phrase.isalpha() and len(phrase) <= 5 and phrase.lower() == phrase:
        return re.search(rf"\b{re.escape(phrase.upper())}(?:S|ES)?\b", text.upper()) is not None
    return False


@dataclass
class Classification:
    """一篇论文的大类归属。"""

    bucket: str
    status: str
    """`classified` / `pending` / `manual`。"""
    confidence: str
    """`high` / `medium` / `low`。是等级，不是概率。"""
    score: float
    reason: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "bucket_status": self.status,
            "bucket_confidence": self.confidence,
            "bucket_score": round(self.score, 3),
            "bucket_reason": self.reason,
            "bucket_evidence": list(self.evidence),
        }


def _score_rule(rule: dict[str, Any], title: str, abstract: str) -> tuple[float, list[str], set[str]]:
    gate = [p for p in rule.get("requires_any") or () if _hit(f"{title} {abstract}", p)]
    if not gate:
        return 0.0, [], set()

    title_hits = [p for p in rule.get("title") or () if _hit(title, p)]
    abstract_hits = [p for p in rule.get("abstract") or () if _hit(abstract, p)]

    score = 0.0
    evidence: list[str] = []
    kinds: set[str] = set()
    if title_hits:
        score += _TITLE_SCORE
        evidence.extend(f"标题含「{p}」" for p in title_hits[:3])
        kinds.add("title")
    if abstract_hits:
        score += _ABSTRACT_SCORE
        evidence.extend(f"摘要含「{p}」" for p in abstract_hits[:2])
        kinds.add("abstract")
    if {"title", "abstract"} <= kinds:
        score += _BOTH_BONUS
        evidence.append("标题与摘要同时命中")
    return score, evidence, kinds


def classify(
    filename: str = "",
    title: str = "",
    abstract: str = "",
    manual_bucket: str = "",
) -> Classification:
    """判定一篇论文的大类。

    `manual_bucket`：人工指定的大类（来自 `data/meta/review_buckets.json` 的
    `assignments`），命中则直接采信并标 `manual`。
    任何证据都不够时返回 `pending`，**不猜**。
    """
    if manual_bucket:
        return Classification(
            bucket=manual_bucket,
            status="manual",
            confidence="high",
            score=_MIN_TITLE_SCORE,
            reason="人工指定",
            evidence=["人工标注"],
        )

    t = (title or "").lower()
    a = (abstract or "").lower()

    scored: list[tuple[float, str, list[str], set[str]]] = []
    for rule in _RULES:
        s, ev, kinds = _score_rule(rule, t, a)
        if "title" in kinds and s >= _MIN_TITLE_SCORE:
            scored.append((s, rule["bucket"], ev, kinds))
        elif "title" not in kinds and s >= _MIN_ABSTRACT_ONLY_SCORE:
            scored.append((s, rule["bucket"], ev, kinds))

    if not scored:
        has_any = bool(t.strip() or a.strip())
        return Classification(
            bucket=PENDING_BUCKET,
            status="pending",
            confidence="low",
            score=0.0,
            reason="标题与摘要都没有足够具体的大类证据" + ("（标题或摘要缺失）" if not has_any else ""),
        )

    # 同分时按 `_RULES` 的先后取，**不按大类名字的字典序**——那是随手定出来的，
    # 和"哪条规则更专"毫无关系。实测踩过：`Instruction Pre-Training` 在
    # 「训练与微调」和「基座模型」上都是 2.0 分，按字典序「基」<「训」就归错了。
    order = {rule["bucket"]: i for i, rule in enumerate(_RULES)}
    scored.sort(key=lambda x: (-x[0], order.get(x[1], len(order))))
    top_score, top_bucket, top_ev, top_kinds = scored[0]
    if "title" in top_kinds:
        level = "high" if top_score >= _TITLE_SCORE + _BOTH_BONUS else "medium"
    else:
        level = "medium"

    return Classification(
        bucket=top_bucket,
        status="classified",
        confidence=level,
        score=top_score,
        reason="；".join(top_ev),
        evidence=top_ev,
    )


# ---------------------------------------------------------------- 持久化

_LOCK = threading.Lock()


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "buckets": list(BUILTIN_BUCKETS),
        "assignments": {},
    }


def load_state() -> dict[str, Any]:
    """读大类名单与人工指定表。文件不存在或坏了都退回默认值。"""
    with _LOCK:
        try:
            raw = BUCKET_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return _default_state()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("大类配置读取失败，使用默认值：%s", exc)
            return _default_state()

    state = _default_state()
    buckets = data.get("buckets")
    if isinstance(buckets, list):
        seen: list[str] = []
        for name in buckets:
            if isinstance(name, str) and name.strip() and name not in seen:
                seen.append(name)
        # 内置类必须都在（改过名也要在，但改名＝旧名消失、新名出现，这里只保证非空）
        if seen:
            state["buckets"] = seen
    assign = data.get("assignments")
    if isinstance(assign, dict):
        state["assignments"] = {
            str(k): str(v) for k, v in assign.items() if isinstance(k, str) and isinstance(v, str)
        }
    return state


def save_state(state: dict[str, Any]) -> None:
    """原子写回。先写临时文件再 replace，避免写一半断电留下半截 JSON。"""
    BUCKET_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=False)
    tmp = BUCKET_FILE.with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(BUCKET_FILE)


def list_buckets() -> list[str]:
    """当前全部大类名（含待分类，永远在最后）。"""
    state = load_state()
    names = [b for b in state["buckets"] if b != PENDING_BUCKET]
    return [*names, PENDING_BUCKET]


def builtin_names() -> list[str]:
    """内置大类名单。内置类只允许改名、不允许删除。"""
    return list(BUILTIN_BUCKETS)


def add_bucket(name: str) -> list[str]:
    """新建一个大类。重名或空名抛 ValueError。"""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("大类名不能为空")
    if len(clean) > 20:
        raise ValueError("大类名最长 20 个字符")
    state = load_state()
    existing = [b for b in state["buckets"] if b != PENDING_BUCKET]
    if clean == PENDING_BUCKET or clean in existing:
        raise ValueError(f"大类「{clean}」已存在")
    existing.append(clean)
    state["buckets"] = existing
    save_state(state)
    return [*existing, PENDING_BUCKET]


def rename_bucket(old: str, new: str) -> list[str]:
    """改大类名。内置类可以改名；「待分类」不允许改名。"""
    src = (old or "").strip()
    dst = (new or "").strip()
    if not dst:
        raise ValueError("大类名不能为空")
    if len(dst) > 20:
        raise ValueError("大类名最长 20 个字符")
    state = load_state()
    existing = [b for b in state["buckets"] if b != PENDING_BUCKET]
    if src == PENDING_BUCKET:
        raise ValueError("「待分类」不允许改名")
    if src not in existing:
        raise ValueError(f"大类「{src}」不存在")
    if dst == PENDING_BUCKET or (dst in existing and dst != src):
        raise ValueError(f"大类「{dst}」已存在")
    state["buckets"] = [dst if b == src else b for b in existing]
    # 人工指定跟着改名走，否则那些论文会掉进待分类
    state["assignments"] = {k: (dst if v == src else v) for k, v in state["assignments"].items()}
    save_state(state)
    return [*state["buckets"], PENDING_BUCKET]


def delete_bucket(name: str) -> list[str]:
    """删一个大类。内置 7 类不允许删；被删类里的论文自动落到「待分类」。"""
    target = (name or "").strip()
    if target == PENDING_BUCKET:
        raise ValueError("「待分类」不允许删除")
    if target in BUILTIN_BUCKETS:
        raise ValueError(f"「{target}」是内置大类，只允许改名，不允许删除")
    state = load_state()
    existing = [b for b in state["buckets"] if b != PENDING_BUCKET]
    if target not in existing:
        raise ValueError(f"大类「{target}」不存在")
    state["buckets"] = [b for b in existing if b != target]
    # 那些论文不能凭空空掉：明确记成待分类，方便人工再指一次
    state["assignments"] = {
        k: (PENDING_BUCKET if v == target else v) for k, v in state["assignments"].items()
    }
    save_state(state)
    return [*state["buckets"], PENDING_BUCKET]


def set_assignment(filename: str, bucket: str) -> list[str]:
    """把某篇论文人工指定到某个大类。传「待分类」等于撤销人工指定。"""
    fn = (filename or "").strip()
    if not fn:
        raise ValueError("缺少文件名")
    clean = (bucket or "").strip()
    state = load_state()
    names = [b for b in state["buckets"] if b != PENDING_BUCKET]
    if clean not in names and clean != PENDING_BUCKET:
        raise ValueError(f"大类「{clean}」不存在")
    if clean == PENDING_BUCKET:
        state["assignments"].pop(fn, None)
    else:
        state["assignments"][fn] = clean
    save_state(state)
    return [*names, PENDING_BUCKET]


def assign_all(items: list[dict]) -> list[dict]:
    """给每个条目补大类字段。

    写回的键：
    - `bucket` / `bucket_status` / `bucket_confidence` / `bucket_reason` / `bucket_score`
    - `category` / `topic`：**旧字段，值等于 bucket**，问答与写作侧还在读
    """
    state = load_state()
    assign = state["assignments"]
    for item in items:
        filename = str(item.get("file") or item.get("filename") or "")
        res = classify(
            filename=filename,
            title=str(item.get("title") or ""),
            abstract=str(item.get("abstract") or ""),
            manual_bucket=assign.get(filename, ""),
        )
        item.update(res.to_dict())
        item["topic"] = res.bucket
        item["category"] = res.bucket
    return items