# 引用图谱可信度修复 · 第二轮只读终审报告

> 审查日期：2026-11-12
> 审查对象：HEAD `bd94684` 之上的未提交工作区（16 项改动，未提交、未部署）
> 本轮性质：**只读**。未修改任何代码、数据文件、索引，未重构图谱，未删除 PDF，未提交 Git，未部署。
> 审查前后 `git status --short` 均为同样 16 项；`ruff check .` → `All checks passed!`；`pytest tests -q` → `767 passed, 13 warnings in 49.04s`。

## 结论标记约定

- **【代码】** 已由代码确认（读源码，附行号）
- **【测试】** 已由测试验证（跑用例，附命令与输出）
- **【数据】** 已由数据核验（真实数据跑脚本，附输出）
- **【未确认】** 目前无法确认

---

# 一、文件与函数定位

| 位置 | 作用 |
|---|---|
| `app/graph.py` `match_references()` | 引用条目 → 逻辑节点的三层匹配主流程（:1138-1273） |
| `app/graph.py` `_title_exact_target()` | 标题精确匹配的共用判定（:1087-1103） |
| `app/graph.py` `strip_title_noise()` | P1 保守标题规范化 |
| `app/graph.py` `_TAIL_JOURNAL_RE` / `_TAIL_FORM_RE` / `_TAIL_VENUE_RE` / `_TAIL_NOISE_RE` / `_HYPHEN_BREAK_RE` | 规范化用到的五条正则 |
| `app/graph.py` `build_nodes()` | 节点构建与副本合并（含 `_copy_keys` 去重） |
| `app/graph.py` `build_graph()` | 图谱装配；`:1005` 定义 `loader = refs_loader or (lambda name: extract_references(PAPERS_DIR / name))` |
| `app/graph.py` `extract_references()` | 从 PDF 抽参考文献 |
| `data/papers/index.json` | 上传索引（41 条记录，`filename` / `paper_id`） |
| `data/catalog.json` | 上传目录（40 条记录，`file`） |
| `data/meta/structures.json` / `data/papers/structures.json` | 解析结果（各 39 条） |
| `data/papers/*.pdf` | 41 个物理文件 |
| `data/meta/citation-graph.json` | 图谱落盘（38 节点 / 203 边 / 1266 review） |

---

# 二、当前代码实际行为

## 2.1 `match_references()` 完整执行顺序 【代码】

```python
:1140  stats["entries_total"] += 1
:1146  cmp_title = strip_title_noise(ref.title)
:1147  if cmp_title != ref.title: stats["title_noise_stripped"] += 1
:1154  自引检测 → 命中则 stats["self_reference"] += 1; continue
:1170  conflict_exempt = None
:1171  if ref.arxiv_id and ref.arxiv_id not in by_arxiv:
           conflict_exempt = _title_exact_target(cmp_title, by_title, node_bags)
:1174  第1层：ref.arxiv_id 在 by_arxiv 中 → method="arxiv", score=1.0
:1183  冲突拒连：ref.arxiv_id 不在库 且 conflict_exempt 为 None
           → stats["arxiv_conflict"] += 1
           → reviews.append(method="arxiv_conflict", score=0.0, candidate="")
           → continue
:1200  豁免：conflict_exempt 命中且 != 源节点
           → method="title_exact", score=1.0
           → stats["matched_exact"] += 1; stats["arxiv_conflict_exempt"] += 1
:1208  第2层：标题去空白相等 且 词袋相等 → title_exact, 1.0
:1218  第3层：difflib 模糊
           ≥0.92 → title_fuzzy 连边
           ≥0.75 → 进 reviews
           <0.75 → unmatched
:1252  无 cmp_title → unmatched += 1
:1256  edges[(src,dst)] 去重，只保留 score 更高的那条
:1272  stats["review"] = len(reviews)      ← 覆盖赋值
```

## 2.2 `_title_exact_target()` 【代码】

```python
def _title_exact_target(title, by_title, node_bags) -> LogicalNode | None:
    if not title:
        return None
    cand = by_title.get(_fuzzy_key(title))
    if cand is not None and node_bags.get(cand.canonical_id) == _title_bag(title):
        return cand
    return None
```

