# 官方 5 问端到端验收

- 数据源：`data/papers/1706.03762v7.pdf`（单篇锁定）
- 判定口径：期望页出现在 evidence（检索正确）+ 回答引用页码与 evidence 交集非空（引用合规）

| 题号 | 问题 | 回答（截断） | 期望页 | 证据页 | 检索命中 | 引用判据 |
|---|---|---|---|---|---|---|
| Q1 | 这篇论文的第六作者是谁？他/她属于哪个单位？ | 论文的第六作者是 Aidan N. Gomez，单位是 University of Toronto。[来源: 1706.03762v7.pdf 第1页]  补充说明：第 1 页作者列表中第 7 位出现的是 Łukasz Kaiser（Google Brain），而元数据中列出的 7 人名单里没有 Łukasz Kais | p1 | [1, 9, 11] | ✅ | 一致 |
| Q2 | 论文里 Adam 优化器的 beta1 和 beta2 分别取多少？ | 论文中 Adam 优化器的参数取值为：β1 = 0.9，β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。 | p7 | [2, 4, 5, 7] | ✅ | 一致 |
| Q3 | 训练用了多少块 GPU、什么型号？训练了多久？ | 根据检索到的原文：  **GPU 数量与型号**：模型训练在一台配备 **8 块 NVIDIA P100 GPU** 的机器上进行。[来源: 1706.03762v7.pdf 第7页]  **训练时长**： - base 模型每个训练步约耗时 **0.4 秒**。[来源: 1706.03762v7.pdf 第7页] - | p7 | [2, 7, 8] | ✅ | 一致 |
| Q4 | 本文在 WMT 2014 英德翻译上达到多少 BLEU？比之前最好结果高多少？ | **WMT 2014 英德（EN-DE）翻译结果：**  - **BLEU 分数：28.4**（Transformer (big) 模型）。摘要中写明 "Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation task" [来 | p8 | [1, 8, 10] | ✅ | 一致 |
| Q5 | LayerNorm 的原始参考文献是哪一篇？ | LayerNorm 的原始参考文献是：  Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. **Layer normalization**. arXiv preprint arXiv:1607.06450, 2016. [来源: 1706.03762v7.pd | p10 | [3, 6, 10] | ✅ | 一致 |

## 人工判据（答案是否等于原文事实）

### Q1

- 期望事实：Aidan N. Gomez / University of Toronto
- 实际回答：论文的第六作者是 Aidan N. Gomez，单位是 University of Toronto。[来源: 1706.03762v7.pdf 第1页]

补充说明：第 1 页作者列表中第 7 位出现的是 Łukasz Kaiser（Google Brain），而元数据中列出的 7 人名单里没有 Łukasz Kaiser、却包含 Illia Polosukhin，两处名单不一致，因此“第六作者”的判定依赖你采用哪份名单。按第 1 页正文顺序，第六位为 Aidan N. Gomez。
- 引用判据：一致（引用页码 [1] 在证据里）

### Q2

- 期望事实：β1 = 0.9, β2 = 0.98, ε = 1e-9（原文 p7）
- 实际回答：论文中 Adam 优化器的参数取值为：β1 = 0.9，β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。
- 引用判据：一致（引用页码 [7] 在证据里）

### Q3

- 期望事实：8 × NVIDIA P100，3.5 天
- 实际回答：根据检索到的原文：

**GPU 数量与型号**：模型训练在一台配备 **8 块 NVIDIA P100 GPU** 的机器上进行。[来源: 1706.03762v7.pdf 第7页]

**训练时长**：
- base 模型每个训练步约耗时 **0.4 秒**。[来源: 1706.03762v7.pdf 第7页]
- big 模型（Transformer (big)）训练耗时 **3.5 天**（8 块 P100 GPU）。[来源: 1706.03762v7.pdf 第8页]
- 论文还提到，在 8 块 P100 GPU 上训练仅 **12 小时**即可达到新的翻译质量 SOTA。[来源: 1706.03762v7.pdf 第2页]

注：原文片段未给出 base 模型的总训练时长（仅给出每步 0.4 秒），也未说明 12 小时对应的是哪个具体模型配置。
- 引用判据：一致（引用页码 [2, 7, 8] 在证据里）

### Q4

- 期望事实：28.4，比之前最好高 2.0 以上
- 实际回答：**WMT 2014 英德（EN-DE）翻译结果：**

- **BLEU 分数：28.4**（Transformer (big) 模型）。摘要中写明 "Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation task" [来源: 1706.03762v7.pdf 第1页]；第 8 页正文与 Table 2 也给出同一数值 28.4 [来源: 1706.03762v7.pdf 第8页]。
- **提升幅度：超过 2.0 BLEU**（相对此前已报告的最好模型，且包含 ensemble 模型）。原文："outperforms the best previously reported models (including ensembles) by more than 2.0 BLEU, establishing a new state-of-the-art BLEU score of 28.4" [来源: 1706.03762v7.pdf 第8页]；摘要表述为 "improving over the existing best results, including ensembles, by over 2 BLEU" [来源: 1706.03762v7.pdf 第1页]。

补充：Table 2 中此前最好的 ensemble 模型为 ConvS2S Ensemble，EN-DE BLEU 26.36 [来源: 1706.03762v7.pdf 第8页]。

（另注：摘要提到英法任务为 41.8 BLEU，而第 8 页正文写 "our big model achieves a BLEU score of 41.0"，两处数值不一致，如需引用英法结果建议核对原文。）
- 引用判据：一致（引用页码 [1, 8] 在证据里）

### Q5

- 期望事实：Ba, Kiros & Hinton (2016) Layer Normalization
- 实际回答：LayerNorm 的原始参考文献是：

Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. **Layer normalization**. arXiv preprint arXiv:1607.06450, 2016. [来源: 1706.03762v7.pdf 第10页]
- 引用判据：一致（引用页码 [10] 在证据里）

## 结论（2026-10-23 第二轮：表格切块 + 页级兜底后）

- **检索命中页 5/5，引用合规 5/5，完整作答 5/5**（上一轮为 3/5）。
- Q1 第六作者 → `Aidan N. Gomez / University of Toronto`（p1）；Q4 → `28.4 BLEU`，并指出摘要 41.8 与正文 41.0 的英法数值差异。
- 改动一：`split_text` 增加表格感知（连续数字行 + 表头合并为一组，分组时重复表头），实测 p8 的 `Transformer (big) 28.4 41.8` 与 `BLEU` 表头同块。
- 改动二：数值类问题（`_needs_page_text`）把命中前 2 页的整页原文补进上下文。
- 数据变化：v7 单篇重建 180 → 184 块；BM25 总块 25953 → 25957。
- 测试：`64 passed`；`ruff check app tests scripts` → All checks passed。
