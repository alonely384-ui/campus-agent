# 交接文档 · 模块 1.5 论文分析与生成

> 写给接手的新会话。读完这一份就能继续干活，不需要翻历史对话。
> 所有数字均为 2026-10-29 现场实测，来源在每处标注了核对命令。

---

## 0. 一句话说明现在在哪

官方赛题模块 **1.5「论文分析与生成」四子项已全部完成并有前端入口**，改动**尚未提交到 git**。
模块 1.6「写作助手」在做技术调研，尚未动代码。

---

## 1. 官方要求 vs 实现位置

赛题 PDF `科研小助手/科研小助手Agent-AI大模型.pdf` 第 1.5 节原文要求四项：

| # | 官方原文 | 实现函数 | 数据来源 | 前端入口 |
|---|---|---|---|---|
| 1.5.1 | 论文引用关系图谱 | `build_graph()` → `graph_view()` | `data/meta/citation-graph.json` | `web/graph.html` 力导向图 |
| 1.5.2 | 基石节点挖掘 | `cornerstone_scores()` | 同上 | `graph.html` 顶部 Top10 榜 |
| 1.5.3 | 自动综述生成 | `survey()` | 图谱 + `catalog.json` + `structures.json` | `web/analysis.html`「自动综述」页签 |
| 1.5.4 | 前沿探索 | `frontier()` | 图谱 + 同上元数据 | `web/analysis.html`「前沿探索」页签 |

两个前端页面都从 `web/index.html` 顶栏进入（「引用图谱」「论文分析」两个按钮）。

---

## 2. 数据流

```
data/papers/*.pdf  (39 个文件)
      │
      ├─ scripts/build_graph.py  ←── app/graph.py: extract_references()
      │       每篇 PDF 抽出参考文献条目（4234 条）
      │       ↓ 三层身份识别（arXiv 号 > 完整规范化标题 > 近似匹配）
      │       文件 → 逻辑节点：39 个文件归并成 36 个节点（Attention 的 3 个副本并成 1 个）
      │       ↓
      └─→ data/meta/citation-graph.json
             ├─ nodes[]  : 36 个逻辑节点（PageRank / 入度 / 出度 / 副本列表）
             ├─ edges[]  : 189 条引用边
             ├─ reviews[]: 48 条待人工核验（模糊匹配分数在 [0.75, 0.92) 之间，不出边）
             └─ summary  : 统计数字
                    │
                    ↓  app/main.py  GET /graph
             graph_view()  —— 补派生层：betweenness / communities / cornerstone
                    │
                    ├─→ GET /graph           → web/graph.html   （力导向图 + 基石榜 + 待核验）
                    ├─→ GET /graph/survey    → web/analysis.html（自动综述）
                    └─→ GET /graph/frontier  → web/analysis.html（前沿探索）
```

### 实测数字（核对命令见第 9 节）

| 指标 | 数值 |
|---|---|
| 库内 PDF 文件数 | 39 |
| 归并后逻辑节点数 | 36（合并掉 3 个副本） |
| 引用边数 | 189 |
| 被引论文数（入度 > 0） | 31 |
| 引用他人论文数（出度 > 0） | 33 |
| 孤立节点数 | 1 |
| 引用社区数（Louvain） | 3 |
| 参考文献条目总数 | 4234 |
| 其中 arXiv 号匹配 | 89 |
| 其中标题精确匹配 | 85 |
| 其中标题近似匹配 | 19 |
| 未匹配 | 3993 |
| 待人工核验 | 48 |
| 自引（已显式跳过） | 0 |
| 模糊阈值 | accept=0.92 / review=0.75 |

**被引 Top5（PageRank 与之一致的头部）**

| 被引次数 | 论文 |
|---|---|
| 18 | BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding |
| 17 | Language Models are Few-Shot Learners |
| 16 | Attention Is All You Need |
| 14 | Scaling Laws for Neural Language Models |
| 12 | Improving Language Understanding by Generative Pre-Training |

---

## 3. 关键函数（`app/graph.py`，1999 行）

按数据流顺序：

