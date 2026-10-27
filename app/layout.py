"""原文版式判定：单栏 / 双栏（赛题 1.3.1「要考虑原文的排版」）。

PDF 里没有"这一页是几栏"这种元数据，只能从正文文本块的横向分布去猜：
双栏页的正文会分成左右两簇，中间留出一条几乎没有文字的中缝。

判定过程（每一页）：
    1. 取长度 ≥20 字的文本行（标题、表头、公式残片不参与，它们会污染分布）；
    2. 按行首 x 分左右两簇：x < 页宽 45% 算左簇，x > 页宽 55% 算右簇；
    3. 两簇都得有足够的行，且两簇之间真的有空隙（右簇最左 - 左簇最右 ≥ 8pt）；
    4. 横跨中缝的行不能太多（否则是单栏居中排版，不是双栏）。

判不出来（扫描件、页数太少、整页只有图）返回 columns=None，前端显示"未知"，
不硬猜 —— 猜错会让"按栏看"把整页切成两半。
"""

from __future__ import annotations

from pathlib import Path

import fitz

MIN_LINES = 8          # 少于这么多有效行，判不出
MIN_CLUSTER = 6        # 一簇至少这么多行才可能是"一栏"
GAP_RATIO = 0.32       # 两簇中心至少隔开 32% 页宽
SIDE_SHARE = (0.2, 0.8)  # 右簇行数占比落在 20%~80% 之间（避免整页只有一条边注）
SAMPLE_PAGES = 40      # 大文档只抽前 40 页（判定整篇版式足够）

# 按 (路径, mtime, 大小) 缓存：打开一次 PDF 要几百毫秒，阅读页每次刷新都会问
_cache: dict[tuple[str, float, int], dict] = {}


def _page_lines(page: fitz.Page) -> list[tuple[float, float]]:
    """返回这一页有效正文行的 (x0, x1)；只取够长的行，避免标题/公式干扰。"""
    rows: list[tuple[float, float]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if len(text.strip()) < 20:
                continue
            x0, _, x1, _ = line["bbox"]
            rows.append((float(x0), float(x1)))
    return rows


def _col_box(lines: list[tuple[float, float]], idxs: list[int]) -> list[float]:
    """一栏的横向范围 [左, 右]。

    用分位数而不是 min/max：跨栏的图表标题、页边注会把极端值撑开，
    取 10 分位的左边界、80 分位的右边界更接近"这一栏的正文宽度"。
    """
    x0s = sorted(lines[i][0] for i in idxs)
    x1s = sorted(lines[i][1] for i in idxs)
    left = x0s[int(len(x0s) * 0.1)]
    right = x1s[min(len(x1s) - 1, int(len(x1s) * 0.8))]
    return [round(left, 1), round(right, 1)]


def _columns_of(
    lines: list[tuple[float, float]], width: float
) -> tuple[int | None, float | None, list[list[float]] | None]:
    """一页的栏数、中缝位置、各栏横向范围。

    判定思路：把行首 x 排序后按空隙聚类，双栏页会自然分成两簇（左栏一簇、右栏一簇），
    中心相距 30% 以上页宽。比"按 x 是否过半页宽切两刀"稳，因为单栏页里
    右对齐的页码、右边注、宽表格都可能制造出 x 很大的行，但它们凑不成一簇。
    `cols` 给前端"按栏看"用：把 [左栏左, 左栏右, 右栏左, 右栏右] 放大到容器宽。
    """
    if len(lines) < MIN_LINES or width <= 0:
        return None, None, None

    order = sorted(range(len(lines)), key=lambda i: lines[i][0])
    clusters: list[list[int]] = [[order[0]]]
    for idx in order[1:]:
        x = lines[idx][0]
        if x - lines[clusters[-1][-1]][0] > max(12.0, 0.05 * width):
            clusters.append([idx])
        else:
            clusters[-1].append(idx)

    big = [c for c in clusters if len(c) >= MIN_CLUSTER]
    if len(big) < 2:
        return 1, None, None

    left_c, right_c = big[0], big[-1]
    left_center = sum(lines[i][0] for i in left_c) / len(left_c)
    right_center = sum(lines[i][0] for i in right_c) / len(right_c)
    if right_center - left_center < GAP_RATIO * width:
        return 1, None, None

    share = len(right_c) / len(lines)
    if not SIDE_SHARE[0] <= share <= SIDE_SHARE[1]:
        return 1, None, None

    # 中缝位置：取左半部分行尾的 75 分位，避开横跨两栏的图表标题
    crossing = (left_center + right_center) / 2
    left_edges = sorted(ln[1] for ln in lines if ln[0] < crossing)
    left_edge = left_edges[int(len(left_edges) * 0.75)] if left_edges else left_center
    right_edge = min(lines[i][0] for i in right_c)
    split = (left_edge + right_edge) / 2
    cols = [_col_box(lines, left_c), _col_box(lines, right_c)]
    return 2, round(split, 1), cols


def detect_layout(pdf_path: str | Path) -> dict:
    """判定整篇版式。

    返回：
        mode: two-column / single-column / mixed / unknown
        pages: {"6": {"columns": 2, "split": 300.1, "cols": [[55.0, 280.0], [307.0, 532.0]]}, ...}
               （键是 1 基页码；cols 是左右两栏的横向范围，前端"按栏看"用）
        switches: [7, 12]  版式发生变化的页码（从这一页起跟前一页不一样）
        total_pages / sampled_pages: 实际页数与参与判定的页数
    """
    path = Path(pdf_path)
    try:
        stat = path.stat()
    except OSError:
        return {"mode": "unknown", "pages": {}, "switches": [], "total_pages": 0, "sampled_pages": 0}

    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    pages: dict[str, dict] = {}
    order: list[tuple[int, int]] = []      # [(页码, 栏数)]
    total = 0
    with fitz.open(path) as doc:
        total = len(doc)
        limit = min(total, SAMPLE_PAGES)
        for idx in range(limit):
            page = doc[idx]
            cols, split, boxes = _columns_of(_page_lines(page), page.rect.width)
            pages[str(idx + 1)] = {"columns": cols, "split": split, "cols": boxes}
            if cols:
                order.append((idx + 1, cols))

    switches = [
        page_no
        for (prev_no, prev_cols), (page_no, cols) in zip(order, order[1:], strict=False)
        if cols != prev_cols
    ]
    two = sum(1 for _, c in order if c == 2)
    one = sum(1 for _, c in order if c == 1)
    if not order:
        mode = "unknown"
    elif two and one:
        mode = "mixed"
    elif two:
        mode = "two-column"
    else:
        mode = "single-column"

    result = {
        "mode": mode,
        "pages": pages,
        "switches": switches,
        "total_pages": total,
        "sampled_pages": limit,
    }
    _cache[key] = result
    return result


def layout_summary(pdf_path: str | Path) -> dict:
    """给章节树接口附带的精简版（前端顶栏徽章用，别把 40 页明细塞进 meta）。"""
    full = detect_layout(pdf_path)
    return {
        "mode": full["mode"],
        "switches": full["switches"],
        "sampled_pages": full["sampled_pages"],
        "total_pages": full["total_pages"],
    }