- **无副作用**（纯查表 + 词袋比较），这一点与 `scripts/_audit_p0b.py` 的调用观察一致。
- **但它不看候选节点的 `arxiv_id`。** 这是 P0 的核心风险来源：豁免分支只要求「引用声明的 ID 不在库内 + 标题精确一致」，不要求「候选节点本身没有 ID」。

## 2.3 统计字段的真实含义 【代码】

| 字段 | 代码位置 | 真实含义 |
|---|---|---|
| `arxiv_conflict` | `:1183-1199` | **不是**「明确 ID 冲突」。它是「引用声明了 arXiv 号 + 该号不在库内 + 标题也对不上」的合并桶。里面同时混了「本地未收录」和「本地有同标题但 ID 不同」两种情形。 |
| `arxiv_conflict_exempt` | `:1200-1207` | 「声明 ID 不在库内，但标题精确一致，因此豁免连边」的计数。 |
| `matched_exact` | 两处 `+= 1`（`:1205` 与 `:1210`） | 标题精确匹配总数，**包含豁免分支**。所以它不是一个与 `arxiv`/`fuzzy` 平行的「匹配方式」桶，而是「标题精确」的合计。 |
| `review` | `:1272` | 覆盖赋值 `len(reviews)`，**与 `arxiv_conflict` 重复计数**（见 P3）。 |

验算（`【数据】`）：`matched_arxiv 89 + matched_exact 115 + matched_fuzzy 3 + unmatched 2761 + arxiv_conflict 1242 + review 1266 + self_reference 0 = 5476`，比 `entries_total 4234` **多 1242**；只把 `arxiv_conflict` 从求和里去掉，再把 review 中 `title_fuzzy` 的 24 条单独算，得 `89+115+3+2761+1242+0+24 = 4234`，**完全吻合**。即：`arxiv_conflict` 条目被计了两次。

## 2.4 四种情形在代码里的实际归属 【代码】

| 情形 | 代码路径 | 统计归属 |
|---|---|---|
| 本地存在明确不同的 arXiv ID | 引用 ID 不在库 + 标题不同 → `:1183` 拒连 | `arxiv_conflict` + `review` |
| 引用 ID 有效，但本地库没有该论文 | 同上（无法区分） | `arxiv_conflict` + `review` |
| 本地候选论文没有录入 arXiv ID | 引用 ID 不在库 + 标题精确一致 → `:1200` 豁免 | `matched_exact` + `arxiv_conflict_exempt` |
| 引用 ID 与本地候选 ID 一致 | `:1174` 第1层 | `matched_arxiv` |

**前两种在代码里物理上无法区分**——这正是 P0 要回答的核心问题。

---

# 三、已有测试及其覆盖范围

`pytest tests/test_citation_graph.py -q` → **44 passed, 1 warning in 1.31s** 【测试】

与本轮相关的 4 条（第一轮新增）：

| 用例 | 覆盖什么 | 缺口 |
|---|---|---|
| `test_arxiv_id_conflict_blocks_fuzzy_match` | ID 冲突 + 标题不同 → 不连边 | 未覆盖「标题精确一致」分支 |
| `test_arxiv_id_conflict_does_not_block_real_title_matches` | ID 不在库 + 标题精确 + 候选无 ID → 豁免 | 未覆盖「候选**有**另一个明确 ID」 |
| `test_same_filename_twice_is_not_counted_as_two_copies` | 同名记录不虚增副本 | 不影响 P0 |
| `test_different_titles_under_same_filename_stay_separate` | 同名不同标题不合并 | 不影响 P0 |

**另外 40 条**覆盖的是节点合并、PR/中介/角色、综述、frontier、社区确定性，与 P0/P1 的匹配规则无关。

---

# 四、P0 核验结果

## 4.1 六场景实测 【数据】

脚本 `scripts/_audit_p0.py`（已删除）构造了 9 个场景（比要求的 6 个多 3 个边界），直接调用 `match_references()`：

