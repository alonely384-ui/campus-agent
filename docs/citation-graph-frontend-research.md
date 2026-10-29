# 引用图谱前端调研：别人怎么做，我们做什么

> 目的：在动手写前端之前，先把同类产品的真实功能和算法口径摸清楚，避免自己拍脑袋设计。
> 本文只做调研与方案建议，不含代码改动。
> 赛题对应：1.5.1 论文引用关系图谱 / 1.5.2 基石节点 / 1.5.3 自动综述 / 1.5.4 前沿探索。
> 生成时间：2026-10-30。所有产品事实均来自公开页面（见文末参考链接）。

---

## 一、四个主流产品在做什么

| 产品 | 核心交互 | 可视化形态 | 算法口径 | 我们能否复刻 |
|---|---|---|---|---|
| **Connected Papers** | 输入一篇"种子论文"，一键生成关联图 | 力导向图；**节点大小=被引数**、颜色=发表年份；位置由相似度决定 | 引用关系 + **语义相似度**双信号，能挖出「没直接引用但概念相近」的论文 | 语义相似度部分可复用已有向量库；但需要全库 embedding 距离，成本中 |
| **Litmaps** | 种子论文 → 时间轴上的引文地图 | **时间轴布局**（横轴=年份），点可展开 | 纯引文关系，270M+ 论文库 | 时间轴布局极易复刻，**强烈建议做** |
| **Research Rabbit** | 种子论文 → Similar / Earlier / Later 三类推荐 | 力导向图 + 作者共现网络 + 时间轴 | 引文关系 + 语义 + 作者共现 | Earlier/Later 复刻简单（就是出边/入边），作者网络不做 |
| **Connected Papers 的 Prior/Derivative Works** | 图旁边列两个清单 | 列表 | 前向/后向引用追一层 | **最容易，直接砍两端就能做** |

### 三个可直接抄的设计决策

1. **"基石节点"不是"被引最多的"** —— 四个产品都区分了「被引次数多」和「结构位置重要」。Litmaps 用时间密度，Connected Papers 用中心性，CiteSpace 用突现度。我们已有 PageRank 和入度，缺的是**桥接度和社区**。
2. **孤立节点要单独成组，不能混在力导向图里** —— 否则用户以为"图渲染坏了"。Connected Papers 直接把无连接的点降级到侧栏。
3. **每个节点必须能点开跳原文** —— 所有产品都做到了。我们现在节点只有后端 JSON，**一个跳转都没有**，这是最大的体验缺口。

---

## 二、学术界的算法口径（决定 1.5.2 怎么算"基石"）

| 指标 | 出处 | 物理含义 | 本库现状 |
|---|---|---|---|
| **PageRank** | Brin & Page 1998 | 被"重要论文"引用，价值更高；能挖出非直接连接的间接影响力 | ✅ 已实现（`app/graph.py:1155 pagerank`，纯 Python 幂迭代 40 轮） |
| **入度 / 出度** | 引文分析基础 | 入度高=基石；出度高=综述/综合性论文 | ✅ 已在节点字段 `cited_by_count` / `citation_edge_count` |
| **中介中心性 betweenness** | Freeman 1977 | 连接两个簇的"桥论文"，跨子领域必读 | ❌ 未实现 |
| **Louvain 社区发现** | Blondel et al. 2008 | 划分子领域，社区间稀疏区=研究空白 | ❌ 未实现 |
| **突现检测 burst detection** | Kleinberg 2002，CiteSpace 采用 | 某篇/某词在短时间窗口内引用激增=新兴前沿 | ❌ 未实现（需外部引用时序数据，我们拿不到） |
| **共被引 / 文献耦合** | Small 1973 / Kessler 1963 | 两篇论文被同一批论文引用 → 主题相近 | ❌ 未实现（可作为语义相似度的廉价替代） |

### 关键事实：本库的 PageRank 有一个语义坑

实测数据（`data/meta/citation-graph.json`，36 节点 / 189 边）：

```
0.1737  被引= 6  Layer Normalization
0.1467  被引=16  Attention Is All You Need
0.0788  被引=18  BERT: Pre-training of Deep Bidirectional Transformers
```

**Layer Normalization 只有 6 条入边，PageRank 却是第一。** 原因不是算法错，而是 PageRank 衡量的是"被影响力大的论文引用"：它的 6 个引用者里有 3 个排进全库前 6（Attention、BERT、GPT）。而 BERT 的 18 个引用者里有大量排名 20 开外的论文，均摊下来反而低。

这个结果是**正确的学术含义**，但用户看到"被引 6 次的排在被引 18 次的前面"会直觉认为算错了。产品上必须给出解释文案，否则就是 bug 观感。

---

## 三、我们现有的家底（实测，非推测）

`GET /graph` 尚未实现，前端零入口。当前数据结构（`data/meta/citation-graph.json`）：

```
summary:  file_count 39 / node_count 36 / edge_count 189
          cited_paper_count 31 / citing_paper_count 33 / isolated_count 1 / merged_away 3
          refs: entries_total 4234, matched_arxiv 89, matched_exact 85, matched_fuzzy 19,
                unmatched 3993, review 48, self_reference 0
nodes[]:  canonical_id, title, arxiv_id, copies[], copy_count, citation_edge_count,
          cited_by_count, pagerank, isolated, match_methods{}
edges[]:  source, target, match_method, match_score, reference_title, reference_arxiv_id
isolated: 1 个（UnderstandingDeepLearning_02_09_26_C.pdf）
reviews:  48 条待人工核验（如「Language models are unsupervised multitask learners」
          被误配到 Instruction Pre-Training，score 0.784 → 这是噪声，需在 UI 里标灰）
```

