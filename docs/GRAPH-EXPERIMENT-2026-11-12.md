# 分类口径补充与图谱专项只读实验报告

> 对应指令：项目负责人验收结论与下一阶段指令（2026-11-12）
> 性质：**只读实验报告**。本轮不修改任何图谱算法、不提交、不部署。
> 工作区：分支 `main`，HEAD `bd94684`（= origin/main）。所有未提交改动保持原样，未做 reset / stash / clean / checkout 覆盖。
> 复现脚本：`scripts/_r_c0.py`、`scripts/_r_sub.py`、`scripts/_p0_graph.py`、`scripts/_p0_align.py`、`scripts/_p0_merge.py`、`scripts/_p1_weights.py`、`scripts/_p2_p3.py`、`scripts/_p2_detail.py`、`scripts/_p3_review.py`（**运行完即删除，不入库**）。

---

## 第一部分：两个统计口径说明

### 1.1 副本去重：原报告错了，此处直接修正

**原报告写的「24 条记录 = 21 个主版本 + 3 个副本，去重后 23 篇」中，「去重后 23 篇」是错的。**

我混用了两个口径：`23` 是**唯一文件名数**，不是**去重后的论文数**。正确结论如下（`scripts/_r_c0.py` 实测输出）：

```
记录数 = 24
唯一 filename = 23
唯一去重ID = 21
主版本条数 = 21
副本条数 = 3
```

**为什么是 21 而不是 23**：社区 0 里有 4 条记录属于同一条逻辑论文，去重后合成 1 条，共减少 3 条：

| 论文家族 | 社区 0 内记录数 | 去重后 | 减少 |
|---|---|---|---|
| `1706.03762v7`（Attention Is All You Need） | 4 | 1 | −3 |
| 其余 20 个 paper_id | 20 | 20 | 0 |
| **合计** | **24** | **21** | **−3** |

24 条唯一文件名是 23，是因为 `1706.03762v7.pdf` 这个**文件名本身出现了两次**（详见 1.3 的缺陷），所以「24 条记录 − 1 条同文件名 = 23 个文件名」，但按逻辑论文归并后只剩 21。

### 1.2 社区 0 的 24 条记录完整明细

| # | 文件名 | paper_id | 对应主版本（duplicate_of） | 是否重复论文 | 去重后论文 ID |
|---|---|---|---|---|---|
| 1 | `1607.06450v1.pdf` | `1607.06450v1` | 无 | 否 | `1607.06450v1` |
| 2 | `1706.03762v1.pdf` | `1706.03762v1` | `1706.03762v7` | 是 | `1706.03762v7` |
| 3 | `1706.03762v1_img.pdf` | `1706.03762v1_img` | `1706.03762v7` | 是 | `1706.03762v7` |
| 4 | `1706.03762v7.pdf` | `1706.03762v7` | 无 | 否（主版本） | `1706.03762v7` |
| 5 | `1706.03762v7.pdf` | `bdfaa68d8984` | `1706.03762v7` | 是 | `1706.03762v7` |
| 6 | `1910.10683v4.pdf` | `1910.10683v4` | 无 | 否 | `1910.10683v4` |
| 7 | `2001.08361v1.pdf` | `2001.08361v1` | 无 | 否 | `2001.08361v1` |
| 8 | `2002.04745v2.pdf` | `2002.04745v2` | 无 | 否 | `2002.04745v2` |
| 9 | `2005.14165v4.pdf` | `2005.14165v4` | 无 | 否 | `2005.14165v4` |
| 10 | `2010.11929v2.pdf` | `2010.11929v2` | 无 | 否 | `2010.11929v2` |
| 11 | `2101.03961v3.pdf` | `2101.03961v3` | 无 | 否 | `2101.03961v3` |
| 12 | `2106.09685v2.pdf` | `2106.09685v2` | 无 | 否 | `2106.09685v2` |
| 13 | `2201.11903v6.pdf` | `2201.11903v6` | 无 | 否 | `2201.11903v6` |
| 14 | `2205.14135v2.pdf` | `2205.14135v2` | 无 | 否 | `2205.14135v2` |
| 15 | `2206.07682v2.pdf` | `2206.07682v2` | 无 | 否 | `2206.07682v2` |
| 16 | `2210.02414v2.pdf` | `2210.02414v2` | 无 | 否 | `2210.02414v2` |
| 17 | `2211.05100v4.pdf` | `2211.05100v4` | 无 | 否 | `2211.05100v4` |
| 18 | `2303.08774v6.pdf` | `2303.08774v6` | 无 | 否 | `2303.08774v6` |
| 19 | `2305.13048v2.pdf` | `2305.13048v2` | 无 | 否 | `2305.13048v2` |
| 20 | `2305.14314v1.pdf` | `2305.14314v1` | 无 | 否 | `2305.14314v1` |
| 21 | `2406.14491v2.pdf` | `2406.14491v2` | 无 | 否 | `2406.14491v2` |
| 22 | `333078981_693988129081760_4712707815225756708_n.pdf` | 同名 | 无 | 否 | 同名 |
| 23 | `N19-1423.pdf` | `N19-1423` | 无 | 否 | `N19-1423` |
| 24 | `language_understanding_paper.pdf` | 同名 | 无 | 否 | 同名 |

