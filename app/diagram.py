"""1.6.2 论文架构图生成：输入一句话描述 → 出可渲染的图 + 直接看得到的预览。

## 为什么选 Mermaid 和 Graphviz 这两个

调研结论（见 docs/writing-assistant-research.md）：能本地渲染、装完即用、
而且能一键变成论文级矢量图的工具，只有这四类。按"论文场景适配度"排：

- **Mermaid**：流程图/时序图语法最接近自然语言，模型基本不写错；
  但默认出图偏"网页味"，圆角+彩色，放进论文需要调主题。
- **Graphviz**：出的图最像学术论文里的下图（黑白、方框、正交边），
  排版引擎会把节点自动排好，适合画流程图和系统模块图。
- **TikZ**：论文模板里最正宗（IEEE/ACM 模板直接支持），但模型写 TikZ
  的语法错误率明显更高，一步渲染失败就得整段重来。
- **PlantUML**：需要 Java 运行时装 JAR，多一层依赖。

所以先做前两个。TikZ 留了接口但没开放——真要上，得先加"编译失败自动重试"
的兜底，否则现场演示一次红字就崩了。

## 渲染失败时怎么办

模型写的 Mermaid/Graphviz 代码**不是每次都能过**。实测中括号没闭合、
箭头写成 `->` 而不是 `-->` 都很常见。这里的处理是：

1. 先原样渲染
2. 失败就把错误信息回喂给模型，让它只改语法、不改结构，重试一次
3. 还失败就返回"渲染失败+原始代码+错误信息"，让人自己改

**不做无限重试**——既浪费额度，又可能让模型越改越偏。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.llm import chat
from app.logging_config import get_logger

logger = get_logger(__name__)

# 中文字体：微软雅黑几乎每台 Windows 都有，中文+英文混排观感也最好。
# Graphviz 自带的 Times-Roman 不含 CJK 字形，不显式指定就渲染成方框。
CJK_FONT = "Microsoft YaHei"

# mmdc / dot 是 npm 包和 Graphviz 安装出来的可执行文件，不在虚拟环境里，
# 而且 Windows 下常常有 .cmd 后缀。这里按固定顺序找，找不到就报清晰的错。
_EXTRA_BIN_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "npm",
    Path("C:/Program Files/Graphviz/bin"),
    Path("C:/Program Files (x86)/Graphviz/bin"),
    Path("C:/Program Files/nodejs"),
    Path("C:/Program Files (x86)/nodejs"),
]


def _which(name: str) -> str | None:
    """找可执行文件，比 shutil.which 多查两个常见安装目录。

    注意不能用 ``shutil.which(name)``：在 Windows 上 PATHEXT 里
    ``.CMD`` 在 ``.EXE`` 前面，而 ``C:\\Users\\<user>\\AppData\\Roaming\\npm\\mmdc``
    还挂着一个**无扩展名**的 shell 脚本（给 Git Bash 用的）。shutil.which
    会先命中那个无扩展名的文件，交给 subprocess 就报 WinError 193。
    所以这里手动按 .cmd → .exe → .bat → 无后缀 的顺序找，Windows 原生优先。
    """
    suffixes = (".cmd", ".exe", ".bat", "") if os.name == "nt" else ("",)
    for d in _search_dirs():
        for suffix in suffixes:
            p = d / f"{name}{suffix}"
            if p.is_file():
                return str(p)
    return None


def _search_dirs() -> list[Path]:
    """PATH 里的目录 + 安装目录，按优先级排好。"""
    dirs: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw.strip():
            dirs.append(Path(raw))
    # 把已知安装目录插到最前面，避免被 PATH 里别的同名文件抢走
    return _EXTRA_BIN_DIRS + dirs


# --------------------------------------------------------------------------- TikZ

# 模型只输出 tikzpicture 环境体，preamble 由我们固定拼上。
# 为什么不让模型自己写完整 .tex：它经常把 \documentclass 写错（article 缺
# tikz 包、或者用了 standalone 但没装），一错整份编译就废；
# preamble 固定住以后，出错范围只剩图本身，好定位也好兜底。
TIKZ_PREAMBLE = r"""\documentclass[border=6pt]{standalone}
\usepackage{tikz}
\usepackage{ctex}
\usetikzlibrary{arrows.meta,positioning,shapes.geometric,fit,backgrounds,calc}
\begin{document}
"""
TIKZ_POSTAMBLE = "\n\\end{document}\n"


def build_tikz_source(code: str) -> str:
    """把模型给的 tikzpicture 体包成完整可编译的 .tex。

    模型有三种常见输出：
        1. 只有 tikzpicture 环境        → 直接包 preamble
        2. 只有环境体（没 \\begin）     → 补上 \\begin{tikzpicture}
        3. 已经写了完整文档             → 原样返回，别重复包
    """
    body = strip_fence(code)
    if "\\documentclass" in body:
        return body if body.endswith("\n") else body + "\n"
    if "\\begin{tikzpicture}" in body:
        return TIKZ_PREAMBLE + body + TIKZ_POSTAMBLE
    if "\\end{tikzpicture}" in body:
        return TIKZ_PREAMBLE + body + TIKZ_POSTAMBLE
    return TIKZ_PREAMBLE + "\\begin{tikzpicture}\n" + body + "\n\\end{tikzpicture}" + TIKZ_POSTAMBLE


def _pdf_first_page_png(pdf: Path, dpi: int = 200) -> bytes | None:
    """把编译出的 PDF 第一页转成 PNG。用 pymupdf，项目里已经有这个依赖。"""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 老版本包名
        except ImportError:
            return None
    try:
        doc = pymupdf.open(str(pdf))
        if doc.page_count == 0:
            return None
        zoom = dpi / 72.0
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("png")
        doc.close()
        return data
    except Exception as exc:  # noqa: BLE001 - 转换失败按渲染失败处理
        logger.warning("PDF 转 PNG 失败: %s", exc)
        return None


def render_tikz(code: str, out_dir: Path) -> tuple[bytes | None, str]:
    """把 TikZ 代码用 pdflatex 编译成 PDF，再转 PNG。返回 (图片字节, 错误信息)。

    ⚠ 失败兜底有三层，任何一层都不能抛到调用方：
        1. 本机没装 pdflatex → 直接返回可读错误（`renderer_available()` 会提前告知前端）
        2. 编译失败 → 从 MiKTeX 的日志里抠出 ``! `` 开头的首条错误回给模型
        3. PDF 转图片失败 → 回可读错误，不 500
    """
    exe = _which("pdflatex")
    if not exe:
        return None, "本机未安装 LaTeX（pdflatex），无法渲染 TikZ"
    out_dir.mkdir(parents=True, exist_ok=True)
    source = build_tikz_source(code)

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "figure.tex"
        src.write_text(source, encoding="utf-8")
        # -interaction=nonstopmode 是必须的：默认遇到错误会等人输入，
        # 在无 tty 的服务进程里会一直挂到超时。
        argv = [
            exe,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory",
            td,
            str(src),
        ]
        rc, log = _run(argv, cwd=Path(td), timeout=120)
        pdf = Path(td) / "figure.pdf"
        if rc != 0 or not pdf.exists():
            # 只把第一条 ! 错误挖出来回喂，整段日志太长模型也读不进去
            first = ""
            for line in log.splitlines():
                if line.startswith("! "):
                    first = line.strip()
                    break
            if not first:
                # 没有 ! 行时退一步找 l.<数字> 那行，LaTeX 的报错位置标记
                for line in log.splitlines():
                    if re.match(r"^l\.\d+", line.strip()):
                        first = line.strip()
                        break
            detail = first or log.strip()[:300]
            return None, f"pdflatex 编译失败（退出码 {rc}）: {detail}"
        png = _pdf_first_page_png(pdf)
        if png is None:
            return None, "LaTeX 编译成功，但 PDF 转 PNG 失败（检查 pymupdf 是否可用）"
        return png, ""


def renderer_available() -> dict[str, bool]:
    """给前端展示「本机能不能渲染」，避免用户点了才知道不能用。"""
    return {"mermaid": bool(_which("mmdc")), "graphviz": bool(_which("dot")), "tikz": bool(_which("pdflatex"))}


# --------------------------------------------------------------------------- prompt

SYSTEM = """你是论文绘图助手。把用户描述的系统结构画成示意图。

