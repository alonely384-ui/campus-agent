"""论文翻译（赛题 1.3.3）：按页抽正文 → 调 LLM 译成中文 → 缓存到 data/meta/translations。

三个子需求与这里的对应关系：
    1.3.3.1 部分翻译 —— `translate_text()` 直接译一段（摘要 / 用户选中的段落），不落缓存；
    1.3.3.2 整篇翻译 —— 按 PDF 页抽文本，`translate_pages()` 逐页翻译并流式返回，
                            公式/代码/引用编号靠提示词要求"原样保留"；
    1.3.3.3 中英对比 —— 前端拿"页"当锚点做双向滚动同步，所以翻译结果必须按页存放。

为什么翻译单位是"页"而不是"段落"：
    页是 PDF 和阅读页天然共享的坐标，前端不用维护复杂的段落映射就能做对比滚动；
    一页一段 Markdown 也方便缓存（同一页译过就不花钱再译）。

缓存为什么放 data/meta/translations/：
    data/papers/ 被 .gitignore 排除，翻译结果跟着丢；放 meta 下可以提交/备份。
    文件名用 `<pdf 文件名>.json`，内容是 {页码: {hash, markdown}}，hash 用来判断
    原文抽出来的文字有没有变（变了才重译）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import fitz  # pymupdf

from app.config import settings
from app.llm import chat
from app.logging_config import get_logger
from app.papers import META_DIR

logger = get_logger(__name__)

TRANSLATIONS_DIR = META_DIR / "translations"

# 一页正文超过这个长度就切块翻译：长请求更容易超时/超上下文，切块还能让"逐块出活"更早
CHUNK_CHARS = 6000
# 选中翻译的长度上限：防止有人把整篇糊进输入框，一次请求打穿额度
SELECTION_MAX_CHARS = 6000

SYSTEM_PROMPT = (
    "你是学术论文翻译专家，把英文论文片段译成简体中文。硬性要求：\n"
    "1. 忠实、准确，术语用学界通行译法（如 attention→注意力、fine-tuning→微调）。\n"
    "2. 数学公式、LaTeX（$...$、\\begin{equation}...）、代码块、变量名、函数名、URL、"
    "参考文献编号（如 [12]、[38, 24]）、图表编号（Table 2、Figure 3）一律原样保留，不要翻译。\n"
    "3. 输出 Markdown：段落之间空一行；保留原文的小标题层级（## / ###）；列表保持列表。\n"
    "4. 只输出译文本身，不要写「以下是翻译」之类的开场白，也不要复述原文。\n"
    "5. 遇到参考文献列表时，保留每条编号和文献信息（作者/年份/标题可保留英文），不要自行删减。"
)


def translation_file(filename: str) -> Path:
    """翻译缓存的落盘位置（文件名里的 .pdf 换成 .json）。"""
    stem = Path(filename).stem
    return TRANSLATIONS_DIR / f"{stem}.json"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _clean_block(text: str) -> str:
    """把 PDF 文本块压成一段：去连字符换行、合并空白、去掉首尾。"""
    text = text.replace("\u00ad", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)   # 断词连字符：inter-\nest → interest
    text = re.sub(r"\s*\n\s*", " ", text)
    # PDF 里项目符号常被抽成 \x00-\x08 这类控制字符，留着会把模型带跑偏
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def page_texts(pdf_path: Path, max_pages: int | None = None) -> dict[int, str]:
    """按页抽正文（1 基页码）→ {页码: 文本}。

    双栏论文按"先左栏后右栏"排序，否则两栏的行会交错，翻出来根本读不懂。
    """
    out: dict[int, str] = {}
    with fitz.open(pdf_path) as doc:
        last = len(doc) if max_pages is None else min(len(doc), max_pages)
        for pno in range(last):
            page = doc[pno]
            width, height = page.rect.width, page.rect.height
            blocks = [b for b in page.get_text("blocks") if b[6] == 0]
            if not blocks:
                continue
            mid = width / 2
            left = [b for b in blocks if b[0] < mid - 8]
            right = [b for b in blocks if b[0] >= mid - 8]
            # 两栏都够"实"(至少 2 块)才按栏排序，否则当单栏处理
            if len(left) >= 2 and len(right) >= 2:
                ordered = sorted(left, key=lambda b: b[1]) + sorted(right, key=lambda b: b[1])
            else:
                ordered = sorted(blocks, key=lambda b: b[1])

            parts: list[str] = []
            for b in ordered:
                text = _clean_block(b[4] or "")
                if not text:
                    continue
                # 页眉页脚：贴边且很短的行（页码、running head）直接丢
                short_at_edge = (b[1] < height * 0.06 or b[3] > height * 0.94) and len(text) < 80
                if short_at_edge:
                    continue
                parts.append(text)
            if parts:
                out[pno + 1] = "\n\n".join(parts)
    return out


def load_translation(filename: str) -> dict:
    """读缓存；没有就返回空骨架（前端据此判断"还没翻"）。"""
    path = translation_file(filename)
    empty = {"source": filename, "model": settings.llm_model, "pages": {}}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("翻译缓存损坏，忽略: %s", path)
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
        return empty
    data.setdefault("source", filename)
    data.setdefault("model", settings.llm_model)
    return data


def save_translation(filename: str, data: dict) -> Path:
    TRANSLATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = translation_file(filename)
    data["source"] = filename
    data["model"] = settings.llm_model
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _chunks(text: str) -> list[str]:
    """按段落把长页切成若干块，每块 ≤ CHUNK_CHARS，尽量在段落边界切。"""
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > CHUNK_CHARS:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


async def translate_text(text: str) -> str:
    """译一段文本（选中翻译 / 摘要翻译走这里）。空文本直接返回空串。"""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > SELECTION_MAX_CHARS:
        text = text[:SELECTION_MAX_CHARS]
    pieces: list[str] = []
    for chunk in _chunks(text):
        pieces.append(await chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": chunk},
            ],
            temperature=0.2,
        ))
    return "\n\n".join(p.strip() for p in pieces if p.strip())


async def translate_pages(
    filename: str,
    pdf_path: Path,
    pages: list[int] | None = None,
    force: bool = False,
) -> AsyncIterator[dict]:
    """逐页翻译并 yield 事件（供 SSE 直接转发）。

    事件形状：
        {"type": "page", "page": 3, "markdown": "...", "cached": true, "done": 1, "total": 5}
        {"type": "done", "total": 5, "translated": [3, 4, 5, 6, 7]}
        {"type": "error", "page": 3, "message": "..."}   （单页失败不中断整篇）
    """
    texts = page_texts(pdf_path)
    if not texts:
        yield {"type": "error", "message": "这篇 PDF 里抽不到文字（可能是扫描件），无法翻译"}
        return

    # pages=None 才表示"整篇"；显式传空列表就是"一页都不用翻"（调用方算错了就该翻 0 页，
    # 而不是把整篇都烧掉）
    if pages is None:
        wanted = sorted(texts)
    else:
        wanted = sorted({p for p in pages if p in texts})
    if not wanted:
        yield {"type": "done", "total": 0, "translated": []}
        return
    cache = load_translation(filename)
    stored: dict = cache.setdefault("pages", {})
    done = 0
    translated: list[int] = []

    for page_no in wanted:
        source = texts[page_no]
        digest = _digest(source)
        hit = stored.get(str(page_no)) or {}
        if not force and hit.get("hash") == digest and hit.get("markdown"):
            done += 1
            translated.append(page_no)
            yield {
                "type": "page",
                "page": page_no,
                "markdown": hit["markdown"],
                "cached": True,
                "done": done,
                "total": len(wanted),
            }
            continue
        try:
            markdown = await translate_text(source)
        except Exception as exc:  # 单页失败不影响后面的页
            logger.exception("第 %d 页翻译失败", page_no)
            yield {"type": "error", "page": page_no, "message": str(exc)[:200]}
            continue
        stored[str(page_no)] = {"hash": digest, "markdown": markdown, "chars": len(source)}
        save_translation(filename, cache)
        done += 1
        translated.append(page_no)
        yield {
            "type": "page",
            "page": page_no,
            "markdown": markdown,
            "cached": False,
            "done": done,
            "total": len(wanted),
        }

    yield {"type": "done", "total": len(wanted), "translated": translated}


def references_page_of(filename: str) -> int | None:
    """从 structures.json 取 References 起始页（1 基）；没有就 None。

    整篇翻译默认跳过参考文献页之后的内容：翻参考文献对"读论文"帮助很小，
    却要按条目烧掉大把 token。
    """
    try:
        from app.papers import load_structures

        meta = load_structures().get(filename) or {}
        refs = meta.get("references") or {}
        page = refs.get("page")
        return int(page) + 1 if page is not None else None
    except Exception:  # 结构文件缺失不该让翻译接口整体挂掉
        return None