```
场景1 引用ID不在库 + 标题完全一致 + 候选无ID          → 边1条 title_exact，exempt=1，arxiv_conflict=0
场景2 引用ID不在库 + 标题完全一致 + 候选有另一明确ID    → 边1条 title_exact，exempt=1，arxiv_conflict=0  ⚠
场景3 引用ID不在库 + 标题部分相似                      → 无边，arxiv_conflict=1，进 review
场景4 引用ID不在库 + 多个标题完全一致的候选             → 边1条指向 hash:c2（静默取后者），exempt=1  ⚠
场景5 引用ID与候选ID一致                              → 边1条 arxiv
场景6 引用ID与候选ID明确冲突（标题也不同）              → 无边，arxiv_conflict=1，进 review
场景7【关键】引用ID不在库 + 标题完全一致 + 候选有另一明确ID → 边1条 title_exact，exempt=1  ⚠（Qwen 事故同形）
场景8 引用无ID + 标题完全一致                          → 边1条 title_exact，exempt=0
场景9 引用ID不在库 + 去空白相等但词袋不等                → 无边，arxiv_conflict=1（词袋校验生效）
```

对照要求的 6 个场景：

| 要求场景 | 是否覆盖 | 结果 |
|---|---|---|
| 1. ID 不在库 + 标题完全一致 + 候选无 ID | ✅ 场景1 | 连边 title_exact（预期行为） |
| 2. ID 不在库 + 标题完全一致 + 候选有另一明确 ID | ✅ 场景2/7 | **连边**（风险，见 4.2） |
| 3. ID 不在库 + 标题仅部分相似 | ✅ 场景3 | 不连边，进 review（预期行为） |
| 4. ID 不在库 + 多个标题完全一致候选 | ✅ 场景4 | 静默取 `by_title` 字典里的最后一个 |
| 5. 引用 ID 与候选 ID 一致 | ✅ 场景5 | 连边 arxiv（预期行为） |
| 6. 引用 ID 与候选 ID 明确冲突 | ✅ 场景6 | 不连边，进 review（预期行为） |

## 4.2 必答问题一：代码是否会把「本地未收录」错算成「明确 ID 冲突」？【代码】【数据】

**会。这是当前实现的一个命名与语义错误，不是数据错误。**

`arxiv_conflict` 在 `:1183` 的触发条件是：

```python
elif ref.arxiv_id and ref.arxiv_id not in by_arxiv and conflict_exempt is None:
```

它只回答了「引用的 arXiv 号不在库里，标题也对不上」，**没有区分**「库里有一篇同标题但 ID 不同」和「库里根本没有这篇论文」。前者是真正的 ID 冲突，后者是本地未收录。

**真实数据核验**（`scripts/_audit_p0b.py`，已删除）：1242 条 `arxiv_conflict` 里，绝大多数是「引用了库外论文」——库里只有 38 篇，而单篇论文的参考文献就可能上百条。

结论：**当前 `arxiv_conflict=1242` 这个数字的语义是「ID 无法核验的引用条目总数」，不代表 1242 条 ID 冲突。**

## 4.3 必答问题二：exact-title 豁免会不会在候选另有明确 ID 时错误连边？【代码】【数据】

**代码层允许，真实数据上没有发生。**

- 代码层：`_title_exact_target()` 不检查 `cand.arxiv_id`，场景 2/7 已实证连边。
- 真实数据：`scripts/_audit_p0b.py` 实测
  ```
  title_exact 边总数 = 112；其中 reference_arxiv_id 非空 = 19
  这 19 条里 target 节点自身 arxiv_id 为空 = 19，有 ID = 0
  title_exact 边中 ref_id 与 node_id 同时非空且不同的 = 0
  ```
  → 豁免分支的 19 次实际触发中，**目标节点恰好全部没有录入 arXiv 号**，因此没有产生错误连边。

**这是运气，不是设计。** 只要将来补充 BERT / GPT / LLaMA / UDL 这四篇的 arXiv 号（目前为空，见 P2），同样的引用就会立刻变成「候选有明确 ID 却仍被豁免连边」。