去重 ID 分布：`1706.03762v7` 占 4 条，其余 20 个 ID 各 1 条 → **21 个唯一逻辑论文**。

### 1.3 这份明细暴露的一个真实数据缺陷（本轮只记录，不修）

`1706.03762v7.pdf` 这个文件名在社区 0 内出现了**两次**，对应两个不同的 `paper_id`：

- `1706.03762v7`（主版本，无 `duplicate_of`）
- `bdfaa68d8984`（`duplicate_of = 1706.03762v7`，但**文件名也是 `1706.03762v7.pdf`**）

后果体现在图谱数据结构里（`scripts/_p0_graph.py` 实测）：节点 `arxiv:1706.03762` 的 `copies` 列表是

```
['1706.03762v7.pdf', '1706.03762v1.pdf', '1706.03762v7.pdf', '1706.03762v1_img.pdf']
```

即 `copy_count = 4`，但**唯一文件名只有 3 个**。由此 `summary.file_count = 39` 实际唯一文件只有 38，`summary.merged_away = 3` 实际真正被合并的只有 2 条（`1706.03762v1`、`1706.03762v1_img`）——第三条 `bdfaa68d8984` 合并进了一个和它**同名**的节点，属自我重复。

该缺陷**不影响 Louvain 计算**（节点按 `canonical_id` 聚合，边按节点计），只影响计数类字段。

### 1.4 组合筛选：7 与 6 分别是什么口径

复现脚本 `scripts/_r_sub.py`，实测输出：

```
来源 A /papers/classify：领域 对齐与推理 count=7（人类反馈对齐4 / 推理增强2 / 指令微调1）
来源 B /papers 现算：对齐与推理 记录=7 唯一file=7（同上分布）
来源 C 社区1+对齐：记录=6 唯一file=6（人类反馈对齐4 / 指令微调1 / 推理增强1）
差集：2201.11903v6.pdf  topic_id=0 topic=基础模型与架构 sub=推理增强
社区1 记录=14 唯一file=14，primary_topic 分布={'对齐与推理':6,'训练效率与微调':1,'多模态模型':2,'能力评测与规模':1,'基础模型与预训练':4}
```

| 数字 | 口径 | 说明 |
|---|---|---|
| **7** | 不叠加任何主题筛选，全部 40 篇里 `primary_topic == "对齐与推理"` 的论文数 | 记录数 = 7，唯一文件名 = 7，**无副本** |
| **6** | 在 7 的基础之上，再叠加「引用关系簇 = 1」的筛选 | 社区 1 共 14 篇，其中 6 篇属对齐与推理 |

**两者之差不是副本、不是分页**，而是**筛选范围差异**：被排除的 1 篇是

- `2201.11903v6.pdf`（Chain-of-Thought Prompting Elicits Reasoning in Large Language Models）
- 分类：`对齐与推理 / 推理增强`
- 图谱社区：`topic_id = 0`（基础模型与架构）

也就是说：**「7」是分类全量，「6」是「分类 ∩ 社区」的交集**。分类与社区是两个独立口径（NMI 仅 0.2543，见 3.5），本来就不该相等。

> 按指令「完成这两项后停止修改分类筛选功能」，本轮未对分类与筛选条做任何改动。

---

## 第二部分：图谱数据口径（P0）

复现：`scripts/_p0_graph.py`、`_p0_align.py`、`_p0_merge.py`。数据源 `data/meta/citation-graph.json`。

### 2.1 节点与副本

```
节点 = 36，唯一 canonical_id = 36，ID 重复 = 0
copies 条目 = 39   唯一文件名 = 38   copy_count 总和 = 39
合并多副本的节点 = 1：arxiv:1706.03762 'Attention Is All You Need'
catalog 文件 = 40   图谱 copies 覆盖 = 38
   catalog 有但图谱无: ['1610.08613.pdf', '2312.00752.pdf']
   图谱有但 catalog 无: []
```

**节点是否按逻辑论文去重：是。** `canonical_id` 唯一，36 个节点对应 36 条逻辑论文。副本通过 `merge_decisions` 归并，`action` 分布 `{'新建节点': 36, '并入': 3}`。

