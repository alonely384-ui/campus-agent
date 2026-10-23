# 官方 5 问端到端验收

- 数据源：`data/papers/1706.03762v7.pdf`（单篇锁定）
- 判定口径：期望页出现在 evidence（检索正确）+ 回答引用页码与 evidence 交集非空（引用合规）

| 题号 | 问题 | 回答（截断） | 期望页 | 证据页 | 检索命中 | 引用判据 |
|---|---|---|---|---|---|---|
| Q1 | 这篇论文的第六作者是谁？他/她属于哪个单位？ | 论文的第六作者是 **Aidan N. Gomez**，单位是 **University of Toronto**（多伦多大学）。[来源: 1706.03762v7.pdf 第1页] | p1 | [1, 9, 11] | ✅ | 一致 |
| Q2 | 论文里 Adam 优化器的 beta1 和 beta2 分别取多少？ | 论文中 Adam 优化器的参数取值为：β1 = 0.9，β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。 | p7 | [2, 4, 5, 7] | ✅ | 一致 |
| Q3 | 训练用了多少块 GPU、什么型号？训练了多久？ | 根据检索到的原文：  - **GPU 数量与型号**：模型在一台配备 **8 块 NVIDIA P100 GPU** 的机器上训练 [来源: 1706.03762v7.pdf 第7页]。 - **训练时长**：   - base 模型每个训练步约耗时 **0.4 秒** [来源: 1706.03762v7.pdf 第7 | p7 | [2, 7, 8] | ✅ | 一致 |
| Q4 | 本文在 WMT 2014 英德翻译上达到多少 BLEU？比之前最好结果高多少？ | **WMT 2014 英德（EN-DE）翻译结果：**  - 本文最佳模型（Transformer (big)）达到 **28.4 BLEU**，比之前报告的最佳模型（包括 ensemble）高出 **超过 2.0 BLEU**，创下新的 state-of-the-art。[来源: 1706.03762v7.pdf 第 | p8 | [1, 8, 10] | ✅ | 一致 |
| Q5 | LayerNorm 的原始参考文献是哪一篇？ | LayerNorm 的原始参考文献是：Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. *Layer normalization*. arXiv preprint arXiv:1607.06450, 2016. [来源: 1706.03762v7.pdf 第1 | p10 | [3, 6, 10] | ✅ | 一致 |

## 人工判据（答案是否等于原文事实）

### Q1

- 期望事实：Aidan N. Gomez / University of Toronto
- 实际回答：论文的第六作者是 **Aidan N. Gomez**，单位是 **University of Toronto**（多伦多大学）。[来源: 1706.03762v7.pdf 第1页]
- 引用判据：一致（引用页码 [1] 在证据里）

### Q2

- 期望事实：β1 = 0.9, β2 = 0.98, ε = 1e-9（原文 p7）
- 实际回答：论文中 Adam 优化器的参数取值为：β1 = 0.9，β2 = 0.98，ϵ = 10⁻⁹ [来源: 1706.03762v7.pdf 第7页]。
- 引用判据：一致（引用页码 [7] 在证据里）

### Q3

- 期望事实：8 × NVIDIA P100，3.5 天
- 实际回答：根据检索到的原文：

- **GPU 数量与型号**：模型在一台配备 **8 块 NVIDIA P100 GPU** 的机器上训练 [来源: 1706.03762v7.pdf 第7页]。
- **训练时长**：
  - base 模型每个训练步约耗时 **0.4 秒** [来源: 1706.03762v7.pdf 第7页]；
  - big 模型训练耗时 **3.5 天**（8 块 P100 GPU）[来源: 1706.03762v7.pdf 第8页]；
  - 论文还提到，在 8 块 P100 GPU 上训练 **12 小时**即可达到新的翻译质量 state of the art [来源: 1706.03762v7.pdf 第2页]。
- 引用判据：一致（引用页码 [2, 7, 8] 在证据里）

### Q4

- 期望事实：28.4，比之前最好高 2.0 以上
- 实际回答：**WMT 2014 英德（EN-DE）翻译结果：**

- 本文最佳模型（Transformer (big)）达到 **28.4 BLEU**，比之前报告的最佳模型（包括 ensemble）高出 **超过 2.0 BLEU**，创下新的 state-of-the-art。[来源: 1706.03762v7.pdf 第8页]
- 摘要中也给出同一数字：模型在 WMT 2014 English-to-German 任务上取得 28.4 BLEU，比现有最佳结果（含 ensembles）提升 over 2 BLEU。[来源: 1706.03762v7.pdf 第1页]
- 补充：Table 2 中 Transformer (base model) 的 EN-DE BLEU 为 27.3，Transformer (big) 为 28.4；训练成本分别为 3.3·10^18 与 2.3·10^19 FLOPs。[来源: 1706.03762v7.pdf 第8页]

另外，论文还提到 base model 就已超过此前所有已发表模型和 ensemble，且训练成本只是竞争模型的一小部分。[来源: 1706.03762v7.pdf 第8页]
- 引用判据：一致（引用页码 [1, 8] 在证据里）

### Q5

- 期望事实：Ba, Kiros & Hinton (2016) Layer Normalization
- 实际回答：LayerNorm 的原始参考文献是：Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. *Layer normalization*. arXiv preprint arXiv:1607.06450, 2016. [来源: 1706.03762v7.pdf 第10页]
- 引用判据：一致（引用页码 [10] 在证据里）

## 检索容错（2026-10-23 第三轮）

- `retrieve_scored` 三路召回各自 `try/except`：哈希 / 稠密（ONNX）/ BM25，任一路失败只跳过该路并记 `logger.exception`，其余通道继续 RRF 融合；全空才返回 `[]`。
- 权重接管：稠密不可用时哈希从 0.5 提升到 2.0（离线兜底），BM25 保持 1.0。
- 集合缺失时用 `get_collection` + warning 返回空，不再 `get_or_create` 静默建空集合。
- 测试：`tests/test_retrieval_fallback.py` 4 例（稠密缺席权重接管、三路融合排序、全空、稠密抛错仍返回哈希结果）；全量 `68 passed`，ruff 通过。
- 复跑官方 5 问：检索 5/5、引用 5/5，答案与上一轮一致。
