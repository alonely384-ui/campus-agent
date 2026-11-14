"""论文库的主题分组与标题聚合（只读）。

设计要点：
1. 分组来自引用图谱已有的引用关系簇划分（`app/graph.py` 算好写进图的 JSON），
   这里只做「文件名 → 图节点 → community」的映射，不重新聚类。
   ⚠ 它只反映引用网络结构，**不是研究领域分类**；研究领域请用 `app/topics.py`。
   实测本库的簇划分不通过随机基线检验（当前 38 节点 / 210 边：
   Q=0.079184，200 次随机图均值 0.132341，实际只压过 2/200，p≈0.99，不显著），
   所以界面上叫「引用关系簇」，不叫研究子领域。
2. 标题优先取论文解析结果 `data/meta/structures.json` 里的 title 字段，
   不调大模型、不花额度；解析失败的论文退回文件名。
3. 「短标题」策略：标题本身不超过 `TITLE_MAX_CHARS` 就直接用；
   超长时截断并标 `truncated=True`，前端悬停显示完整标题。
   注意这里**不生成摘要**——摘要要靠大模型，本阶段明确不调。
4. 映射不到的论文统一归入 `OTHER_TOPIC_ID`，保证任何一篇都不会在列表里消失。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.papers import DATA_DIR

logger = logging.getLogger(__name__)

# 数据目录按代码位置算，不按进程当前目录算。
# 原来的 Path("data/meta/...") 是从 CWD 解析的：换一个目录启动就会读到空数据，
# 论文全部掉进「其他」分组，而日志里只有一句「文件不存在」。
_META_DIR = DATA_DIR / "meta"

# 标题超过这个长度就截断（字符数，按 unicode 码位算）
TITLE_MAX_CHARS = 60
# 截断时保留的字符数，留一点余量给省略号
TITLE_KEEP_CHARS = 56

# 论文库每页条数。放在后端是为了让 /papers/topics 把它一起发下去，
# 前端不用再写一份同样的魔法数字。
PAGE_SIZE = 12

# 兜底分组：没能映射到图节点的论文进这里
OTHER_TOPIC_ID = -1
OTHER_TOPIC_NAME = "其他"

# 引用关系簇编号 → 可读名。图谱只给出数字簇号，名字是按各簇被引最多的论文人工归纳的：
#   0 号：Attention / BERT / GPT / LLaMA / Scaling Law 一路，偏基础模型与架构
#   1 号：Codex / InstructGPT / Llama2 / PPO / DPO / DeepSeek-R1，偏对齐与落地
#   2 号：Understanding Deep Learning，单独一本教材
# ⚠ 这两个名字是**人工归纳的展示标签**，不是算法输出，也不代表簇就是研究领域。
#   簇划分本身不通过随机基线检验，界面上已标注为「引用关系簇」。
# 名字取不出来就退回「簇 N」，不会因为名字而丢数据。
COMMUNITY_NAMES: dict[int, str] = {
    0: "基础模型与架构",
    1: "对齐与推理落地",
    2: "深度学习方法论",
}


@dataclass
class PaperTopic:
    """一篇论文的主题归属与展示用标题。"""

    filename: str
    title: str
    """完整标题（解析失败时退回文件名）。"""
    display_title: str
    """截断后的展示标题。"""
    truncated: bool
    """display_title 是否被截断，前端据此决定要不要挂 title 属性显示全名。"""
    topic_id: int
    topic_name: str
    community: int | None = None
    """图谱社区号；映射不到时为 None。"""
    node_id: str = ""
    cited_by: int = 0


@dataclass
class TopicGroup:
    """一个主题分组。"""

    topic_id: int
    name: str
    papers: list[PaperTopic] = field(default_factory=list)


def _structures_path() -> Path:
    return _META_DIR / "structures.json"


def _graph_path() -> Path:
    # 注意：文件名是 citation-graph.json，不是 graph.json（实测确认过）
    return _META_DIR / "citation-graph.json"


def _catalog_path() -> Path:
    """上传索引。它比 structures.json 多两篇：PDF 解析失败、没进解析结果的那些。"""
    return DATA_DIR / "catalog.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        logger.warning("主题分组：%s 不存在，相关论文会归入「%s」", path, OTHER_TOPIC_NAME)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 不吞异常：数据坏了要让调用方和用户都看得出来，但也不能整个论文库打不开
        logger.warning("主题分组：读取 %s 失败（%s），相关论文会归入「%s」", path, exc, OTHER_TOPIC_NAME)
        return {}


def load_titles() -> dict[str, str]:
    """文件名 → 真实标题。解析结果优先，解析失败的用上传索引里的标题补。

    为什么要补这一层：`structures.json` 只收「PDF 解析成功」的论文，实测 40 条
    catalog 里有 2 条（`2312.00752.pdf` Mamba、`1610.08613.pdf` Active Memory）
    因为 PDF 文本层为空而没有解析结果。只读 structures.json 时它们的标题会退化成
    「未解析出标题（文件名）」，可 catalog.json 里明明存着完整标题——
    论文是用户上传时带进来的元数据，不该因为解析失败就丢掉。

    优先级：structures.json（解析出的、最权威）> catalog.json（上传时的元数据）。
    两边都没有才让调用方退回文件名。
    """
    titles: dict[str, str] = {}

    # 第一层：解析结果
    raw = _load_json(_structures_path())
    if isinstance(raw, dict):
        for filename, item in raw.items():
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if title:
                titles[filename] = title

    # 第二层：上传索引兜底（只补第一层没拿到的，不覆盖）
    catalog = _load_json(_catalog_path())
    if isinstance(catalog, list):
        filled = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("file") or "").strip()
            if not filename or filename in titles:
                continue
            title = str(item.get("title") or "").strip()
            if title:
                titles[filename] = title
                filled.append(filename)
        if filled:
            logger.info("标题：%d 篇从上传索引补齐（%s）", len(filled), ", ".join(sorted(filled)))

    return titles


def load_file_communities() -> dict[str, tuple[int, str, int]]:
    """文件名 → (社区号, 节点 id, 被引次数)。

    社区号不在 citation-graph.json 里——文件只存原始节点和边，社区是
    `graph.py: graph_view()` 每次调用时用 Louvain 现算的。这里直接复用同一个
    `communities()` 函数，保证论文库看到的分组和引用图谱页完全一致
    （它有固定遍历顺序，同样输入每次结果相同，不是随机聚类）。

    一个文件名可能出现在多个节点里（图的合并逻辑会保留多副本），
    这里取被引最多的那个节点，保证结果稳定。
    """
    raw = _load_json(_graph_path())
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, list):
        return {}
    edges = raw.get("edges") or []

    try:
        # 延迟导入：graph.py 依赖较重，只在真正要用主题分组时加载
        from app.graph import communities
    except ImportError as exc:  # pragma: no cover - 正常装完依赖不会走到
        logger.warning("主题分组：导入 app.graph 失败（%s），所有论文归入「%s」", exc, OTHER_TOPIC_NAME)
        return {}

    comm = communities(nodes, edges)

    best: dict[str, tuple[int, str, int]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("canonical_id") or node.get("id") or "")
        cited_by = int(node.get("cited_by_count") or node.get("cited_by") or 0)
        community = comm.get(node_id, OTHER_TOPIC_ID)
        for filename in node.get("copies") or []:
            filename = str(filename or "")
            if not filename:
                continue
            current = best.get(filename)
            if current is None or cited_by > current[2]:
                best[filename] = (community, node_id, cited_by)
    return best


def topic_name(community: int) -> str:
    return COMMUNITY_NAMES.get(community) or (OTHER_TOPIC_NAME if community == OTHER_TOPIC_ID else f"主题 {community}")


def short_title(title: str, limit: int = TITLE_MAX_CHARS, keep: int = TITLE_KEEP_CHARS) -> tuple[str, bool]:
    """返回 (展示标题, 是否截断)。

    按码位截断，不切坏多字节字符；在最后一个空格处断开，避免把单词拦腰截断。
    """
    title = title.strip()
    if len(title) <= limit:
        return title, False
    head = title[:keep]
    cut = head.rfind(" ")
    # 找不到空格（或空格太靠前）就直接硬截，否则视觉上会短一大截
    if cut >= keep // 2:
        head = head[:cut]
    return head.rstrip(" ,;:-") + "…", True


def build_topics(filenames: list[str]) -> list[TopicGroup]:
    """把文件名列表整理成按主题分组的结果。

    排序规则：主题按论文数从多到少（「其他」永远排最后），
    主题内按被引次数从多到少，被引相同再按文件名，保证每次刷新顺序一致。

    图谱数据缺失或损坏时不抛异常：所有论文归入「其他」一组，
    保证论文库至少能正常列出来，不会因为图谱坏了就打不开。
    """
    titles = load_titles()
    communities = load_file_communities()

    groups: dict[int, TopicGroup] = {}
    for filename in filenames:
        community, node_id, cited_by = communities.get(filename, (OTHER_TOPIC_ID, "", 0))
        # 解析不出标题的（实测 39 篇里有 1 篇图片版）退回文件名，
        # 但不把 `1706.03762v1_img.pdf` 这种直接怼给用户——加前缀说明它是解析失败的
        title = titles.get(filename) or ""
        if not title:
            title = f"未解析出标题（{filename}）"
        display, truncated = short_title(title)
        paper = PaperTopic(
            filename=filename,
            title=title,
            display_title=display,
            truncated=truncated,
            topic_id=community,
            topic_name=topic_name(community),
            community=None if community == OTHER_TOPIC_ID else community,
            node_id=node_id,
            cited_by=cited_by,
        )
        groups.setdefault(community, TopicGroup(topic_id=community, name=topic_name(community))).papers.append(paper)

    ordered = sorted(
        groups.values(),
        key=lambda g: (g.topic_id == OTHER_TOPIC_ID, -len(g.papers), g.topic_id),
    )
    for group in ordered:
        group.papers.sort(key=lambda p: (-p.cited_by, p.filename))
    return ordered