**副本是否被合并：合并了，但计数虚高 1。** 唯一多副本节点是 `arxiv:1706.03762`，`copies` 里 `1706.03762v7.pdf` 重复出现两次（1.3 已说明）。

**两篇出现在 catalog 但不在图谱里**：`1610.08613.pdf`（Can Active Memory Replace Attention?）、`2312.00752.pdf`（Mamba），二者 `indexed = true` 但 `structures.json` 里没有条目——与上一轮 Mamba 标题缺失是同一个根因，PDF 解析失败。

**索引记录侧**（`data/papers/index.json`）：41 条记录，40 个唯一文件名，3 条带 `duplicate_of`，全部指向 `1706.03762v7`：

```
bdfaa68d8984     -> 1706.03762v7   （文件名也叫 1706.03762v7.pdf）
1706.03762v1     -> 1706.03762v7
1706.03762v1_img -> 1706.03762v7
```

`merge_decisions` 共 39 条，唯一文件名只有 38 个；`1706.03762v7.pdf` 出现两次，一次是「新建节点」，一次是「并入 · 同一 arXiv 编号」。

### 2.2 边

```
边 = 189，唯一 (source,target) 对 = 189，重复对 = 0
互为反向对 = 4，自环 = 0，端点不在 nodes 的边 = 0
match_method 分布 = {'arxiv': 88, 'title_exact': 83, 'title_fuzzy': 18}
所有边的键组合只有 1 种：['match_method','match_score','reference_arxiv_id','reference_title','source','target']
缺 reference_title 的边 = 0
match_score 分布：=1.0:171 / 0.99~1.0:7 / 0.97~0.99:4 / 0.92~0.97:7
```

**引用边是否重复：没有。** 189 条边对 189 个唯一有序对，自环 0。有 4 对互为反向（A 引 B 且 B 引 A），这是真实的相互引用，不是重复边。

**引用方向是否正确：正确。** `source` = 引用方，`target` = 被引用方。可用 PageRank 佐证：`Layer Normalization` 出度 0（它是最早期工作，库内无可引用对象）、入度 6（被 6 篇引用），方向与直觉一致。

**边类型与匹配来源是否完整保留：保留。** 三种 `match_method` 齐全，`reference_title` 189 条无缺失，`reference_arxiv_id` 与 `match_score` 均在。

### 2.3 孤立节点

```
度数0 的 = 1 个（hash:8b8147c4ec17 'Understanding Deep Learning'）
节点属性 isolated=True 的 = 1 个
summary.isolated 列表 = ['hash:8b8147c4ec17']
三者一致 = True
```

孤立节点**保留在图里、单独标记 `isolated=true`、不参与社区划分评分但参与计数**（社区 2 就是这 1 个点）。前端有「孤立」角色配色。

### 2.4 节点 / 边 / 副本统计关系总表

| 口径 | 数值 |
|---|---|
| catalog 文件数 | 40 |
| index.json 记录数 | 41 |
| index.json 唯一文件名 | 40 |
| 图谱节点（逻辑论文） | 36 |
| 图谱 copies 条目 | 39（唯一 38，虚高 1） |
| 图谱总边数 | 189 |
| 边类型分布 | arxiv 88 / title_exact 83 / title_fuzzy 18 |
| 孤立节点 | 1 |
| 被引用过的论文（cited_paper_count） | 31 |
| 引用过别人的论文（citing_paper_count） | 33 |
| 参考文献条目总数 | 4234 |
| 匹配成功条目 | 193（89 + 85 + 19） |
| 匹配覆盖率 | 4.6% |
| 待人工核验（review） | 48 |

---

## 第三部分：边权实验（P1）

复现：`scripts/_p1_weights.py`，结果落 `scripts/_p1_result.json`。

### 3.1 先验证：权重是否真的参与 Louvain 加权计算

**代码依据（`app/graph.py`）**：

| 位置 | 代码 | 作用 |
|---|---|---|
| `:1455` | `weight = weight_of or (lambda _e: 1.0)` | 未传则全 1.0 |
| `:1463` | `w_edges.append((rank[s], rank[t], float(weight(e))))` | 权重进入边表 |
| `:1476-1479` | `deg[a] += w; deg[b] += w; adj[a][b] += w; adj[b][a] += w` | 权重进入度与邻接 |
| `:1505` | `gain = w - deg[i] * comm_deg.get(c, 0.0) / (2.0 * m_edges)` | 权重进入模块度增益 |

**不是「前端有权重、算法内部无权」。** 旧版本 `:1463` 曾写死 `1.0`，本轮已改为传参。