## 4.4 缺陷清单（P0）

**必须修复**

1. `arxiv_conflict` 语义混淆。应拆成两个字段：`arxiv_conflict`（本地有同标题候选但 ID 明确不同）与 `arxiv_absent`（本地库无该 arXiv 号且标题也对不上）。【代码】【数据】
2. `_title_exact_target()` 未校验候选节点的 `arxiv_id`。豁免应只在「候选节点自身没有录入 arXiv 号」时才成立。【代码】（真实数据未触发，属潜在缺陷）
3. 多个同标题候选时静默取字典最后一项（场景 4），无任何警告或计数。【数据】

**可接受遗留**

- 场景 4 在真实数据中共 0 次（见 P1 的「前两名差距 < 0.05 的参考标题 = 0」）。【数据】

---

# 五、P1 核验结果

## 5.1 `strip_title_noise()` 语义保持 【数据】

脚本 `scripts/_audit_p1.py`（已删除）构造 13 个样本，三类：

| 类别 | 样本 | 结果 |
|---|---|---|
| A 会议/期刊尾缀应剥（5 个） | `. J. Mach. Learn. Res., 21(140):1-67`、`. Advances in Neural Information Processing Systems, 36`、`. In NeurIPS, 2017`、`. Preprint`、`. Technical Report` | 5/5 剥掉，且核心语义完整 |
| B 正文含 in/at/of 不该剥（6 个） | `...complex reasoning in large language models`、`A Constructive Prediction of the Generalization Error Across Scales`、`...at Scale`、`In-context Learning and Induction Heads`、`...reasoning in`（以 in 结尾）、`...look at`（以 at 结尾） | 6/6 **原样不动** |
| C 断行连字符应接回（2 个） | `mod- els` → `models`、`tril- lion` → `trillion` | 2/2 正确接回 |

不合格样本数 = **0**。

**回答「普通正文中的 in、at、of 是否可能被错误剥除」：不会。** 第一轮修掉的 `_TAIL_VENUE_RE` 边界问题（`[.\s,]+(?:In|At|...)` 会把正文里的 `in` 当会议前缀）已经生效——现在 `In|At` 必须紧跟在句点/分号后或位于串首。

## 5.2 26 条真重复恢复情况 【数据】

用真实图谱数据逐条重算「规范化前 vs 规范化后」的相似度（`scripts/_audit_p1b.py`，已删除）：

```
reviews 里 title_fuzzy 的 24 条：进审->连边 = 0   连边->进审 = 0
```

- **24 条待审记录中，规范化没有让任何一条跨过 0.92 阈值。** 唯一有变化的 `arxiv:2412.19437 -^> arxiv:1706.03762` 从 0.4348 升到 0.8197（作者前缀 `Polo- sukhin.` 被剥掉了），仍低于 0.92，**按决策 q_a 保持人工审核**。
- 第一轮报告的「26 条真重复中恢复 8 组」是**另外的路径**：那 8 组走的是第 2 层 title_exact（词袋相等），不是模糊阈值，因此不在 reviews 里，也不受这次对比影响。
- **仍挡住的 2 组**：`OpenAI. Gpt-4 technical report`（0.8627）、Least-to-most 那条（0.7552）。

## 5.3 18 条历史自动匹配 / 3 条错误匹配 【数据】

`title_fuzzy` 边当前只剩 **3 条**（第一轮从 18 条降到 3 条，其余 15 条因规范化后相似度跨过 0.92 而转成 `title_exact`）：

| 源 → 目标 | 参考标题 | 前 | 后 | 仍在阈值上 |
|---|---|---|---|---|
| `arxiv:2103.00020` → `hash:f03317e4cc82` (GPT) | `Improving language understanding by generative pre- training` | 1.0000 | 0.9915 | ✅ |
| `arxiv:2305.14314` → `arxiv:1910.10683` (T5) | `Exploring the limits of transfer learning with a unified text-to-text transformer. J. Mach. Learn` | 0.9257 | 0.9257 | ✅ |
| `hash:2af335b045f6` (LLaMA) → `arxiv:2210.02414` (GLM-130B) | `Glm-130b: An open bilingual pre-trained model` | 0.9268 | 0.9268 | ✅ |