规则：
1. 只画真实存在的模块，**不要虚构用户没提到的组件**。
2. 节点名用中文或英文均可，但保持简短（不超过 12 个字）。
3. 不要写解释文字，不要写 Markdown 代码块围栏。
4. 图要能表达数据流动方向，不要只画一堆孤立方块。
"""


def build_prompt(description: str, kind: str) -> list[dict]:
    """生成绘制对应图类型的对话消息。抽出来便于单测。"""
    if kind == "mermaid":
        spec = (
            "输出一段 **Mermaid flowchart** 代码，形如：\n"
            "flowchart TD\n"
            "    A[输入文本] --> B[编码器]\n"
            "    B --> C[解码器]\n"
            "只输出代码本身，第一行必须是 flowchart TD 或 flowchart LR。"
        )
    elif kind == "graphviz":
        spec = (
            "输出一段 **Graphviz DOT** 代码，形如：\n"
            'digraph G {\n  rankdir=LR;\n  node [shape=box, fontsize=12];\n'
            '  A [label="输入"];\n  A -> B;\n}\n'
            "只输出代码本身，必须从 digraph 开始、以 } 结束。\n"
            "注意：所有 node 用同一个 fontsize，不要给某个节点单独设 style/size，"
            "否则同一张图里字号会不一致。"
        )
    elif kind == "tikz":
        spec = (
            "输出一段 **TikZ 代码**，只写 tikzpicture 环境本身，形如：\n"
            "\\begin{tikzpicture}[node distance=1.6cm, "
            "box/.style={draw, rounded corners, minimum width=2.6cm, minimum height=0.9cm, font=\\small}]\n"
            "  \\node[box] (a) {输入文本};\n"
            "  \\node[box, right=of a] (b) {编码器};\n"
            "  \\draw[-{Stealth}] (a) -- (b);\n"
            "\\end{tikzpicture}\n"
            "硬性要求：\n"
            "1. 不要写 \\documentclass、\\begin{document}、\\usepackage，这些由系统自动补；\n"
            "2. 箭头用 `-{Stealth}`，需要 arrows.meta 之外不要引用其它库；\n"
            "3. 节点用 `\\node[box]` 统一风格，中文节点直接写中文；\n"
            "4. 只输出代码，不要 Markdown 围栏，不要解释。"
        )
    else:
        raise ValueError(f"不支持的图类型: {kind}")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"【要画的系统】\n{description.strip()}\n\n{spec}"},
    ]


def strip_fence(code: str) -> str:
    """模型经常无视"不要写围栏"的指令，把 ```mermaid ... ``` 一起吐出来。"""
    text = (code or "").strip()
    if "```" not in text:
        return text
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- 渲染

def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """跑外部命令，返回 (退出码, 合并后的输出)。找不到可执行文件返回 127。

    Windows 上 ``mmdc`` 实际是 ``mmdc.cmd``，直接交给 subprocess 会报
    ``WinError 193: 不是有效的 Win32 应用程序``——因为 .cmd 不是 PE 可执行文件。
    必须显式经 cmd.exe 调用。
    """
    argv = list(cmd)
    if not argv or not argv[0]:
        return 127, f"找不到可执行文件: {cmd[0] if cmd else cmd}"
    if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c"] + argv
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"找不到可执行文件: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"渲染超时（{timeout}s）"
    except OSError as exc:
        # 权限、路径非法等，别让整个请求 500，转成可读错误
        return 126, f"无法执行 {cmd[0]}: {exc}"


def render_mermaid(code: str, out_dir: Path) -> tuple[bytes | None, str]:
    """把 Mermaid 代码渲染成 PNG。返回 (图片字节, 错误信息)。

    这里**绕开 mmdc.cmd**，直接用 node 跑 cli.js。原因是那个 .cmd 里写死了
    用 ``node`` 这个名字去调起 node.exe，而服务若是被测试框架/IDE 拉起来的，
    PATH 里不一定有 node 目录，就会报
    ``'"node"' is not recognized as an internal or external command``。
    直接找 node.exe 并走 cli.js 更稳。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "g.mmd"
        dst = Path(td) / "g.png"
        src.write_text(code, encoding="utf-8")

        node = _which("node")
        cli = (
            Path(os.environ.get("APPDATA", ""))
            / "npm"
            / "node_modules"
            / "@mermaid-js"
            / "mermaid-cli"
            / "src"
            / "cli.js"
        )
        if node and cli.exists():
            # -b white 是必须的：默认透明背景在深色网页里几乎看不见
            argv = [node, str(cli), "-i", str(src), "-o", str(dst), "-b", "white", "-s", "2"]
        else:
            exe = _which("mmdc")
            if not exe:
                return None, "本机未安装 mermaid-cli（既没有 node + mermaid-cli，也没有 mmdc）"
            argv = [exe, "-i", str(src), "-o", str(dst), "-b", "white", "-s", "2"]

        rc, log = _run(argv, cwd=Path(td), timeout=90)
        if rc != 0 or not dst.exists():
            return None, f"mmdc 渲染失败: {log.strip()[:600]}"
        return dst.read_bytes(), ""