**实跑反证**（人为放大 arXiv 边权重，分区必须发生变化）：

```
arXiv 边 x1.0   社区=3 规模={0:18, 1:17, 2:1}  Q=0.093552  与 B 口径成员不同 = 0
arXiv 边 x5.0   社区=4 规模={0:19, 1:12, 2:4, 3:1}  Q=0.177957  与 B 口径成员不同 = 14
arXiv 边 x50.0  社区=4 规模={0:16, 1:12, 2:7, 3:1}  Q=0.256393  与 B 口径成员不同 = 26
```

权重放大 5 倍就有 14 个节点改换社区，放大 50 倍有 26 个。**权重确实参与计算。**

### 3.2 三口径结果

| 口径 | 说明 | 边数 | 边权分布 | m | 社区数 | 社区规模 | 加权 Q |
|---|---|---|---|---|---|---|---|
| **A** | 全部边权重 1.0 | 189 | `{1.0: 189}` | 189.0 | 3 | `{0:21, 1:14, 2:1}` | **0.150780** |
| **B** | arXiv 1.0 / 标题精确 0.8 / 标题模糊 0.5 | 189 | `{0.5:18, 0.8:83, 1.0:88}` | 163.4 | 3 | `{0:18, 1:17, 2:1}` | **0.093552** |
| **C** | 去掉 18 条标题模糊边，其余全 1.0 | 171 | `{1.0: 171}` | 171.0 | 4 | `{0:17, 1:10, 2:8, 3:1}` | **0.119575** |

**注意**：B 的 Q 低于 A，**不是因为加权变差了**，而是因为分母 m 变小（163.4 vs 189），加权模块度的绝对量级不可直接跨口径比较。真正可比的是**随机基线中的相对位置**（3.3）。

### 3.3 随机基线

随机化方式：**目标端点（被引用方）不变，源端点（引用方）整体洗牌**，保持每篇论文的引用总数不变，只打乱它引用了谁。重复 **200 次**，固定随机种子 **`seed = 20261112`**。

| 口径 | 实际 Q | 随机 mean | 随机 p95 | 随机 max | 实际超过随机次数 | p 值 |
|---|---|---|---|---|---|---|
| A | 0.150780 | 0.138021 | 0.187205 | 0.212921 | 64 / 200 | **0.32** |
| B | 0.093552 | 0.137319 | 0.187448 | 0.209486 | 191 / 200 | **0.955** |
| C | 0.119575 | 0.146889 | 0.200253 | 0.243698 | 161 / 200 | **0.805** |

**解读（这是本轮最重要的结论）**：

- **A 口径的实际 Q 落在随机分布中位数附近**（超过 64/200，p = 0.32）。也就是说，**「这批引用关系能划出比随机更好的社区」在统计上不成立**。实际值连随机分布的 p95 都没到。
- **B 口径的实际 Q 比 96% 的随机图都低**（p = 0.955）。加权后划分**比随机还差**。
- **C 口径同样差于随机**（p = 0.805）。

结论：**三种口径的社区划分都没有通过随机基线检验。**

### 3.4 跨口径稳定性

| 对比 | NMI | ARI | 成员迁移数 |
|---|---|---|---|
| A vs B | 0.1879 | 0.0632 | 21 / 36 |
| A vs C | 0.1323 | **0.0009** | 22 / 36 |
| B vs C | 0.1435 | 0.0204 | 20 / 36 |

**A vs C 的 ARI = 0.0009**——去掉 18 条模糊边（占 9.5%）之后，两种划分的一致性**几乎等于随机**。说明这个社区结构**极端不稳定**：边权稍作调整，划分就重排。

### 3.5 社区 vs 分类一致性

| 口径 | NMI（社区, 主分类） |
|---|---|
| A | **0.2543** |
| B | 0.1728 |
| C | 0.2082 |

三个口径都不高（NMI 0.25 意味着社区几乎不能解释分类差异）。A 最高，B 最差——**加权让社区与主题的对应关系更差了**。

### 3.6 P1 结论

按指令「加权无明显改善就保留旧算法」，**建议保留口径 A（全权重 1.0），不实施边权改造**。理由：

1. B 与 C 的模块度低于 A，且在随机基线中位置更差。
2. 三种口径之间 ARI 极低（最低 0.0009），划分不稳定。
3. A 口径本身的 Q 也未超过随机分布 p95（p = 0.32），「发现子领域」这一功能定位**当前不成立**。

**因此本轮不实施任何边权改造。** 若要改善，方向不是调权重，而是**提高引用抽取的匹配覆盖率**（当前仅 4.6%，4234 条参考文献只匹配上 193 条）——边太少、太稀，才是社区结构弱的根因。