| 函数 | 作用 | 备注 |
|---|---|---|
| `norm_title(text) -> str` | 标题规范化 | 为什么保留空格见第 6.1 节踩坑 |
| `norm_arxiv(text) -> str` | arXiv 号规范化 | 去 `arXiv:` 前缀、空格、版本号 |
| `extract_references(pdf_path, max_pages=90) -> list[ReferenceEntry]` | 从 PDF 抽参考文献条目 | 复用 `app/structure.py` 的 `_lines` / `_is_ref_entry` |
| `build_nodes(rows) -> (list[LogicalNode], list[merge_decisions])` | 文件 → 逻辑节点归并 | 三层身份识别在此实现 |
| `match_references(...)` | 条目 → 节点匹配，产生边 | 低置信度进 `reviews` 不出边 |
| `pagerank(edges, node_ids, damping=0.85, rounds=40)` | 纯手写 PageRank | 不依赖 networkx |
| `build_graph(...)` | 串起全流程，返回落盘结构 | `rows` / `refs_loader` 可注入，测试用假数据 |
| `cornerstone_scores(nodes, edges)` | **1.5.2 基石节点** | 三项加权：入度 45% + PageRank 40% + 桥度 15%，各转分位后加权 |
| `betweenness(nodes, edges)` | 中介中心性 | 纯手写 Brandes |
| `communities(nodes, edges, rounds=50)` | 社区发现 | 纯手写 Louvain |
| `graph_view(data, limit_reviews=60)` | 落盘数据 → 前端形状 | 只读 + 派生，不重算抽取 |
| `node_meta(canonical_id, title, arxiv_id)` | **元数据补层** | 从 `catalog.json` + `structures.json` 补 year / category / abstract |
| `survey(view, focus="")` | **1.5.3 自动综述** | 见下方设计取舍 |
| `frontier(view, limit=8)` | **1.5.4 前沿探索** | 三条线索，每条带 `basis` 依据 |
| `save_graph` / `load_graph` / `cache_path` | 落盘与读取 | 缓存文件 `data/meta/citation-graph.json` |

### 基石节点角色划分

`cornerstone_scores()` 给每个节点一个 `role`：

| role | 判定 | 含义 |
|---|---|---|
| `cornerstone` | 综合分前列 | 基石论文 |
| `hub` | 入度极高 | 高被引枢纽 |
| `bridge` | 桥度极高 | 连接多个社区 |
| `foundation` | PageRank 极高但入度、桥度都低 | 通用基础件（如 Layer Normalization） |
| `fringe` | 综合分靠后 | 库内相对地位靠后 |
| `isolated` | 无任何边 | 孤立 |

`foundation` 这个角色是专门为 Layer Normalization 加的 —— 它的 PageRank 全库第 7，但直接引用只有 6 篇、也不桥接。判成「边缘」会和前端「金边 = PageRank 前 3」自相矛盾，所以单列。

### 自动综述的设计取舍（重要）

**不调用 LLM**，用「模板 + 图谱事实」拼装。原因：

1. 综述里每一句都要能指回具体论文和具体数字（引用数、年份、社区编号）。答辩被问「你这段话依据什么」，能立刻答出来。
2. 不调 LLM 就不会出现幻觉引用 —— 而赛题 1.6 明确点名「确保 References 真实没有幻觉」，说明评审**在意这件事**，1.5 这一层先立住同样的品格是加分的。
3. 断网也能演示。

综述输出五段：领域概览 / 主题聚类 / 发展脉络 / 代表性工作 / 综合地位靠后的工作。

### 前沿探索的三条线索

| 线索 | 判定 | 为什么 |
|---|---|---|
| `recent_cited` 新近且被引 | 最近两年内发表且入度 > 0 | 已被同行接住的新方向 |
| `bridge` 跨社区桥接 | betweenness ≥ 全库前 25% 分位 **且** 角色不是 cornerstone/hub | 在缝两个社区，而不是人人都引的那几篇 |
| `island` 未被接入 | 出度 = 0 且入度 = 0 | 可能方向独立，也可能是样本覆盖不足 |

每条输出都带 `basis` 字段（人类可读的依据）和 `metric`（量化指标），前端原样展示。

---

## 4. 前端

### `web/graph.html`（494 行）