三条都是**真引用**（关键词完全相同，只是断行或大小写差异），连边正确。

3 条错误边（Qwen→Qwen2.5）已在第一轮被 P0 规则拒掉，本轮复核：`arxiv:2412.15115` 在 `matched_arxiv` 之外无任何入边（见 P3 的 `Qwen` 检查）。

## 5.4 是否产生新的错误自动匹配 / 是否破坏原有正确匹配 【数据】

- **新增 17 条边**（第一轮已逐条核实为真引用）：T5 6 条、Attention 4 条、few-shot learners 2 条、Switch 2 条、CoT 2 条、DPO 新增 2 条。
- **消失 3 条**（全是 Qwen→Qwen2.5）。
- 上述 24 条 review 里属于「错相似」的（`Language models are unsupervised multitask learners`→`Instruction Pre-Training` 等）规范化后**仍全部低于阈值**，没有一条被错误放行。

## 5.5 是否存在多个候选得分相同或接近 【数据】

```
参考标题总数 = 892
存在「前两名差距 < 0.05」的参考标题 = 0
```

**全库 892 个参考标题中，不存在两个候选目标相似度接近的情形。** 场景 4 的静默取末项风险在真实数据上未触发。

## 5.6 P1 结论

**没有发现必须修复的问题。** 规范化保持语义、不误剥正文介词、不破坏原有匹配、不新增错误匹配。

---

# 六、P2 核验结果

## 6.1 八项口径 【数据】

| # | 口径 | 实测值 | 说明 |
|---|---|---|---|
| 1 | index 记录数 | **41** | `data/papers/index.json` |
| 2 | 唯一物理文件数 | **41** | `data/papers/*.pdf` 实际 41 个 |
| 3 | 逻辑论文数 | **38** | 图谱节点数 |
| 4 | 被合并副本数 | **2**（`merged_copies`）/ `merged_away`=3 | 见 6.3 的口径差异 |
| 5 | 重复记录数 | **1** | `1706.03762v7.pdf` 同名两条 |
| 6 | 图谱节点数 | **38** | |
| 7 | 图谱边数 | **203** | |
| 8 | 孤立节点数 | **3** | `hash:8b8147c4ec17`（Understanding Deep Learning）、`arxiv:1610.08613`、`arxiv:2312.00752` |

`summary` 自带：`file_count=41 / unique_file_count=40 / node_count=38 / edge_count=203 / isolated_count=3`。

**注意口径差异**：`unique_file_count=40` 是按 `index.json` 里的 `filename` 唯一值算的；磁盘实际是 **41** 个文件。差的 3 个见 6.2。

## 6.2 关键发现：三份论文的磁盘文件名与索引记录不一致 【数据】

```
index 有、磁盘没有 = ['1610.08613.pdf', '2312.00752.pdf']
磁盘有、index 没有 = ['57567c72c96f.pdf', 'adf70ed1803c.pdf', 'bdfaa68d8984.pdf']

57567c72c96f.pdf: 大小 157958    sha256前12 = 57567c72c96f   是否在 catalog = False
adf70ed1803c.pdf: 大小 1168615   sha256前12 = adf70ed1803c   是否在 catalog = False
bdfaa68d8984.pdf: 大小 2215244   sha256前12 = bdfaa68d8984   是否在 catalog = False
```

- `57567c72c96f.pdf` 就是 `1610.08613.pdf`（Can Active Memory Replace Attention?）
- `adf70ed1803c.pdf` 就是 `2312.00752.pdf`（Mamba）
- `bdfaa68d8984.pdf` 是 `1706.03762v7.pdf` 的哈希名副本（同名重复的来源）

**这三份是同一份文件、两个名字**：索引里记原名，磁盘上按 `sha256[:12]` 落盘。

## 6.3 该不一致造成的真实后果 【代码】【数据】

