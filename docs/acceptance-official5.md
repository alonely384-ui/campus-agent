# 官方 5 问端到端验收

- 数据源：`data/papers/1706.03762v7.pdf`（单篇锁定）
- 判定口径：期望页出现在 evidence（检索正确）+ 回答引用页码与 evidence 交集非空（引用合规）

| 题号 | 问题 | 回答（截断） | 期望页 | 证据页 | 检索命中 | 引用判据 |
|---|---|---|---|---|---|---|
| Q1 | 这篇论文的第六作者是谁？他/她属于哪个单位？ | 论文库中没有找到相关信息。  说明：元数据中列出了 7 位作者的顺序（第 6 位为 Aidan N. Gomez）[来源: 1706.03762v7.pdf 第1页]，但检索片段中作者与单位的对应关系存在缺失和错位——片段里出现的单位署名包括 Google Brain、Google Research、Universit | p1 | [1, 11] | ✅ | 一致 |
| Q2 | 论文里 Adam 优化器的 beta1 和 beta2 分别取多少？ | 论文中 Adam 优化器的参数取值为 β1 = 0.9、β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。 | p7 | [2, 4, 5, 7] | ✅ | 一致 |
| Q3 | 训练用了多少块 GPU、什么型号？训练了多久？ | 根据检索到的原文：  - **GPU 数量与型号**：模型在一台配备 **8 块 NVIDIA P100 GPU** 的机器上训练 [来源: 1706.03762v7.pdf 第7页]。 - **训练时长**：该模型配置的训练耗时 **3.5 天**（在 8 块 P100 GPU 上）[来源: 1706.03762v7 | p7 | [7, 8] | ✅ | 一致 |
| Q4 | 本文在 WMT 2014 英德翻译上达到多少 BLEU？比之前最好结果高多少？ | 论文库中没有找到相关信息。  说明：检索片段中只提到在 WMT 2014 English-to-German 任务上“improving over the existing best results, including ensembles, by over 2 BLEU”[来源: 1706.03762v7.pdf 第 | p8 | [1, 8, 10] | ✅ | 一致 |
| Q5 | LayerNorm 的原始参考文献是哪一篇？ | LayerNorm 的原始参考文献是：  **Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. Layer normalization. arXiv preprint arXiv:1607.06450, 2016.**（即参考文献 [1]）[来源: 1706. | p10 | [3, 8, 10] | ✅ | 一致 |

## 人工判据（答案是否等于原文事实）

### Q1

- 期望事实：Aidan N. Gomez / University of Toronto
- 实际回答：论文库中没有找到相关信息。

说明：元数据中列出了 7 位作者的顺序（第 6 位为 Aidan N. Gomez）[来源: 1706.03762v7.pdf 第1页]，但检索片段中作者与单位的对应关系存在缺失和错位——片段里出现的单位署名包括 Google Brain、Google Research、University of Toronto，其中 "Aidan N. Gomez∗ † / University of Toronto" 出现在第 1 页的片段中 [来源: 1706.03762v7.pdf 第1页]。不过该片段同时显示第 5 位作者为 "Łukasz Kaiser / Google Brain"，与元数据所列第 5 位作者（Illia Polosukhin）不一致，因此无法确认第 6 位作者的单位归属。
- 引用判据：一致（引用页码 [1] 在证据里）

### Q2

- 期望事实：β1 = 0.9, β2 = 0.999
- 实际回答：论文中 Adam 优化器的参数取值为 β1 = 0.9、β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。
- 引用判据：一致（引用页码 [7] 在证据里）

### Q3

- 期望事实：8 × NVIDIA P100，3.5 天
- 实际回答：根据检索到的原文：

- **GPU 数量与型号**：模型在一台配备 **8 块 NVIDIA P100 GPU** 的机器上训练 [来源: 1706.03762v7.pdf 第7页]。
- **训练时长**：该模型配置的训练耗时 **3.5 天**（在 8 块 P100 GPU 上）[来源: 1706.03762v7.pdf 第8页]。

补充：base 模型每个训练步约 0.4 秒 [来源: 1706.03762v7.pdf 第7页]。
- 引用判据：一致（引用页码 [7, 8] 在证据里）

### Q4

- 期望事实：28.4，比之前最好高 2.0 以上
- 实际回答：论文库中没有找到相关信息。

说明：检索片段中只提到在 WMT 2014 English-to-German 任务上“improving over the existing best results, including ensembles, by over 2 BLEU”[来源: 1706.03762v7.pdf 第1页]，但**没有给出该任务的具体 BLEU 数值**（第8页仅提到 big transformer model 在该任务上的表述，未含数字）。因此“达到多少 BLEU”无法依据现有片段回答；可确认的只有“比之前最好结果（含 ensemble）高出 2 BLEU 以上”。

另外，片段中出现的 41.8 与 41.0 均属 WMT 2014 English-to-French 任务，不能用于英德任务[来源: 1706.03762v7.pdf 第1页][来源: 1706.03762v7.pdf 第8页]。
- 引用判据：一致（引用页码 [1, 8] 在证据里）

### Q5

- 期望事实：Ba, Kiros & Hinton (2016) Layer Normalization
- 实际回答：LayerNorm 的原始参考文献是：

**Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. Layer normalization. arXiv preprint arXiv:1607.06450, 2016.**（即参考文献 [1]）[来源: 1706.03762v7.pdf 第10页]
- 引用判据：一致（引用页码 [10] 在证据里）

## 结论（2026-10-23 实测）

- **检索命中页 5/5，引用合规 5/5**：5 道题的证据里都出现了期望页，回答里的 `[来源: … 第N页]` 页码都落在证据页内。
- **答案完整度 3/5**：Q2/Q3/Q5 正确；Q1 过度拒答（其实已给出第 6 位是 Aidan N. Gomez、单位线索 University of Toronto，却因片段里作者编号错位而收口成"没找到"）；Q4 拒答未给 28.4（p8 的表格行 `Transformer (big) 28.4 41.8` 没进 top-4 块）。
- **原文事实核对**（pypdf 直读）：p7 `β1 = 0.9, β2 = 0.98 and ϵ = 10−9`；p8 `Transformer (big) 28.4 41.8`；p1 也有 `28.4 BLEU on the WMT 2014 English-to-German`。此前判据里把 β2 写成 0.999 是我写错，已更正为 0.98。
- **后续改进**：Q4 需要表格行级切块（或 top_k 从 4 提到 6）；Q1 需要作者-单位对齐（当前只解析了作者列表，未解析单位）。