def _ensure_font(code: str) -> str:
    """给 DOT 代码补上中文字体，否则中文会渲染成方框。

    Graphviz 默认字体是 Times-Roman，**不含 CJK 字形**，所以中文节点
    渲染出来是一排豆腐块（实测：节点文字全部变成空格方框）。
    模型写的代码往往只写 ``node [shape=box]`` 而不指定 fontname。

    这里在 ``digraph ... {`` 后插一条全局默认，不改动模型写的内容。
    如果模型自己指定了 fontname，我们插入的会被它后面的覆盖——这也是期望行为。
    """
    if "fontname" in code:
        return code
    m = re.search(r"^(digraph[^{]*\{)", code, re.MULTILINE)
    if not m:
        return code
    inject = (
        f'\n  graph [fontname="{CJK_FONT}"];\n'
        f'  node [fontname="{CJK_FONT}"];\n'
        f'  edge [fontname="{CJK_FONT}"];'
    )
    return code[: m.end()] + inject + code[m.end() :]


def render_graphviz(code: str, out_dir: Path) -> tuple[bytes | None, str]:
    """把 DOT 代码渲染成 PNG。返回 (图片字节, 错误信息)。

    渲染前会调 ``_ensure_font`` 补中文字体，否则中文全是方框。
    """
    exe = _which("dot")
    if not exe:
        return None, "本机未安装 Graphviz（dot）"
    out_dir.mkdir(parents=True, exist_ok=True)
    code = _ensure_font(code)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "g.dot"
        dst = Path(td) / "g.png"
        src.write_text(code, encoding="utf-8")
        rc, log = _run([exe, "-Tpng", "-Gdpi=130", str(src), "-o", str(dst)], cwd=Path(td))
        if rc != 0 or not dst.exists():
            return None, f"dot 渲染失败: {log.strip()[:600]}"
        return dst.read_bytes(), ""


