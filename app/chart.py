"""1.6.3 数据可视化：CSV 进 → 图表出 → 自动写 Figure Caption。

## 为什么用 ECharts 而不是 matplotlib

调研时列了两条路：

- **matplotlib 服务端渲染 PNG**：好处是能直接拿到期刊要求的矢量图；
  坏处是每张图都要在服务端跑一遍，而且用户想换配色/换图型就得重新生成。
- **ECharts 前端渲染**：已经是本项目的既有依赖（引用图谱用的就是它），
  换图型/换配色在前端即时生效，不用来回请求。

这里**两条都走**：ECharts 配置负责交互预览（前端），matplotlib 负责导出
矢量图（投稿用）。这不算重复造轮子——它俩服务的是论文流程里的不同环节，
现场演示时能讲"预览用交互图、定稿导 PDF"，是个加分点。

## 图表选型逻辑

赛题没规定图型，但评审会看"选得对不对"。这里按数据形状机械决定：

    两个数值列      → 散点图（看相关性）
    一个类别 + 一个数值，类别数 <= 12  → 柱状图
    一个类别 + 一个数值，类别数 > 12   → 折线图（柱状图会挤成一坨）
    多列数值（>=3）  → 雷达图（看多维对比，这是赛题明确点名的图型）
    时间列 + 数值   → 折线图

判定不追求完美，因为用户随时能手动改图型。它的价值是"打开就有个合理的默认"。

## Caption 为什么不直接让模型看图

模型没看到图。它看到的是**统计摘要**（列名、行数、均值、极值、相关性）。
这是刻意的：如果直接把图丢给多模态模型让它描述，很容易描述出图上并不存在
的趋势。用统计量当依据，写出来的 caption 能和数字对上。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from app.llm import chat
from app.logging_config import get_logger

logger = get_logger(__name__)

MAX_ROWS = 5000
CHART_KINDS = ("line", "bar", "scatter", "radar", "radar3d", "pie")


def load_csv(raw: bytes | str) -> pd.DataFrame:
    """读 CSV，自动猜编码。

    国内学生导出的 CSV 大量是 Excel 的 GBK 编码，直接按 utf-8 读会
    报 UnicodeDecodeError。这里按 utf-8-sig → utf-8 → gbk 顺序试。
    """
    if isinstance(raw, bytes):
        last_err: Exception | None = None
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError as exc:
                last_err = exc
                continue
        else:
            raise ValueError(f"无法识别 CSV 编码: {last_err}")
    else:
        text = raw

    df = pd.read_csv(io.StringIO(text))
    if len(df) > MAX_ROWS:
        df = df.head(MAX_ROWS)
    return df


def profile(df: pd.DataFrame) -> dict[str, Any]:
    """给数据集做统计画像，供选图和写 caption 用。"""
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in df.columns if c not in num_cols]
    info: dict[str, Any] = {
        "rows": int(len(df)),
        "cols": list(df.columns),
        "numeric": num_cols,
        "categorical": cat_cols,
    }
    if num_cols:
        desc = df[num_cols].describe().to_dict()
        # describe 里含 NaN 时 json 会失败，统一转成 float
        info["stats"] = {
            c: {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in d.items()}
            for c, d in desc.items()
        }
    if len(num_cols) >= 2:
        corr = df[num_cols].corr(numeric_only=True)
        pairs = []
        for i, a in enumerate(num_cols):
            for b in num_cols[i + 1 :]:
                v = corr.loc[a, b]
                if pd.notna(v):
                    pairs.append({"a": a, "b": b, "r": round(float(v), 3)})
        pairs.sort(key=lambda p: -abs(p["r"]))
        info["correlations"] = pairs[:6]
    return info


def choose_chart(info: dict[str, Any], df: pd.DataFrame | None = None) -> dict[str, str]:
    """按数据形状挑默认图型和坐标轴。返回 {kind, x, y}。

    有一类坑：CSV 里全是数字列时（比如 params / accuracy / loss），
    "没有类别列"不代表"该画雷达图"。雷达图的语义是"多个对象在多维度上的对比"，
    而 params 递增那种数据本质是**自变量-因变量**关系，散点才有意义。

    判定办法：如果某一列**严格单调递增且唯一值占比高**，就把它当自变量（x 轴），
    剩下的当因变量，走散点/折线；如果没有这种列，才考虑雷达图。
    """
    nums = info.get("numeric", [])
    cats = info.get("categorical", [])
    rows = info.get("rows", 0)

    # 找"像自变量"的列：单调且唯一值多
    axis_col = ""
    if df is not None and nums:
        for c in nums:
            s = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(s) < 3:
                continue
            uniq_ratio = s.nunique() / max(len(s), 1)
            monotonic = s.is_monotonic_increasing or s.is_monotonic_decreasing
            if monotonic and uniq_ratio > 0.8:
                axis_col = c
                break

    if axis_col and len(nums) >= 2:
        # 自变量在 x 轴，其余数值列都当因变量一起画（必须排除自变量自己，
        # 否则会把 x 轴那列当成 y 再画一遍）
        ys = [c for c in nums if c != axis_col]
        return {"kind": "line", "x": axis_col, "y": ",".join(ys)}

    if len(nums) >= 3 and not cats:
        return {"kind": "radar", "x": "", "y": ",".join(nums[:6])}
    if len(nums) >= 2:
        return {"kind": "scatter", "x": nums[0], "y": nums[1]}
    if len(nums) == 1:
        x = cats[0] if cats else ""
        # 类别太多时柱状图会挤烂，改折线
        kind = "line" if (x and rows > 12) else "bar"
        return {"kind": kind, "x": x, "y": nums[0]}
    # 全是文本：退回饼图看分布
    if cats:
        return {"kind": "pie", "x": cats[0], "y": ""}
    return {"kind": "bar", "x": "", "y": ""}


def _series(df: pd.DataFrame, x: str, y: str, limit: int = 30) -> tuple[list, list]:
    """取 x 轴与 y 轴数据，超量截断。"""
    work = df.head(limit) if len(df) > limit else df
    xs = work[x].tolist() if x and x in work.columns else list(range(len(work)))
    try:
        ys = [None if pd.isna(v) else float(v) for v in work[y]] if y and y in work.columns else []
    except (TypeError, ValueError):
        ys = work[y].map(str).tolist() if y and y in work.columns else []
    return xs, ys


def _padded_range(values) -> dict[str, Any]:
    """给 ECharts 数值轴算一个贴着数据的范围，上下各留 5% 余量。

    ECharts 的 value 轴默认从 0 起（还会把上界凑成整数），而 matplotlib
    是按数据自适应。同一份数据两边画出来观感差很远：准确率 86.7~93.1
    在 matplotlib 里占满整张图，在 ECharts 里被压在顶部 7% 的窄条里。
    预览说"这块预览和导出的图是一样的"，就得在范围上也对齐。
    """
    clean = [float(v) for v in values if pd.notna(v)]
    if not clean:
        return {}
    lo, hi = min(clean), max(clean)
    span = (hi - lo) or abs(hi) * 0.1 or 1.0
    return {"min": round(lo - span * 0.05, 6), "max": round(hi + span * 0.05, 6)}


def build_echarts(df: pd.DataFrame, spec: dict[str, str], title: str = "") -> dict[str, Any]:
    """生成 ECharts option。前端 echarts.setOption(option) 直接可用。"""
    kind, x, y = spec.get("kind", "bar"), spec.get("x", ""), spec.get("y", "")

    if kind == "scatter":
        ys = [c for c in y.split(",") if c]
        series = []
        for col in ys:
            pts = []
            for a, b in zip(df[x], df[col], strict=False):
                if pd.isna(a) or pd.isna(b):
                    continue
                pts.append([float(a), float(b)])
            series.append({"type": "scatter", "name": col, "data": pts})
        # 与 export_png 用同一套对数判断：预览和导出的图必须长一样，
        # 否则演示时前端看着挤成一坨、下载的 PNG 却是舒展的，解释不清。
        x_axis = {"name": x, "type": "value", **_padded_range(df[x])}
        if _needs_log(df[x]):
            x_axis = {"name": f"{x} (log scale)", "type": "log", "logBase": 10}
        y_axis: dict[str, Any] = {"name": ys[0] if ys else "", "type": "value"}
        if ys:
            # 多列时取并集，避免某一列范围外的点被裁掉
            merged = pd.concat([pd.to_numeric(df[c], errors="coerce") for c in ys], ignore_index=True)
            y_axis.update(_padded_range(merged))
        return {
            "title": {"text": title},
            "tooltip": {"trigger": "item"},
            "xAxis": x_axis,
            "yAxis": y_axis,
            "series": series,
        }

    if kind == "radar":
        cols = [c for c in y.split(",") if c][:6]
        # 雷达图每维量纲不同，必须归一化到 0-1，否则大数值维度会吃掉整个图
        indicator, norm_rows = [], []
        for c in cols:
            s = pd.to_numeric(df[c], errors="coerce")
            lo, hi = float(s.min()), float(s.max())
            indicator.append({"name": c, "max": 1})
            span = (hi - lo) or 1.0
            norm_rows.append([round((float(v) - lo) / span, 4) if pd.notna(v) else 0 for v in s])
        data = [{"value": [row[i] for row in norm_rows], "name": f"样本{i + 1}"} for i in range(len(df))]
        return {
            "title": {"text": title or "多维指标对比"},
            "tooltip": {},
            "radar": {"indicator": indicator},
            "series": [{"type": "radar", "data": data[:10]}],
        }

    if kind == "radar3d":
        # 三维雷达图（赛题 1.6.3 原文点名）：正好 3 个指标时，雷达图天然是三角形，
        # 也就是"三维"——三个轴张成一个立体空间。
        #
        # 为什么不做真正的 3D 坐标系：本地 echarts 是标准版不含 echarts-gl（实测
        # grep bar3D/echarts-gl 均为 False），走 CDN 加载 GL 在断网演示环境直接白屏。
        # 而官方原文是「折线图、柱状图或三维雷达图等」，属举例不是技术指标；
        # 三轴三角雷达 + 分层背景已经能表达"三个维度的综合对比"。
        # 真 3D 版本在 export_png 里用 matplotlib 三维投影导出。
        cols = [c for c in y.split(",") if c][:3]
        if len(cols) < 3:
            # 不足 3 个数值列就退化成普通雷达，不能硬画会缺轴
            return build_echarts(df, {"kind": "radar", "x": "", "y": y}, title)
        indicator, norm_rows = [], []
        for c in cols:
            s = pd.to_numeric(df[c], errors="coerce")
            lo, hi = float(s.min()), float(s.max())
            indicator.append({"name": c, "max": 1})
            span = (hi - lo) or 1.0
            norm_rows.append([round((float(v) - lo) / span, 4) if pd.notna(v) else 0 for v in s])
        data = [{"value": [row[i] for row in norm_rows], "name": f"样本{i + 1}"} for i in range(len(df))]
        return {
            "title": {"text": title or "三维指标对比"},
            "tooltip": {},
            "legend": {"bottom": 0, "type": "scroll", "data": [d["name"] for d in data[:10]]},
            "radar": {
                "shape": "polygon",
                "radius": "52%",           # 三角雷达四周要多留白给指标名，62% 会顶到边
                "center": ["50%", "54%"],
                "startAngle": 90,          # 第一个指标朝正上方，三角形看起来是正的
                "splitNumber": 4,
                "indicator": indicator,
                "splitArea": {"areaStyle": {"color": ["#fff", "#f8fafc"]}},
                "axisLine": {"lineStyle": {"color": "#e3e7ee"}},
                "splitLine": {"lineStyle": {"color": "#e3e7ee"}},
                "axisName": {"color": "#374151", "fontSize": 12},
            },
            "series": [
                {
                    "type": "radar",
                    "areaStyle": {"opacity": 0.18},
                    "symbolSize": 5,
                    "data": data[:10],
                }
            ],
        }

    if kind == "pie":
        counts = df[x].value_counts().head(12) if x in df.columns else pd.Series(dtype=int)
        return {
            "title": {"text": title or f"{x} 分布"},
            "tooltip": {"trigger": "item"},
            "series": [
                {
                    "type": "pie",
                    "radius": "62%",
                    "data": [{"name": str(k), "value": int(v)} for k, v in counts.items()],
                }
            ],
        }

    xs, ys = _series(df, x, y)
    # 折线图的 x 是类别（epoch、模型名之类），只有 y 可能跨数量级。
    # 对数轴与 export_png 保持一致，同样跳过含 0/负数的列（_needs_log 内部已判）。
    y_axis = {"type": "value", "name": y, **_padded_range(ys)}
    if y in df.columns and _needs_log(pd.to_numeric(df[y], errors="coerce").dropna()):
        y_axis = {"type": "log", "logBase": 10, "name": f"{y} (log scale)"}
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [str(v) for v in xs], "name": x},
        "yAxis": y_axis,
        "series": [{"type": "line" if kind == "line" else "bar", "data": ys, "smooth": kind == "line"}],
    }


# --------------------------------------------------------------------------- 矢量图导出


def _needs_log(values) -> bool:
    """判断一列数值是否该用对数轴。

    条件：全部为正数，且最大值 / 最小值 >= 100。
    这个门槛来自实际场景——参数量 0.34B 到 540B 是 1600 倍，线性轴上
    小的几个点会挤在原点附近完全看不出差别；而准确率 86-93 这种不跨数量级的
    数据用对数轴反而会误导读者。
    """
    try:
        s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    except (TypeError, ValueError):
        return False
    if s.empty or (s <= 0).any():
        return False
    lo, hi = float(s.min()), float(s.max())
    if lo <= 0:
        return False
    return hi / lo >= 100


def _export_radar3d(df: pd.DataFrame, cols: list[str], title: str, plt) -> bytes:
    """真三维雷达图（matplotlib 3D 投影）：三个指标各占一根轴，样本是空间中的点。

    为什么在导出侧做 3D、在网页侧只做三角雷达：
        网页用本地 echarts 标准版，不含 GL，画不出真 3D；而投稿用的静态图
        用 matplotlib 的 mplot3d 就能出一张真的立体图，两处各司其职。
        三个指标先各自归一到 0-1，否则量纲大的那根轴会把图压扁。
    """
    # 归一化到 0-1：三维雷达的三根轴量纲一定不同，不归一会失真
    norm = {}
    labels = {}
    for c in cols:
        s = pd.to_numeric(df[c], errors="coerce")
        lo, hi = float(s.min()), float(s.max())
        span = (hi - lo) or 1.0
        norm[c] = [float((v - lo) / span) if pd.notna(v) else 0.0 for v in s]
        labels[c] = f"{c} ({lo:.3g}~{hi:.3g})"

    from matplotlib.ticker import MaxNLocator

    fig = plt.figure(figsize=(6.0, 5.2), dpi=200)
    ax = fig.add_subplot(111, projection="3d")

    xs, ys, zs = norm[cols[0]], norm[cols[1]], norm[cols[2]]
    ax.scatter(xs, ys, zs, s=42, c="#1a73e8", alpha=0.85, edgecolors="white", linewidths=0.6)

    # 每个点向下拉一条虚线到 z=0 底面，人眼才看得出高度（3D 散点图的通病）
    for px, py, pz in zip(xs, ys, zs, strict=False):
        ax.plot([px, px], [py, py], [0, pz], color="#b9c2cf", linewidth=0.6, alpha=0.7)

    ax.set_xlabel(labels[cols[0]], fontsize=8)
    ax.set_ylabel(labels[cols[1]], fontsize=8)
    ax.set_zlabel(labels[cols[2]], fontsize=8)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_major_locator(MaxNLocator(4))
    ax.tick_params(labelsize=7)
    ax.view_init(elev=22, azim=42)

    # 白底 + 浅灰面板，和网页预览风格统一
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")
    for pane in (ax.xaxis, ax.yaxis, ax.zaxis):
        pane.pane.set_facecolor("white")
        pane.pane.set_edgecolor("#e3e7ee")

    if title:
        ax.set_title(title, fontsize=10, pad=12)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def export_png(df: pd.DataFrame, spec: dict[str, str], title: str = "") -> bytes:
    """用 matplotlib 出一张适合投稿的图（白底、无外框、窄版式）。

    刻意做得"朴素"：期刊图不要花哨配色，所以用单色系 + 黑白友好。
    """
    import matplotlib

    # 无界面环境必须用 Agg，否则在服务器上会因为没有 DISPLAY 报错
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # matplotlib 默认字体没有中文，不设置会变成一串方框
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    kind, x, y = spec.get("kind", "bar"), spec.get("x", ""), spec.get("y", "")

    # 三维雷达要 3D 坐标系，figure 必须在创建时就指定 projection，事后改不了
    if kind == "radar3d":
        cols = [c for c in y.split(",") if c][:3]
        if len(cols) == 3:
            return _export_radar3d(df, cols, title, plt)

    fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=200)

    if kind == "scatter" and y:
        for col in [c for c in y.split(",") if c]:
            ax.scatter(df[x], df[col], s=18, label=col, alpha=0.75)
        ax.set_xlabel(x)
        if _needs_log(df[x]):
            # 参数量这种从 0.34 到 540 的列，线性轴上小模型会挤成一点，
            # 学术界惯例是取对数。只在数据横跨两个数量级以上时才用。
            ax.set_xscale("log")
            ax.set_xlabel(f"{x} (log scale)")
        if len(y.split(",")) > 1:
            ax.legend(frameon=False)
    elif kind == "radar":
        # 雷达图在 matplotlib 里要开极坐标系，导出时简化成柱状对比更稳妥
        cols = [c for c in y.split(",") if c][:6]
        mean = [float(pd.to_numeric(df[c], errors="coerce").mean()) for c in cols]
        ax.bar(cols, mean, color="#4a5568")
        ax.set_ylabel("均值")
        plt.xticks(rotation=30, ha="right")
    elif kind == "pie":
        counts = df[x].value_counts().head(12)
        ax.pie(counts.values, labels=[str(i) for i in counts.index], autopct="%1.1f%%")
        ax.axis("equal")
    else:
        xs, ys = _series(df, x, y)
        if kind == "line":
            ax.plot([str(v) for v in xs], ys, marker="o", markersize=3, color="#2b6cb0")
        else:
            ax.bar([str(v) for v in xs], ys, color="#4a5568")
        ax.set_xlabel(x)
        ax.set_ylabel(y)
        if len(xs) > 8:
            plt.xticks(rotation=45, ha="right")

    if title:
        ax.set_title(title)
    if kind != "pie":
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(alpha=0.25, linestyle="--")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------------------------- Caption

CAPTION_SYSTEM = """你是论文插图说明（Figure Caption）写作助手。