- ECharts 力导向图，本地 `web/vendor/echarts/echarts.min.js`（1,030,855 字节，**不走 CDN**）
- 节点大小 = 被引数，金边 = PageRank 前 3，颜色 = 社区
- 顶部「基石节点 Top 10」榜，点一行右侧出详情
- 底部可展开「待人工核验」区（48 条）
- 点节点可跳 `reader.html?paper=<文件名>&page=<页>`

### `web/analysis.html`（325 行，本轮新建）

- 两个页签：自动综述 / 前沿探索
- 综述页：4 个统计格 + 概览段落 + 发展脉络柱状图（ECharts）+ 聚类/代表工作表
- 顶栏有主题下拉（`focus` 参数），选「基础模型与预训练」后综述从 36 篇收窄到 14 篇
- 前沿页：按三条线索分组，每条一张卡，卡里有依据文字和「打开原文」链接

---

## 5. 接口

| 接口 | 参数 | 返回 |
|---|---|---|
| `GET /graph` | `limit_reviews=60` | summary / nodes / edges / cornerstones / reviews / merge_decisions |
| `GET /graph/survey` | `focus=`（主题名，可选） | stats / overview / sections[] / clusters / timeline / representatives / topics |
| `GET /graph/frontier` | `limit=8` | stats / recent_cut / items[] / legend |

三个接口在缓存不存在时都返回 `{"ready": false, "detail": "图谱缓存还没生成，请先运行 python scripts/build_graph.py"}`，前端据此提示。

---

## 6. 踩过的坑（每条都真踩过，别再踩一次）

### 6.1 标题规范化删掉空格 → 误连不同论文

**试了什么**：`norm_title` 把所有空格删掉再比，`A Survey of X` → `asurveyofx`。
**为什么不行**：`asurveyofllms` 和 `surveyofllms` 只差首字母，模糊匹配会误判成同一篇。
**最后怎么解决**：新增 `_fuzzy_key()`（保留词间空格，NFKC + NFKD，处理软连字符）和 `_title_bag()`，精确层和近似层都改用它。

### 6.2 自引陷阱 → 凭空造边

**试了什么**：条目精确命中源节点自己，在内层被静默丢弃，然后掉进模糊层撞上标题相近的别家论文。
**为什么不行**：结果是一条根本不存在的引用边。
**最后怎么解决**：用 `self_hit`（键相同 **且** 词袋相同）显式跳过，计入 `stats["self_reference"]`（实测 0 次）。

### 6.3 桥接判定失真（本轮新踩的）

**试了什么**：`betweenness > 0` 全算成桥。
**为什么不行**：这张图 36 个节点里 35 个大于 0。BERT / Attention / GPT-3 这类被引 16~18 次的节点中介性同样很高 —— 它们是「人人都要经过的路」，不是因为它们在缝社区。结果真正在缝社区的 Llama 3 / GPT-4 被埋掉。
**最后怎么解决**：取前 25% 分位线以上，**且**排除 `cornerstone` / `hub` 角色。桥接条数从 35 降到 2。

### 6.4 `_first_sentence` 句尾多一个空格

**试了什么**：`re.search(r"(?<=[.!?])\s", t)` 然后用 `t[:m.start()+1]`。
**为什么不行**：lookbehind 让 `m.start()` 指向空格而不是标点，切出来尾部带空格。
**最后怎么解决**：改成 `re.search(r"[.!?]\s", t)`，`m.start()` 就指向标点本身。

### 6.5 不归并副本会让 PageRank 失真

**这是设计动机，不是 bug**。库里 Attention 有三份（`1706.03762v7.pdf` / `1706.03762v1.pdf` / `bdfaa68d8984.pdf`，前者后者标题相同、中间那份文件名根本不是 arXiv 号）。
按文件建节点的话，同一篇论文被拆成 3 个点，「被引用 N 次」也被拆成三份分别算。所以必须先在逻辑层归并，**但不删文件**。

### 6.6 `chart.getSize()` 不存在

ECharts 的实例方法里没有 `getSize()`，正确的是 `getWidth()` / `getHeight()`。这个错误让布局自适应静默失败了 4 轮排查。

### 6.7 `coordinateSystem` 的字段是 `transform` 不是 `_transform`