---

## 第四部分：核心节点科研解释（P2）

复现：`scripts/_p2_p3.py`、`scripts/_p2_detail.py`。

### 4.1 必须先说清的两句话

- **PageRank ≠ 论文质量，也不等于学术影响力的完整评价。** 它只在这 36 篇的小网络里衡量「被其它重要论文引用的程度」，与同行评审、引用总数（本库只有 36 篇，远小于真实引用数）、时间效应无关。
- **中介中心性 ≠ 研究价值。** 它衡量「有多少条最短路径经过它」，只反映**在这张局部图里的桥接位置**，高不代表论文更原创或更重要。

### 4.2 PageRank 前十

| # | 社区 | PR | 出度 | 入度 | 中介 | 标题 |
|---|---|---|---|---|---|---|
| 1 | 0 | 0.1737 | 0 | 6 | 0.0499 | Layer Normalization |
| 2 | 0 | 0.1467 | 1 | 16 | 0.8245 | Attention Is All You Need |
| 3 | 0 | 0.0788 | 1 | 18 | 0.8450 | BERT |
| 4 | 0 | 0.0568 | 2 | 12 | 0.2479 | GPT（Improving Language Understanding by Generative Pre-Training） |
| 5 | 0 | 0.0487 | 5 | 17 | 1.0000 | Language Models are Few-Shot Learners（GPT-3） |
| 6 | 0 | 0.0475 | 4 | 10 | 0.3377 | T5（Exploring the Limits of Transfer Learning） |
| 7 | 0 | 0.0407 | 3 | 14 | 0.3589 | Scaling Laws for Neural Language Models |
| 8 | 1 | 0.0300 | 7 | 11 | 0.4730 | Evaluating Large Language Models Trained on Code（Codex） |
| 9 | 0 | 0.0240 | 11 | 10 | 0.9200 | LLaMA |
| 10 | 1 | 0.0200 | 4 | 8 | 0.3010 | Training language models to follow instructions（InstructGPT） |

**结构位置解读**：

- **PR 第 1 的 Layer Normalization 入度只有 6，却是第一**。它引用了库内 0 篇（库内最早期工作，无可引用对象），但引它的 6 篇全是头部论文（Attention、GPT、GPT-4、T5、GLM-130B、On Layer Normalization）。**PageRank 按「引用者自身的 PR」分配票权**，所以少量高权重票就能超过 BERT 的 18 张低权重票。这是算法特性，不是错误，但**正好说明「PR 第一 ≠ 最重要」**——BN/LayerNorm 的技术重要性显然不如 Transformer。
- **Attention Is All You Need（PR#2）** 是社区的源头：出度 1（只引 LayerNorm）、入度 16。
- **BERT（PR#3）** 入度 18，全场最高，但 PR 只排第 3——被大量中低 PR 的论文引用。

### 4.3 中介中心性前十

| # | 社区 | 中介 | PR | 出度 | 入度 | 标题 |
|---|---|---|---|---|---|---|
| 1 | 0 | 1.0000 | 0.0487 | 5 | 17 | Language Models are Few-Shot Learners（GPT-3） |
| 2 | 0 | 0.9200 | 0.0240 | 11 | 10 | LLaMA |
| 3 | 0 | 0.8450 | 0.0788 | 1 | 18 | BERT |
| 4 | 0 | 0.8245 | 0.1467 | 1 | 16 | Attention Is All You Need |
| 5 | 1 | 0.7568 | 0.0098 | 16 | 1 | The Llama 3 Herd of Models |
| 6 | 0 | 0.5395 | 0.0145 | 13 | 5 | GPT-4 Technical Report |
| 7 | 1 | 0.4730 | 0.0300 | 7 | 11 | Codex |
| 8 | 0 | 0.3589 | 0.0407 | 3 | 14 | Scaling Laws for Neural Language Models |
| 9 | 0 | 0.3377 | 0.0475 | 4 | 10 | T5 |
| 10 | 1 | 0.3010 | 0.0200 | 4 | 8 | InstructGPT |

**中介 > 0 的节点 = 35 / 36**，只有孤立节点 Understanding Deep Learning 为 0。跨社区边 = 58 / 189（30.7%）。

**真正连接两个社区的四篇**（按其跨社区边的具体走向）——这是可以拿来对研究者讲的实际案例：