def render(kind: str, code: str, out_dir: Path) -> tuple[bytes | None, str]:
    if kind == "mermaid":
        return render_mermaid(code, out_dir)
    if kind == "graphviz":
        return render_graphviz(code, out_dir)
    if kind == "tikz":
        return render_tikz(code, out_dir)
    return None, f"不支持的图类型: {kind}"


# --------------------------------------------------------------------------- 库内已有架构图

def find_paper_figures(paper: str, keyword: str = "architecture", limit: int = 6) -> list[dict]:
    """在库里找到某篇论文的架构图。

    这条是"参考真实架构图"而不是"生成架构图"——用户输入一个论文名，
    我们直接从 PDF 里把它的架构图裁出来给他看/用。评委问"这图哪来的"，
    能直接指回原文第几页，比模型画的可信得多。
    """
    root = Path(__file__).resolve().parent.parent
    index_file = root / "data" / "meta" / "structures.json"
    if not index_file.exists():
        return []
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    stem = Path(paper).stem
    # 输入可能是文件名、arXiv 号或论文标题的一部分
    hits = [k for k in index if k == paper or Path(k).stem == stem or stem in k]
    if not hits:
        needle = paper.lower().strip()
        hits = [k for k in index if needle and needle in k.lower()]

    out: list[dict] = []
    for name in hits:
        for i, fig in enumerate(index.get(name, {}).get("figures", [])):
            caption = (fig.get("caption") or "").replace("\n", " ").strip()
            if keyword and keyword.lower() not in caption.lower():
                continue
            out.append(
                {
                    "file": name,
                    "index": i,
                    "page": fig.get("page"),
                    "caption": caption[:200],
                    # 复用已有的裁图接口，保证和阅读页看到的是同一张
                    "image_url": f"/paper/{name}/figure/{i}",
                }
            )
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------- 主流程