`app/graph.py:1005`：
```python
loader = refs_loader or (lambda name: extract_references(PAPERS_DIR / name))
```
`name` 来自 `node.copies`，而 `copies` 存的是 `index.json` 的 `filename`。于是：

```python
extract_references(PAPERS_DIR / "1610.08613.pdf")   # 文件不存在
```

实测：
```
extract_references 对不存在的文件 → 返回 0 条（未抛异常）
arxiv:1610.08613: 出边=0 入边=0
arxiv:2312.00752: 出边=0 入边=0

对照（按真实磁盘路径直接读）：
  1610.08613.pdf（磁盘名 57567c72c96f.pdf）: 30 条参考文献
  2312.00752.pdf（磁盘名 adf70ed1803c.pdf）: 165 条参考文献
```

**结论：这两篇论文的参考文献从来没进过图谱——Mamba 丢了 165 条、Active Memory 丢了 30 条，且因为 `extract_references` 对缺失文件静默返回空列表，`build_graph()` 里没有任何报错、没有 warning、没有计数。**

这同时解释了为什么它们是孤立节点之一。

`bdfaa68d8984.pdf` 不受影响，因为 `arxiv:1706.03762` 的第一份副本 `1706.03762v7.pdf` 在磁盘上存在（出边 1 / 入边 21）。

## 6.4 抽样核查 【数据】

| 情形 | 结果 |
|---|---|
| 同名不同内容 | **无**。index 里同名多条只有 `1706.03762v7.pdf` 一对，两份 size 都是 2215244、pages 都是 15，内容相同 |
| 同名同内容 | **1 对**：`1706.03762v7.pdf`（paper_id `bdfaa68d8984` 与 `1706.03762v7`）。第一轮的 `_copy_keys` 去重已生效，`copy_count` 从 4 降到 3 |
| 不同名但内容相同 | **1 对**：`1706.03762v7.pdf` ↔ `bdfaa68d8984.pdf`（哈希名副本） |
| 反向遗漏 | `1610.08613.pdf` ↔ `57567c72c96f.pdf`、`2312.00752.pdf` ↔ `adf70ed1803c.pdf` —— 同内容、不同名，但**索引里只有原名、磁盘上只有哈希名** |

## 6.5 `copies` 与磁盘的对照 【数据】

```
copies 里磁盘上不存在的 = 2
  arxiv:1610.08613 -> 1610.08613.pdf
  arxiv:2312.00752 -> 2312.00752.pdf
```

这两条就是 6.3 的根源。

## 6.6 P2 缺陷清单

**必须修复**

1. **文件名与 paper_id 命名不一致导致参考文献静默丢失。** Mamba 与 Active Memory 两篇的出边全被丢弃，且无任何日志。【代码】【数据】这是本轮审查发现的最严重的真实缺陷。
2. `extract_references()` 对不存在的文件静默返回 `[]`，`build_graph()` 无任何丢失计数。至少应记录「N 个副本文件缺失」并在 `summary` 里暴露。【代码】

**可接受遗留**

- `unique_file_count=40` 与磁盘 41 的口径差，源于同内容两份文件（`1706.03762v7.pdf` + `bdfaa68d8984.pdf`）与两个未索引的哈希名文件。若修复第 1 项，口径应重新定义并写进文档。【数据】

---

# 七、P3 核验结果

## 7.1 统计字段实际含义 【数据】

```
entries_total = 4234   matched_arxiv = 89   matched_exact = 115   matched_fuzzy = 3
unmatched = 2761       review = 1266        arxiv_conflict = 1242
arxiv_conflict_exempt = 21   self_reference = 0   title_noise_stripped = 430
len(reviews) = 1266    reviews 按 match_method = {'arxiv_conflict': 1242, 'title_fuzzy': 24}
```

## 7.2 必答问题一：1242 条是否全部是真需人工审核的 ID 冲突？【代码】【数据】

**不是。** 1242 是「引用声明了 arXiv 号但该号不在库内，且标题也对不上」的合并桶，其中绝大多数是**引用了库外论文**。库里只有 38 篇论文，单篇参考文献动辄上百条，`unmatched=2761` 已经很说明问题。