| 论文 | 结构作用 | 具体连接的社区内容 |
|---|---|---|
| **GPT-3**（BC=1.0） | 社区 0 的「被引锚点」，没有任何出边跨社区，但**有 6 条跨社区入边**来自社区 1 | 社区 1 的 InstructGPT、Codex、DPO、Visual Instruction Tuning、CLIP 都在引它 |
| **LLaMA**（BC=0.92） | 唯一一篇**双向跨社区**的论文：2 条跨社区出边 + 7 条跨社区入边 | 出边指向社区 1 的 InstructGPT、Codex；入边来自 Mistral、DeepSeek-V3、Llama 2、DPO、Llama 3 |
| **Llama 3 Herd**（BC=0.7568，PR 仅 0.0098） | 典型的「PR 低但中介高」：出度 16、入度 1，5 条出边全部跨社区 | 引了社区 0 的 GPT-4、ViT、BERT、Scaling Laws、LLaMA |
| **Codex**（BC=0.4730） | 社区 1 的第二大跨社区枢纽，跨社区边点出现 10 次，全场最多 | 连接代码/评测方向与基础模型方向的桥梁 |

### 4.4 对研究者的实际帮助（可讲的三个用法）

1. **「读进这个方向」用 PageRank 前几名**：Attention → BERT → GPT 系列，是按「谁被最重要的人引用」排出来的主线，适合初学者建骨架。
2. **「补哪个方向的课」用出度**：`The Llama 3 Herd of Models`（出度 16，引用库内 16 篇，只有 1 篇引它）、`GPT-4 Technical Report`（出度 13）、`Qwen2.5`（出度 12）是「读完这一篇就串完大半库」的入口。反过来，它们入度很低——**这类论文是综述型/报告型，适合当索引，不适合当原创参考。**
3. **「找两个方向之间的桥梁」用中介中心性**：LLaMA 是唯一双向跨社区的一篇，是「预训练基座 → 对齐/指令微调」这条链上的必经节点；Llama 3 是「把基础模型全家桶引用齐」的枢纽。

**必须同时说明**：这些名次是在 36 篇、189 边的**极稀疏图**上算的（真实参考文献 4234 条，只匹配上 4.6%）。换一个文献库，排名会完全变。**不能把「本库 PageRank 第一」当成「这篇论文整体影响力第一」对外宣称。**

---

## 第五部分：待确认匹配案例（P3）

复现：`scripts/_p3_review.py`。

### 5.1 两个队列的分界

| 队列 | 分数区间 | 条数 | 处理方式 |
|---|---|---|---|
| **已自动连边**（`title_fuzzy`） | 0.92 ~ 1.0 | 18 | 已进入图谱，作为边参与所有计算 |
| **待人工核验**（`reviews`） | 0.7552 ~ 0.8916 | **48** | 未连边，仅记录在 `reviews` 里 |

全库 `match_score < 1.0` 的边只有 18 条，全部是 `title_fuzzy` 类型，分布：`0.92~0.97: 7 / 0.97~0.99: 4 / 0.99~1.0: 7`。**指令关注的 0.75~0.92 区间，48 条全部落在 review 队列里，没有任何一条被自动连边。**

### 5.2 已自动连边的 18 条：逐条核验结果

**真实重复（应当连边）——10 条**。特征都是**断行连字符**或**关键词被引切成两块**：

| score | 引用原文标题 | 实际论文 | 判定 |
|---|---|---|---|
| 0.9939 | `Exploring the limits of transfer learn- ing with a uniﬁed text-to-text` | T5 | ✅ 真实重复（断行连字符） |
| 0.9937 | `BERT: Pre-training of deep bidirec- tional transformers for language u` | BERT | ✅ 真实重复（断行） |
| 0.9926 | `Zero: Memory optimizations toward training tril- lion parameter models` | ZeRO | ✅ 真实重复（断行 + 大小写） |
| 0.9916 ×4 | `Improving Language Under- standing by Generative Pre-Training`（4 篇引用同一句） | GPT | ✅ 真实重复（断行） |
| 0.9867 | `Language mod- els are few-shot learners` | GPT-3 | ✅ 真实重复（断行） |
| 0.9804 ×3 | `Atten- tion is all you need` | Attention | ✅ 真实重复（断行） |
| 0.9448 | `Direct preference optimization: Your language model is secretly a rewa` | DPO | ✅ 真实重复（长标题截断） |
| 0.9343 | `An image is worth 16x16 words: Transformers for image recognition` | ViT | ✅ 真实重复（大小写） |
| 0.9268 | `Glm-130b: An open bilingual pre-trained model` | GLM-130B | ✅ 真实重复（大小写） |
| 0.9257 | `Exploring the limits of transfer learning with a unified text-to-text` | T5 | ✅ 真实重复（重复条目） |

**错误相似（不该连边）——6 条，且这是一个真缺陷**：

