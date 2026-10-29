"""1.6.4 学术中译英：把中文论文片段译成可投稿的英文学术表达。

## 和现有 ``app/translate.py`` 的区别

``translate.py`` 做的是**英译中**（阅读论文时用），提示词是
"把英文论文片段译成简体中文"。这里是**中译英**，方向相反，
而且目标不是"让人看懂"，是"能直接塞进 IEEE/ACM 模板"。

两者共享的只有分块、缓存这些工程骨架；**提示词必须完全另写**。
直接复用会得到"机翻腔"，评审一眼就能看出来。

## 为什么不用普通翻译模型的一句话翻

学术写作有大量隐性规范，普通翻译会丢：
- 术语要统一（"注意力机制"全文都得是 attention mechanism，不能一处 attention、一处 attention module）
- 时态要讲规矩（讲已有工作用现在完成时/一般现在时，讲自己的实验结果用过去时）
- 不能出现"I think"、"very"、"obviously"这类口语或主观词
- 中文的长流水句要拆成英文的短句

所以提示词里显式写了这些约束，并且要求模型**只输出译文**，
不要加"以下是译文"之类的客套话——那种话贴进论文还得手动删。

## 术语表为什么是可选注入

如果论文里"Transformer"前面已经定过译法，后面就要保持一致。
这里允许调用方传一份术语表（中文 → 英文），拼进提示词。
不传也能用，只是长文档里可能有轻微不一致。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.llm import chat
from app.logging_config import get_logger

logger = get_logger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "meta" / "zh2en"
CHUNK_CHARS = 1800


SYSTEM = """You are an expert academic translator specialising in computer science papers.
Translate the Chinese text into English suitable for submission to IEEE/ACM venues.

Hard rules:
1. Output ONLY the translation. No preamble, no "Here is the translation", no explanations.
2. Use formal academic register. Never use contractions ("don't"), subjective hedges
   ("I think", "we believe it is obvious"), or casual intensifiers ("very", "really").
3. Keep terminology consistent with the glossary if one is provided, and with the
   earlier segments of the same document.
4. Chinese long run-on sentences should be split into shorter English sentences.
5. Preserve every number, unit, citation marker like [12], LaTeX fragment, and
   proper noun exactly as written.
6. Tense: established work and general facts in present or present perfect;
   your own experimental procedures and observations in past tense.
7. Do not add content that is not in the source, and do not omit any sentence.
"""


def _digest(text: str, glossary: str) -> str:
    return hashlib.sha1(f"{text}\x00{glossary}".encode()).hexdigest()[:20]


def _chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """按段落切块，避免把句子从中间劈开。

    切法是贪心装填：一段一段往里塞，超了 size 就另起一块。
    单段本身就超 size 的（很少见），才真的按字符硬切。
    """
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > size:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(p), size):
                out.append(p[i : i + size])
            continue
        if len(buf) + len(p) + 2 > size and buf:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out or [text]


def parse_glossary(raw: str) -> dict[str, str]:
    """解析术语表，支持 ``中文=English`` 或 ``中文,English`` 两种写法，一行一条。"""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ",", "，", "\t"):
            if sep in line:
                k, _, v = line.partition(sep)
                k, v = k.strip(), v.strip()
                if k and v:
                    out[k] = v
                break
    return out


def _glossary_text(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    lines = "\n".join(f"- {k} → {v}" for k, v in glossary.items())
    return f"\n\nGlossary (must be followed exactly):\n{lines}"


async def translate_zh2en(text: str, glossary: dict[str, str] | None = None) -> dict:
    """中译英主流程：分块 → 逐块翻 → 拼接。同样的输入直接命中缓存。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("待翻译文本不能为空")

    glossary = glossary or {}
    gl_text = _glossary_text(glossary)
    digest = _digest(text, gl_text)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{digest}.json"

    if cache.exists():
        import json

        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            data["cached"] = True
            return data
        except (OSError, ValueError):
            pass  # 缓存坏了就当没有，重新翻

    pieces = _chunks(text)
    logger.info("中译英：%s 字，切成 %s 块", len(text), len(pieces))

    translated: list[str] = []
    for piece in pieces:
        prev = translated[-1][-400:] if translated else ""
        # 把上一块的结尾给它看，保证跨块的术语和指代连贯
        context = f"\n\nPrevious segment (for continuity only, do not translate it again):\n{prev}" if prev else ""
        messages = [
            {"role": "system", "content": SYSTEM + gl_text},
            {"role": "user", "content": f"Translate the following segment:\n\n{piece}{context}"},
        ]
        out = await chat(messages, temperature=0.2)
        translated.append(out.strip())

    result = {
        "source": text,
        "translation": "\n\n".join(translated),
        "chunks": len(pieces),
        "chars_in": len(text),
        "chars_out": len("\n\n".join(translated)),
        "cached": False,
    }
    import json

    try:
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("写入翻译缓存失败: %s", exc)

    return result