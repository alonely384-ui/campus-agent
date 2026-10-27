"""图表裁切：把 PDF 里某张图按题注定位、裁成 PNG。

为什么按需裁、而不是解析时全裁一遍：
    39 篇全量解析一次要几分钟（表格走 pdfplumber 很慢），而用户一次只看一张图。
    这里用 structures.json 里已有的题注（页码 + 题注文字）当线索，点开哪个裁哪个，
    裁完缓存在 data/meta/figures/<stem>/<index>.png，第二次直接读文件。

定位策略（按可靠度排序）：
    1. 在该页所有图片块（`page.get_text("dict")` 里 type==1 的块）里挑最大的那块；
    2. 有多块时，挑离题注最近的那块（题注通常在图的下面，所以看 |块底 - 题注顶|）；
    3. 一页没有图片块就返回 None —— 宁可不给图，也不给一张裁错位置的截图。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.logging_config import get_logger
from app.papers import META_DIR

logger = get_logger(__name__)

FIGURES_DIR = META_DIR / "figures"
DPI = 150
PAD = 4.0  # 裁的时候向外多留一点，避免把图边缘切掉


def _image_blocks(page) -> list[tuple]:
    """返回 [(rect, 字节数)]，只保留真正的图片块（type==1）。"""
    blocks = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 1:
            continue
        bbox = block.get("bbox") or ()
        if len(bbox) != 4:
            continue
        width = float(bbox[2]) - float(bbox[0])
        height = float(bbox[3]) - float(bbox[1])
        # 太小的多半是 logo / 项目符号 / 公式碎片
        if width < 40 or height < 40:
            continue
        blocks.append((tuple(float(v) for v in bbox), len(block.get("image") or b"")))
    return blocks


def _caption_rect(page, caption: str):
    """在页面上找到题注文字的位置（只取前 40 字，太长会匹配不到）。"""
    needle = re.sub(r"\s+", " ", caption or "").strip()[:40]
    if not needle:
        return None
    try:
        rects = page.search_for(needle)
    except Exception:  # noqa: BLE001
        return None
    if rects:
        return rects[0]
    # 退一步：只搜 "Figure N" / "Table N" 这种前缀
    prefix = re.match(r"^((?:Figure|Fig\.?|Table)\s*\d+)", needle, re.I)
    if prefix:
        try:
            rects = page.search_for(prefix.group(1))
        except Exception:  # noqa: BLE001
            return None
        return rects[0] if rects else None
    return None


def pick_block(page, caption: str):
    """挑出这一页里最可能是"该题注对应的图"的图片块，返回 (x0,y0,x1,y1) 或 None。"""
    blocks = _image_blocks(page)
    if not blocks:
        return None
    cap = _caption_rect(page, caption)
    if len(blocks) == 1 or cap is None:
        # 只有一块：直接用它；多块又定位不到题注：退化成"最大那块"
        return max(blocks, key=lambda b: (b[0][2] - b[0][0]) * (b[0][3] - b[0][1]))[0]

    def score(item) -> tuple:
        bbox, _size = item
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        # 离题注越近越好（用块与题注的纵向间隙），面积大的优先
        if bbox[3] <= cap.y0:
            gap = cap.y0 - bbox[3]
        elif bbox[1] >= cap.y1:
            gap = bbox[1] - cap.y1
        else:
            gap = 0.0
        return (gap, -(width * height))

    return sorted(blocks, key=score)[0][0]


def crop_figure(pdf_path: Path, page_no: int, caption: str, out_path: Path) -> bytes | None:
    """把某一页的图裁成 PNG 字节；同时写一份到 out_path 当缓存。失败返回 None。

    page_no 是 **0 基**（structures.json 里存的页号口径）。
    """
    import pymupdf

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        logger.warning("打不开 PDF，无法裁图: %s", pdf_path)
        return None
    try:
        if not (0 <= page_no < len(doc)):
            return None
        page = doc[page_no]
        bbox = pick_block(page, caption)
        if bbox is None:
            return None
        clip = pymupdf.Rect(
            bbox[0] - PAD,
            bbox[1] - PAD,
            bbox[2] + PAD,
            bbox[3] + PAD,
        )
        clip = clip & page.rect  # 不能超出页面，否则 get_pixmap 会报错
        if clip.is_empty or clip.width < 20 or clip.height < 20:
            return None
        pix = page.get_pixmap(clip=clip, dpi=DPI)
        data = pix.tobytes("png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return data
    except Exception:  # noqa: BLE001
        logger.exception("裁图失败: %s 第 %d 页", pdf_path.name, page_no + 1)
        return None
    finally:
        doc.close()


def figure_cache_path(stem: str, index: int) -> Path:
    return FIGURES_DIR / stem / f"fig{index}.png"


def figure_bytes(pdf_path: Path, stem: str, index: int, page_no: int, caption: str) -> bytes | None:
    """先读缓存，没有再裁。"""
    cached = figure_cache_path(stem, index)
    if cached.exists():
        return cached.read_bytes()
    return crop_figure(pdf_path, page_no, caption, cached)


def page_png(pdf_path: Path, page_no: int, dpi: int = 110) -> bytes | None:
    """整页渲染成 PNG，当"裁不出图"时的兜底。

    CLIP / LayerNorm 这类论文的图是矢量绘制的（`get_text("dict")` 里没有
    type==1 的图片块），按图片块定位必然失败。这时至少把这一页给出来：
    用户能看见图，点一下也能跳到正文那一页。
    """
    import pymupdf

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return None
    try:
        if not (0 <= page_no < len(doc)):
            return None
        return doc[page_no].get_pixmap(dpi=dpi).tobytes("png")
    except Exception:  # noqa: BLE001
        logger.exception("整页渲染失败: %s 第 %d 页", pdf_path.name, page_no + 1)
        return None
    finally:
        doc.close()