SIZE_FILE = "diagram.png"


async def generate_diagram(description: str, kind: str = "mermaid") -> dict[str, Any]:
    """1.6.2 主流程：让模型写代码 → 本地渲染 → 失败重试一次。

    返回里一定带 ``code``（模型最终写的代码），即使渲染失败也给，
    这样用户能自己拿去改，而不是只看到一个报错。
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("描述不能为空")
    if kind not in ("mermaid", "graphviz", "tikz"):
        raise ValueError(f"不支持的图类型: {kind}")

    messages = build_prompt(description, kind)
    raw = await chat(messages, temperature=0.2)
    code = strip_fence(raw)

    out_dir = Path(tempfile.mkdtemp(prefix="dsh-diagram-"))
    png, err = render(kind, code, out_dir)

    retried = False
    if png is None:
        # 只让模型改语法，不允许改结构，避免它"修着修着把图改成别的"
        retried = True
        logger.info("首次渲染失败，回喂错误重试: %s", err[:200])
        fix_msgs = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"这段代码渲染报错了：\n{err[:800]}\n\n"
                    "请只修正语法错误，**不要改变图的语义和结构**，重新输出完整代码。"
                    "仍然只输出代码本身。"
                ),
            },
        ]
        raw2 = await chat(fix_msgs, temperature=0.1)
        code2 = strip_fence(raw2)
        png, err = render(kind, code2, out_dir)
        if png is not None:
            code = code2

    result: dict[str, Any] = {
        "kind": kind,
        "description": description,
        "code": code,
        "retried": retried,
        "ok": png is not None,
        "available": renderer_available(),
    }
    if png is not None:
        # 存到静态目录供前端 <img> 直接取；文件名用内容哈希避免重复
        import hashlib

        digest = hashlib.sha1(png).hexdigest()[:16]
        static = Path(__file__).resolve().parent.parent / "data" / "meta" / "diagrams"
        static.mkdir(parents=True, exist_ok=True)
        target = static / f"{digest}.png"
        if not target.exists():
            target.write_bytes(png)
        result["image_url"] = f"/writing/diagram/{digest}.png"
        result["bytes"] = len(png)
    else:
        result["error"] = err
    return result