| score | 引用原文标题 | 被连到了 | 实际情况 | 严重性 |
|---|---|---|---|---|
| **0.9333 ×3** | `Qwen Technical Report`（出现在 Instruction Pre-Training / Llama 3 / DeepSeek-V3 的参考文献里） | **GPT-4 Technical Report** | 引用的是 **Qwen 家族（2309.16609）**，不是 GPT-4 | ⚠️ **错边** |
| 0.9268 附带 | — | — | — | — |

**这 3 条的根因**：模糊匹配只比标题字符串。「Qwen Technical Report」与「GPT-4 Technical Report」共享 `Technical Report` 这个高频词，字符相似度 0.9333 越过 0.92 阈值，就被连成了边。三条边分别来自 Instruction Pre-Training（社区 0）、Llama 3（社区 1）、DeepSeek-V3（社区 1），**恰好都是跨社区边**——直接污染了 3.3 的社区划分。

**改进方向（本轮不动算法，只记录）**：引用条目里有 `reference_arxiv_id = 2309.16609` 时，应该先用 arXiv ID 精确匹配，匹配不上就不连边，而不是回落标题模糊。当前逻辑是「arXiv 匹配失败 → 标题模糊兜底」，把本可判定的「不同论文」判成了相似。

### 5.3 review 队列 48 条：逐条判定

48 条归并后是 16 组不同（引用标题, 候选论文）组合。

**真实重复（本应连边，被 0.92 阈值挡住了）——8 组 / 26 条**：

| 组内条数 | score | 引用原文 | 应连到 | 挡回原因 |
|---|---|---|---|---|
| 6 | 0.7826 | `Exploring the limits of transfer learning with a uniﬁed text-to-text transformer. Journal of Machine Learning Research, 21:1–67` | T5 | 尾部带了期刊卷页 |
| 5 | 0.8475 | `Attention is all you need. Advances` | Attention | 被切在 `Advances` |
| 3 | 0.7845 | `Switch transformers: Scaling to tril- lion parameter models with simple and efficient sparsity. Journal of Machine Learning Research, 23(120):1–39` | Switch Transformers | 卷页 + 断行 |
| 2 | 0.8627 | `OpenAI. Gpt-4 technical report` | GPT-4 | 带了作者前缀 |
| 2 | 0.7977 + 0.7788 | `Chain-of-thought prompting elicits reasoning in large language models. In S. Koyejo, ...` | CoT | 带了会议/编委 |
| 1 | 0.8916 | `Language models are few-shot learners. Advances` | GPT-3 | 尾部 `Advances` |
| 1 | 0.7727 | `Qwen2 Technical Report` | **Qwen2.5 Technical Report** | 版本号差异（这是**同一家族不同版本**，边界情况） |
| 1 | 0.7552 | `Least-to-Most Prompting Enables Complex Reasoning in Large Language Models` | **Chain-of-Thought** | ❌ 其实是**不同论文**，见下 |

**错误相似（不该连边，幸好被挡住了）——8 组 / 22 条**：

| 组内条数 | score | 引用原文 | 被相似到 | 实际情况 |
|---|---|---|---|---|
| **10** | 0.7840 | `Language models are unsupervised multitask learners`（**GPT-2**） | Instruction Pre-Training: Language Models are **Supervised** Multitask Learners | ❌ 不同论文，标题仅一词之差 |
| **10** | 0.8235 | `Finetuned language models are zero-shot learners`（**FLAN**） | Language Models are **Few-Shot** Learners（GPT-3） | ❌ 不同论文 |
| 2 | 0.8049 | `Large Language Models are Zero-Shot Reasoners`（**Zero-shot-CoT, 2205.11916**） | Language Models are Few-Shot Learners（GPT-3） | ❌ 不同论文，**引用条目已给出 arXiv ID 2205.11916** |
| 2 | 0.7595 | `Scaling Data-Constrained Language Models`（**2305.16264**） | Scaling Laws for Neural Language Models（2001.08361） | ❌ 不同论文，**已给出 arXiv ID 2305.16264** |
| 1 | 0.7907 | `Qwen Technical Report`（**2309.16609**） | GPT-4 Technical Report | ❌ 不同论文，**已给出 arXiv ID** |
| 1 | 0.7727 | `Qwen2 Technical Report`（**2407.10671**） | GPT-4 Technical Report | ❌ 不同论文，**已给出 arXiv ID** |
| 1 | 0.8444 | `Palm 2 technical report` | GPT-4 Technical Report | ❌ 不同论文（PaLM 2） |
| 1 | 0.7895 | `Group normalization` | Layer Normalization | ❌ 不同论文（GN vs LN） |

### 5.4 P3 结论与风险