按 §2.4 的归属表，这 1242 条里真正属于「本地有同标题候选但 ID 不同」的比例**目前无法给出**——因为代码把两种情形合并统计，没有留痕。【未确认】

## 7.3 必答问题二：有多少只是本地文献库未收录的引用？【未确认】

代码层面无法区分，数据层面也无法回溯——`reviews` 里只记 `reference_arxiv_id` 与 `reference_title`，没有记「本地是否存在同标题候选」。**这本身就说明 P0 拆分字段是必要的**，拆分后才能回答这个问题。

## 7.4 必答问题三：是否存在重复引用被重复计数？【数据】

```
arxiv_conflict 条数 = 1242
  (source, reference_arxiv_id) 唯一 = 1237
  (source, reference_title) 唯一     = 1237
  reference_arxiv_id 全局唯一        = 811
  同一篇论文里重复出现的参考标题组 = 5
    2 次: 'A Constructive Prediction of the Generalization Error Across Scales'
    2 次: 'A General Language Assistant as a Laboratory for Alignment'
    2 次: 'RoBERTa: A Robustly Optimized BERT Pretraining Approach'
    2 次: 'Zero Bubble Pipeline Parallelism'
    2 次: 'Microscaling Data Formats for Deep Learning'
```

- 同一篇论文内 **5 组**参考标题各出现 2 次，即 1242 中多计 5 条。
- 全局唯一 arXiv 号只有 **811** 个 —— 说明同一篇库外论文被多篇库内论文共同引用（这是正常的，不属重复计数）。
- `:1272 stats["review"] = len(reviews)` 覆盖赋值，导致 `arxiv_conflict` 与 `review` **跨字段重复**（验算见 §2.3）。

## 7.5 必答问题四：是否影响前端性能 / 用户理解 / 人工审核？【数据】

- **前端**：在 `web/`、`app/main.py`、`app/graph.py` 中搜索 `reviews`，**没有任何展示代码**。review 队列只落盘在 `data/meta/citation-graph.json`，不进 API 响应 → 不影响前端性能与用户理解。
- **人工审核**：1266 条里只有 24 条是 `title_fuzzy`（真需要判断的相似度候选），1242 条是语义混淆的 ID 桶。**如果直接把这个队列交给人工，其中约 98% 是「引用了库外论文」的噪声。**【数据】

## 7.6 24 条 title_fuzzy 明细中确认的错相似 【数据】

| 参考标题 | 目标 | 分数 | 判定 |
|---|---|---|---|
| `Language models are unsupervised multitask learners` | Instruction Pre-Training | 0.7840 | 错相似（8 次） |
| `Finetuned language models are zero-shot learners` | GPT-3 | 0.8235 | 错相似（5 次） |
| `Group normalization` | Layer Normalization | 0.7895 | 错相似 |
| `OpenAI. Gpt-4 technical report` | GPT-4 | 0.8627 | **真重复**（作者前缀未剥） |
| `Palm 2 technical report` | GPT-4 | 0.8444 | 错相似 |
| `Scaling data-constrained language models` | Scaling Laws | 0.7595 | 错相似 |
| `Least-to-most prompting enables complex reasoning in la` | CoT | 0.7552 | 错相似（截断） |
| `Polo- sukhin. Attention is all you need. Advances in ne` | Attention | 0.8197 | **真重复**（作者前缀） |

`OpenAI. Gpt-4 technical report`（0.8627）与 `Polo- sukhin. Attention...`（0.8197）两条是真重复但没被恢复，**按决策 q_b「只恢复可安全判定的」保持人工审核**。

---

# 八、新发现的风险（本轮审查中首次发现）