取 `_transform` 会回退成单位矩阵，**看起来正常但实际没生效**。

### 6.8 `scripts/` 下的脚本必须自己加 `sys.path`

直接 `python scripts/xxx.py` 会 `ModuleNotFoundError: No module named 'app'`。脚本开头要
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`。

---

## 7. 已知缺口（交接时务必知道）

### 7.1 四篇论文缺年份

`catalog.json` 里共 **5 条** `year: None`（实测 `[r for r in catalog if not r.get("year")]` 长度为 5）：BERT / GPT-1 / LLaMA / Understanding Deep Learning / Attention 副本。前四条是非 arXiv 来源（ACL Anthology、OpenReview、书籍），没有 arXiv 号可推；最后一条是 Attention 的重复文件，归并后不影响逻辑节点。**故实际影响 4 个逻辑节点**。

**当前处理**：综述里如实写明「4 篇缺少发表年份，按年份未知处理，未被丢弃」，不悄悄跳过。
**如果要补**：arXiv API 在当前环境可达（实测 `ti:"BERT Pre-training"` 返回 7431 字节正常 XML），可以按标题检索补年份。但这属于改数据，需要单独确认。

### 7.2 两个目录没被 gitignore

`data/meta/figures/` 和 `data/meta/translations/` 是运行期产物，但 `.gitignore` 没覆盖。
`git add -A` 会把它们带进仓库。提交时要按显式路径加。

### 7.3 改动全部未提交

见第 8 节。

---

## 8. 未提交改动清单

```
 M .gitignore                                       (本会话早期改的)
 M app/main.py                                      (+3 个 graph 端点，import 增加 frontier/survey)
 M web/index.html                                   (+「论文分析」入口)
 M data/meta/online/recommendations_latest.json     (运行期产物变化)
 M data/meta/recommendations.json                   (运行期产物变化)
?? app/graph.py                                     (1999 行，1.5 全部后端逻辑)
?? web/graph.html                                   (494 行，引用图谱页)
?? web/analysis.html                                (325 行，综述+前沿页)
?? web/vendor/echarts/                              (ECharts 5.5.1 本地副本，1MB)
?? tests/test_citation_graph.py                     (575 行，34 个用例)
?? scripts/build_graph.py                           (49 行，构建落盘)
?? scripts/probe_citation_graph.py                  (早期探路脚本)
?? docs/citation-graph-frontend-research.md         (前端方案调研)
?? docs/HANDOFF-1.5.md                              (本文档)
?? data/meta/citation-graph.json                    (图谱数据 83,405 字节)
?? data/meta/figures/                               (⚠ 运行期产物，不该入库)
?? data/meta/translations/2412.15115v2.json         (⚠ 运行期产物，不该入库)
```

---

## 9. 如何核对本文档的数字

```powershell
cd D:\alonely\deepseek\campus-agent
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path

# 图谱统计
& $py -c "import sys;sys.path.insert(0,'.');from app import graph;s=graph.load_graph()['summary'];print(s)"

# 全量测试（基线 269 passed）
& $py -m pytest -q

# lint
& .\.venv\Scripts\ruff.exe check .

# 重建图谱（会改写 data/meta/citation-graph.json）
& $py scripts\build_graph.py

# 起服务
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**当前基线（2026-10-29 实测）**：
```
pytest -q      → 269 passed in 32.15s
ruff check .   → All checks passed!
```

---

## 10. 如果下一步要动 1.5，可能的改进点

按性价比排序，都不是必需的：

1. **补回 3993 条未匹配参考文献**：目前只抽出了标题的 241 条能匹配。未匹配的大多是分栏切割导致标题被截断的。可以接 arXiv API 批量反查（`_fetch_arxiv_titles` 已有骨架）。
2. **48 条待核验人工过一遍**：都在 `reviews[]` 里，每条带 `match_score`，可以按分数排序人工确认后转正。
3. **综述接 LLM 润色**：当前模板化文本偏「报表感」。可以让 LLM 在**不新增事实**的前提下把同样的数字重写成更通顺的段落 —— 但必须把「不许出现原文没有的数字和论文名」写进 prompt 并在输出后做校验。
4. **补 4 篇缺失年份**：见 7.1。