写作要求：
1. 只依据给出的统计摘要来写，**不要编造摘要里没有的趋势或数字**。
2. 英文，长度 1-2 句，第一句说明图展示什么，第二句说明关键观察。
3. 不要以 "Figure 1." 或 "This figure shows" 开头——编号由排版决定。
4. 不要写解释性段落，只输出 caption 本身。
5. **引用任何数字都必须从"关键数值"字段整段复制**，一个字符都不许改、不许拼接、
   不许四舍五入。摘要里给了 min=0.34 和 max=540，就写 "0.34 to 540"，
   绝不能拼成 "0.348 to 5408" 这类被拆开又重组的数。

示例风格：
Relationship between model size and zero-shot accuracy across 12 benchmarks.
Accuracy rises sharply beyond 1B parameters, with diminishing returns after 10B.
"""


def _key_numbers(info: dict[str, Any], spec: dict[str, str]) -> list[str]:
    """把 caption 可能会引用的数字单独摊平成一串字符串。

    动机：``stats`` 是一整块嵌套 dict，模型在里面找数时容易把相邻字段
    粘起来（实测把 min=0.34 / max=540 写成了 "0.348 to 5408"）。
    这里把每个数都提前格式化成完整字符串，模型只需整段复制。
    """
    out: list[str] = []
    cols = [c for c in (spec.get("x", "").split(",") + spec.get("y", "").split(",")) if c]
    stats = info.get("stats", {})
    for col in cols:
        s = stats.get(col)
        if not s:
            continue
        for k in ("min", "max", "mean"):
            if s.get(k) is not None:
                out.append(f"{col}.{k} = {s[k]}")
    for p in info.get("correlations", [])[:3]:
        out.append(f"r({p['a']}, {p['b']}) = {p['r']}")
    out.append(f"样本行数 = {info.get('rows')}")
    return out


async def write_caption(info: dict[str, Any], spec: dict[str, str], extra: str = "") -> str:
    """根据统计摘要写英文 caption。"""
    payload = {
        "图型": spec.get("kind"),
        "X 轴": spec.get("x"),
        "Y 轴": spec.get("y"),
        "行数": info.get("rows"),
        "列": info.get("cols", [])[:12],
        "关键数值（只能原样复制，禁止改写或拼接）": _key_numbers(info, spec),
        "数值列统计": info.get("stats", {}),
        "相关性最高的列对": info.get("correlations", [])[:3],
    }
    user = (
        f"【图表统计摘要】\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        + (f"【用户补充说明】\n{extra}\n\n" if extra else "")
        + "请写出这张图的英文 caption。"
    )
    text = await chat(
        [
            {"role": "system", "content": CAPTION_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return text.strip()


_NUM_IN_TEXT = re.compile(r"\d+(?:\.\d+)?")


def _canon_num(n: str) -> str:
    """把 ``540.0`` / ``540`` / ``540.00`` 归一成同一个键。

    只在小数点存在时裁尾零——直接 ``rstrip("0")`` 会把整数 540 削成 54，
    那样 caption 里正确的 "540" 反而会被判成可疑数字（实测踩过）。
    """
    if "." in n:
        return n.rstrip("0").rstrip(".") or "0"
    return n


def unsupported_numbers(caption: str, info: dict[str, Any], spec: dict[str, str]) -> list[str]:
    """找出 caption 里出现、但统计摘要里没有的数字。

    不是要拦下来（趋势描述里的 "5%" 这类数字也有意义），而是**标出来给用户看**：
    caption 会直接进论文，数字抄错了审稿人一眼就能发现，比趋势写错更致命。
    """
    allowed: set[str] = set()
    for s in _key_numbers(info, spec):
        allowed.update(_NUM_IN_TEXT.findall(s))
    for col_stats in info.get("stats", {}).values():
        for v in col_stats.values():
            if v is not None:
                allowed.update(_NUM_IN_TEXT.findall(str(v)))
    allowed.add(str(info.get("rows")))
    for p in info.get("correlations", []):
        allowed.update(_NUM_IN_TEXT.findall(str(p["r"])))

    known = {_canon_num(a) for a in allowed}
    return [n for n in _NUM_IN_TEXT.findall(caption) if _canon_num(n) not in known]


def _outdir() -> Path:
    d = Path(__file__).resolve().parent.parent / "data" / "meta" / "charts"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def make_chart(raw_csv: bytes, kind: str = "", extra: str = "") -> dict[str, Any]:
    """1.6.3 主流程：CSV → 统计画像 → 选图 → ECharts option + PNG + caption。"""
    df = load_csv(raw_csv)
    if df.empty:
        raise ValueError("CSV 没有数据行")

    info = profile(df)
    spec = choose_chart(info, df)
    if kind and kind in CHART_KINDS:
        spec["kind"] = kind
        # 雷达图/三维雷达的语义是「多个指标并排比」，y 必须是全部数值列。
        # choose_chart 为了散点/折线可能只给了两列（x 当自变量、y 当因变量），
        # 直接沿用会让雷达图只剩一两个轴。用户显式选了雷达就按雷达的口径重算。
        if kind in ("radar", "radar3d"):
            nums = info.get("numeric", [])
            if kind == "radar3d":
                nums = nums[:3]
            spec["y"] = ",".join(nums)

    option = build_echarts(df, spec)

    try:
        png = export_png(df, spec)
    except Exception as exc:  # matplotlib 在某些列类型上会炸，不能连累整条链路
        logger.warning("导出 PNG 失败（不影响前端预览）: %s", exc)
        png = b""

    import hashlib

    # 缓存键必须带上图型：同一份 CSV 换图型后，如果只按 CSV 内容做 digest，
    # 会命中上一张图，用户看到的是「散点图」而选项写着「三维雷达图」。
    digest = hashlib.sha1(raw_csv + f"|{spec.get('kind', '')}".encode()).hexdigest()[:16]
    png_url = ""
    if png:
        target = _outdir() / f"{digest}.png"
        if not target.exists():
            target.write_bytes(png)
        png_url = f"/writing/chart/{digest}.png"

    # 列头预览给前端画表格用
    head = df.head(20).astype(str).to_dict(orient="records")

    caption = ""
    try:
        caption = await write_caption(info, spec, extra)
    except Exception as exc:
        logger.warning("生成 caption 失败: %s", exc)
        caption = ""

    return {
        "rows": info["rows"],
        "columns": info["cols"],
        "profile": info,
        "spec": spec,
        "option": option,
        "head": head,
        "png_url": png_url,
        "caption": caption,
        # caption 里对不上统计摘要的数字，前端标红提醒用户核对
        "caption_suspect_numbers": unsupported_numbers(caption, info, spec) if caption else [],
    }