1. **0.75~0.92 区间目前只有 48 条，全部在 review 队列，没有一条被自动连边。** 人工核验机制是有效的。
2. **但如果把阈值从 0.92 降到 0.75，会误连 22 条错误边**（8 组里的 10+10+2+2+1+1+1+1），其中「GPT-2 ↔ Instruction Pre-Training」和「FLAN ↔ GPT-3」各 10 条，是最高频的误报。**强烈不建议下调阈值。**
3. **反方向的损失**：有 26 条真实重复被 0.92 挡住（T5 被挡 6 次、Attention 被挡 5 次、Switch 被挡 3 次），主要是「尾部带期刊卷页」「断在 Advances」「带作者/会议前缀」这三类。**这才是最值得改的地方**，且不需要动算法——只需在模糊匹配前**剥掉尾部的期刊卷页、会议名、作者前缀**。
4. **已自动连边的 18 条里有 3 条错边**（`Qwen Technical Report → GPT-4`，score 0.9333），全部源于「arXiv ID 匹配失败后回落到标题模糊」。**这 3 条是当前最高优先级的修正对象**，因为它污染了社区划分。
5. **本轮不重写匹配算法，不扩大自动连边范围。** 以上全部只记录，等负责人确认。

---

## 第六部分：风险与待决策事项

### 6.1 数据层面的风险

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | `1706.03762v7.pdf` 俩记录同名（`1706.03762v7` 与 `bdfaa68d8984`），`copies` 重复 | `file_count` 39 vs 唯一 38；`merged_away` 3 vs 真合并 2 | 修合并逻辑去重 `copies`，或修定价 `file_count` 口径 |
| 2 | `1610.08613.pdf`、`2312.00752.pdf` 在 catalog 有、`structures.json` 无 | 不进图谱（36 vs 40），且无摘要可供分类（其中 Mamba 仍可经标题分类） | 查 PDF 解析为何失败；标题已由 catalog 兜底 |
| 3 | 引用匹配覆盖率仅 4.6%（193 / 4234） | 图太稀，社区结构与核心节点排名的统计基础很薄 | 提高匹配率比调边权更有效 |

### 6.2 算法与统计的风险

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| 1 | 三口径社区划分均未通过随机基线 | A p=0.32、B p=0.955、C p=0.805 | 保留 A，不实施边权改造 |
| 2 | 划分极不稳定 | A vs C 的 ARI = 0.0009 | 对外统一称「引用关系簇」，不称「研究领域」 |
| 3 | PageRank 名次反直觉（LayerNorm 第一） | 入度 6 却 PR#1 | 讲解时必须说明「PR 按引用者权重分配票权，不等于重要性」 |
| 4 | 存在 3 条 Qwen→GPT-4 错边 | score 0.9333 越过 0.92 | 引用条目有 arXiv ID 时应优先/仅用 arXiv 匹配 |

### 6.3 需要负责人决策的四个问题

1. **是否接受「社区功能降级为引用关系簇」？** 现有数据不支持「发现研究子领域」的表述。
2. **是否修正 3 条 Qwen→GPT-4 错边？** 这会改变 189 条边中的 3 条，从而改变社区划分结果（需重跑实验并更新文档）。
3. **是否清理 `1706.03762v7.pdf` 同名重复记录？** 只影响计数字段，不影响算法。
4. **是否投入提高匹配覆盖率？** 这是唯一能实质改善社区结构的方向，但工作量远大于本轮。

---

## 附录：复现方法

```powershell
cd D:\alonely\deepseek\campus-agent
$env:PYTHONPATH = "D:\alonely\deepseek\campus-agent"
$env:PYTHONIOENCODING = "utf-8"

.venv\Scripts\python.exe scripts\_r_c0.py      # 1.2 社区0明细 24 条 + 三种计数
.venv\Scripts\python.exe scripts\_r_sub.py     # 1.4 组合筛选 7 vs 6
.venv\Scripts\python.exe scripts\_p0_graph.py  # 2.1-2.4 节点/边/副本统计
.venv\Scripts\python.exe scripts\_p0_align.py  # copies 唯一性核对
.venv\Scripts\python.exe scripts\_p0_merge.py  # merge_decisions 核对
.venv\Scripts\python.exe scripts\_p1_weights.py  # 3.1-3.5 全部 P1 数字
.venv\Scripts\python.exe scripts\_p2_p3.py     # 4/5 榜单与 fuzzy 边明细
.venv\Scripts\python.exe scripts\_p2_detail.py # 4.2-4.4 结构位置
.venv\Scripts\python.exe scripts\_p3_review.py # 5.3 review 队列 48 条
```

随机基线固定 `SEED = 20261112`、`REPEATS = 200`，重跑结果完全一致（Louvain 实现无随机性，洗牌有固定种子）。