依赖现状：
- `networkx 3.7` 已安装，但**未写入 `requirements.txt`** → 部署环境会缺。
- **`scipy` 未安装** → `nx.pagerank()` 会报 `ModuleNotFoundError`，现有 `app/graph.py` 是自写幂迭代，属于正确规避。
- `nx.algorithms.community.louvain_communities` 可用（实测 `hasattr` 为 True）。
- 前端 `web/` 目录**没有任何图表库**，需要引入 ECharts（或自绘 canvas）。

---

## 四、建议方案（分三档，按性价比排序）

### 阶段一：最小可用（1–1.5 天，零 LLM 成本）

| 做的事 | 依据 |
|---|---|
| 新增 `GET /graph` 读缓存 JSON | 数据已就绪，无计算 |
| `web/index.html` 新增「引用图谱」入口 → 新页 `web/graph.html` | 与现有阅读页解耦 |
| ECharts 力导向图：节点大小=入度，颜色深浅=PageRank，深色边框=有副本的节点 | 复刻 Connected Papers 视觉惯例 |
| 节点点击 → 弹出侧栏（标题/被引/引用/PageRank/文件名/匹配方式）→ 「打开阅读页」按钮跳 `reader.html` | 补最大体验缺口 |
| 孤立节点单独列在右侧"未与库内任何论文关联"分组 | 避免被当成渲染 bug |
| 待核验 48 条折叠区，标"低置信度，未计入图谱" | 诚实展示，加分项 |
| 图例 + 一段说明："PageRank 高 = 被有影响力的论文引用，不等于被引次数最多" | 消除第二节的语义坑 |

**验收**：本地起服务打开页面，能看见 36 个节点、189 条边，点击任意节点能跳到对应阅读页。

### 阶段二：基石节点强化（+1 天）

- 补 **betweenness 中心性**（networkx 内置，36 节点瞬时完成，实测非零节点 35/36）。
- 补 **Louvain 社区**（实测可分 5 个社区，规模 12/10/7/6/1），按社区上色。
- 「基石节点 Top5」榜单：综合排序 = PageRank 0.5 + 入度归一化 0.3 + betweenness 归一化 0.2，每项都显示原始值，不只给一个黑盒分。

### 阶段三：前沿探索最小版（+1 天，可选）

- **共被引矩阵**：两篇论文被同一批论文引用 → 主题相近。这个不需要 LLM，用现有 edges 就能算，可替代 Connected Papers 的"语义相似度"。
- 「研究空白」提示：两个社区之间零边的组合，列出候选（置信度低，文案要好）。

### 明确不做（理由）

| 不做 | 理由 |
|---|---|
| 突现检测 / burst | 需要外部引用时序数据，本库拿不到 |
| 作者共现网络 | 解析作者字段的投入产出比太差 |
| 全库语义相似度图 | 需要现算 39 篇的 embedding 距离，与现有 RAG 索引耦合，风险高 |
| 自动综述 / Future Work 抽取（1.5.3） | 这属于另一条链路，本轮不碰 |

---

## 五、风险与坑（已实测）

1. **networkx 不在 `requirements.txt`** —— 阶段二用 louvain 前必须补进去，否则线上崩。
2. **scipy 缺失** —— 不能用 `nx.pagerank()`，继续用自写版本；若后续要 betweenness，`nx.betweenness_centrality` 是纯 Python，可用（已实测跑通）。
3. **PowerShell 输出中文乱码** —— `ConvertTo-Json` 直接打印会显示 `鏂板缓`。用 Python 读 JSON 验证，实测 action 取值是正常的 `新建节点 / 并入`，**数据文件本身没问题**。
4. **48 条待核验里有明显噪声**（`Language models are unsupervised multitask learners` → Instruction Pre-Training，score 0.784）。这些不进图，但要在 UI 里可见，体现"我们知道自己不知道"。
5. **孤立节点只有 1 个**（`UnderstandingDeepLearning_02_09_26_C.pdf`），不是 15 个了——之前探路的 15 篇是针对旧口径，现在 arXiv/标题匹配补上了大部分。

---

## 参考链接

- [Connected Papers 功能说明（Texas A&M 图书馆指南）](https://tamu.libguides.com/c.php?g=1539840&p=11582546)
- [Litmaps 功能说明（同上）](https://tamu.libguides.com/c.php?g=1539840&p=11582548)
- [Research Rabbit 功能说明（同上）](https://tamu.libguides.com/c.php?g=1539840&p=11582549)
- [引文网络与结构洞察（Skill.re 课程 Lesson 1.3）](https://skill.re/course/skill-re/researcher-l2/13-discovering-connected-research-through-citation-networks/)
- [CiteSpace II: Detecting and visualizing emerging trends（Chen 2006）](https://scholar.google.co.ve/scholar_lookup?title=CiteSpace+II:+Detecting+and+visualizing+emerging+trends+and+transient+patterns+in+scientific+literature&author=Chaomei+Chen&year=2006&doi=10.1002/asi.20317)