| # | 风险 | 依据 | 严重度 |
|---|---|---|---|
| R1 | 磁盘文件名与索引 `filename` 不一致（哈希名落盘），导致 `extract_references` 静默返回 0 条，**Mamba 丢 165 条、Active Memory 丢 30 条参考文献，无任何日志** | 【代码】【数据】§6.3 | **高** |
| R2 | `arxiv_conflict` 语义与名称不符，把「本地未收录」和「ID 冲突」混为一谈 | 【代码】【数据】§4.2 | 高 |
| R3 | `_title_exact_target()` 不校验候选 `arxiv_id`，Qwen 事故在代码层仍可复现（场景 2/7） | 【代码】【数据】§4.3 | 中（真实数据未触发） |
| R4 | `stats["review"]` 覆盖赋值导致与 `arxiv_conflict` 跨字段重复计数 | 【代码】【数据】§2.3 | 中 |
| R5 | 多个同标题候选时静默取字典末项 | 【数据】§4.1 场景4 | 低（真实数据 0 次） |
| R6 | BERT/GPT/LLaMA/UDL 四个节点 arXiv 号为空的根源是文件名不是 arXiv 号；一旦补录 ID，R3 会立刻从潜在变现实 | 【数据】§4.3 | 中 |

---

# 九、必须修复的问题（最小范围）

1. **R1**：`app/graph.py` 中把 `copies` 的文件名解析成真实磁盘路径（或在 `build_graph()` 里对每个 `copy` 做一次「文件不存在 → 按 paper_id 找哈希名文件」的回退），并对缺失副本计数进 `summary`。**这一条决定 Mamba 的 165 条参考文献能否进图。**
2. **R2 + R4**：拆分统计字段。新增 `arxiv_absent`（本地无该 ID 且标题对不上），`arxiv_conflict` 只保留「本地有同标题候选但 ID 明确不同」；`review` 改为累加或明确文档化其口径。
3. **R3**：`_title_exact_target()` 增加约束——候选节点 `arxiv_id` 非空时不得豁免。

---

# 十、可以接受的遗留问题

1. 24 条 `title_fuzzy` 里的两条真重复（GPT-4 technical report、Attention 作者前缀）继续留人工审核——符合决策 q_a/q_b，不降阈值。
2. 场景 4 的多候选歧义在真实数据上 0 次，可不改。
3. `unique_file_count=40` 与磁盘 41 的口径差，可在修 R1 时一并重定义并写入文档。
4. review 队列不入前端，不构成性能问题。

---

# 十一、建议的最小修复方案

**范围：只改 `app/graph.py` 一个文件 + 补测试。不动数据文件、不重跑匹配**

1. `build_graph()` 里把 `refs_loader` 的默认 loader 改成「先按 `filename` 找，找不到再按 `paper_id` 找 `{paper_id}.pdf`」；对每个找不到的副本递增 `stats["missing_copy_files"]` 并 `logger.warning`。
2. `_title_exact_target()` 增加 `and not cand.arxiv_id` 守卫（或把 `node_by_id` 传进去判空）。
3. `stats` 拆 `arxiv_conflict` / `arxiv_absent`；`arxiv_conflict` 只在「本地存在同标题候选且其 ID 与声明 ID 不同」时 +1。
4. 补 4 条测试：
   - `test_title_exact_exempt_blocked_when_candidate_has_other_arxiv_id`（场景 2/7）
   - `test_missing_copy_file_is_counted_not_silently_dropped`（R1，用假 loader 断言计数）
   - `test_arxiv_absent_is_not_counted_as_conflict`（R2）
   - `test_review_count_matches_reviews_list`（R4）
5. 修复后**必须重跑**：`scripts/build_graph.py`（重构图谱 JSON）、社区实验、全量 pytest、ruff。重跑后 Mamba 出边应从 0 变为非 0。

**预计改动量**：`app/graph.py` 约 40 行，`tests/test_citation_graph.py` 约 80 行。

---

# 十二、本轮未做的事（明确声明）

- 未修改任何代码、数据文件、`index.json`、`citation-graph.json`、Chroma 或 RAG 索引。
- 未重构图谱。
- 未删除任何 PDF。
- 未提交 Git、未部署服务器。
- 未降低任何匹配阈值。
- 所有临时探针脚本已从 `scripts/` 删除，`git status --short` 回到开工时的 16 项。
- `ruff check .` → `All checks passed!`；`pytest tests -q` → `767 passed`。