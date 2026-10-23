# 开发日志（PROGRESS）

> 每天一条，记录**做了什么 / 证据 / 踩的坑 / 下一步**。
> 这是提交历史之外的第二份“过程证据”，对应赛题对“完整的 Git 提交历史、体现真实开发过程”的要求。

---

## 2026-10-20（周二）

- 环境从零修好：Python 3.12.10（winget 用户级安装）、Git 2.55.0.5、`.venv` 重建、依赖装齐。
- 证据：`pytest -q` → `25 passed in 2.36s`；`/health` → `{"status":"ok","model":"deepseek-chat","version":"0.1.0"}`；服务监听 `127.0.0.1:8000`。
- 坑：环境变量 `HTTP_PROXY` 指向 `127.0.0.1:65532`，梯子没开时 pip 报 `ProxyError('Cannot connect to proxy.')`；清掉四个代理变量即可直连 PyPI。

## 2026-10-21（周三）

- 实现 RAG 管道四件套：`load_text` / `split_text`（500 字一段、重叠 50）/ `index_chunks`（Chroma 入库）/ `retrieve`（top_k=4）。
- 自建脱敏样例 `data/uploads/培养方案样例.txt`，10 块入库，3 个问题 top1 全中。
- 发现：简版 2-gram 向量是**字面相似**不是语义相似（“助学金”命中“奖学金”段 0.267，“转专业”段 0.261），必须加相似度阈值做拒答。
- PyCharm Community 2025.2.6.1 装好，解释器自动识别 `.venv`。

## 2026-10-22（周四）

- 论文库完成：`app/papers.py`（上传 / 列表 / 删除 / 内容查重）+ `GET /papers`、`POST /papers/upload`、`DELETE /papers/{paper_id}`；新增 8 个测试，全量 `33 passed`。
- 真实查重验证：v7 与 v1 前 3 页相似度 **0.873**，判重命中 `bdfaa68d8984`。
- 外壳改为科研小助手：`SYSTEM_PROMPT` 加「必须标来源 / 查不到就拒答 / 拒绝编造」规则，前端标题与开场白同步；README 补依赖与许可表。
- 学习：看了约 4 小时 RAG 课程，**实战部分还没看完**。
- 缴费：**还没缴，正在凑钱**。硬提醒：预指导要求 **10/31 15:00 前**完成缴费。
- Git：本地 5 个提交推送到 GitHub（`66f9878..827773b`），SSH 密钥配置完成。

## 2026-10-22（周四·晚）

- 检索改造：`embed` 从「一律按相邻两字（英文碎成 tr/ra/ai）」改为「中文 2-gram + 英文整词 + 希腊字母归一（β→beta）+ 极简词形还原（training/trained→train）+ IDF 加权」，维度 256→1024。
- 新增 BM25 倒排索引（`data/chroma/bm25.json`）与 RRF 混合检索：向量召回 + BM25 精排，`retrieve(..., source=...)` 支持单篇过滤。
- RAG 正式接入 Agent：`_rag_context` 把命中片段注入系统消息，prompt 要求标注 `[来源: 文件名 第N页]`、查不到就答「论文库中没有找到相关信息」。
- 证据：`45 passed in 0.96s`；`ruff check` 全绿；全量 39 篇重建 **15048 块 / 113 秒**；官方 4 问单篇检索——Adam β1β2 命中第 7 页 ✓、BLEU 命中第 8 页 ✓，GPU 数量（第 2/8/1 页）与第六作者（第 10 页）仍错。
- 坑：pytest 的 `Select-Object -First` 会提前掐断上游进程，把建库跑到一半杀掉（库变 0 块）；正确做法是先把输出落文件再读。
- 下一步：句子级切块 + 邻块扩展修「答案句被 500 字切断」；结构化解析作者列表修「第六作者」这类问题。

## 2026-10-23（周五）

- 只读验证（独立副本 `_verify/data-copy`，正式库 sha256 前后不变）：
  - **R5 空检索幻觉：不成立**。5 个库外问题 × 有无单篇过滤，共 10 次，全部回答“论文库中没有找到相关信息”，无编造事实、无假引用。对照组（第六作者）能答对，但引用写成 `[来源: 1706.03762v1.pdf 本篇元数据]`（无页码）→ 暴露元数据引用缺页码。
  - **P0-2 删除后仍可检索：成立**。副本里删掉 `bdfaa68d8984`（180 块）后，`docs`/`docs_dense` 仍是 26120、`bm25.json` 仍是 26120、用新进程检索逐位同分（0.05698 / 0.056336 / 0.056068）→ 确认 `delete_paper` 不碰索引。
- 修复：
  - `pipeline.py` 新增 `add_chunks` / `remove_source` / `index_paper` / `_merge_bm25` / `_prune_bm25` / `_dfs_from_postings`；上传即入库，删除按 `source` 清 Chroma 两集合 + BM25 + IDF，删块后重编 `doc_id` 防止 `lens` 错位。
  - `papers.py` 新增 `delete_structure`；`main.py` 上传返回 `indexed`、删除返回 `removed_chunks`；`agent.py` 元数据行改为 `【本篇元数据｜第1页】` 并提示引用标第 1 页。
  - 清理真实数据不一致：`remove_source("1706.03762v1.pdf")` 删 167 块 + `delete_structure` → 三处索引 26120→**25953**、`structures.json` 40→39、38 个来源全部在磁盘上。
- 证据：`58 passed in 3.48s`；`ruff check` 全绿；端到端上传 `e2e-test.pdf` → `indexed=167`、可检索（p7 Adam 0.0571），删除 → `removed_chunks=167`、检索返回 0；清理后基线 `Recall@1 80% / @3 100% / @5 100%`；第六作者回答引用 `[来源: 1706.03762v7.pdf 第1页]`。
- 备份：清理前快照 `D:\alonely\deepseek\_verify\backup-reconcile-20261023-0915`（chroma.sqlite3 165.5MB、bm25.json 23.4MB、idf.json 13.5MB、index/structures）。
- 下一步：官方 5 问端到端验收表；公网地址 + 5–8 分钟视频 + 技术文档 PDF。

## 2026-10-23（周五·晚）

- **L2 多篇对比落地**：
  - `app/agent.py`：新增 `PER_PAPER_TOP_K = 3`、`MULTI_MAX_EVIDENCE = 8`、`_merge_scored()`（按 `(来源, 页码, 正文前 80 字)` 去重、按融合分降序、截断 8 条）；`_rag_context_with_evidence(question, source, sources)` 在 `sources` 有 ≥2 篇时逐篇检索再合并，并注入“本次检索覆盖 N 篇论文，属于多篇对比”的提示；多篇时不注入单篇元数据（防张冠李戴）。
  - `SYSTEM_PROMPT` 规则 6：两篇以上时先给 Markdown 对比表（第一列维度、每列一篇论文），每格末尾标 `[来源: 文件名 第N页]`，表后用【结论】写差异。
  - `app/main.py`：`ChatRequest.sources`，`/chat` 与 `/chat/stream` 都透传。
  - `web/index.html`：下拉框换成勾选列表（0 篇=全库、1 篇=单篇、≥2 篇=对比）；新增 `looksLikeRow`/`buildTable`/`renderAnswer`，把模型输出的 Markdown 表格渲染成真表格（单元格用 `textContent` 防注入）。
- 证据：`71 passed in 3.61s`、`ruff check app tests` 全绿；两篇（`1706.03762v7.pdf` + `2005.14165v4.pdf`）实测 `/chat` → 证据 6 条、来源两篇、回答含对比表与四段结构；单篇回归 4 条证据（第 2/4/5/7 页）；流式 753 个事件、1 个 evidence 事件（6 条）、回答 1475 字含表格。
- 提交 `d7268d5 feat(L2): 多篇对比——每篇各检索、合并去重、前端勾选与对比表渲染`，已 push。
- 未验证：浏览器里对比表的实际视觉效果（只核对了页面元素与接口返回，没有人工点页面）。

## 2026-10-24（周六）重排（Cross-Encoder）落地

- 新增 `app/rag/rerank.py`：交叉编码器 `ms-marco-MiniLM-L-6-v2`（ONNX 量化 22.1MB + `tokenizer.json`，放 `data/models/`，已进 `.gitignore`）；懒加载，`score_pairs()` 返回 logits，`rerank()` 按分数重排并追加 `rerank` 通道；模型缺失抛 `RerankUnavailable`。
- `app/rag/pipeline.py`：新增 `_RERANK_ENABLED`（`RAG_RERANK=0` 关闭）、`_RERANK_POOL=15`；`retrieve_scored` 先融合取 15 个候选、再重排取 top_k；重排不可用时自动回退融合顺序并记 warning。
- `app/agent.py`：`Evidence.to_dict()` 的 `score_type` 在通道含 `rerank` 时输出 `"rerank"`。
- 证据（38 题 / 3 篇，`scripts/eval_rerank_ab.py`）：关重排 R@1 25/38、R@3 34/38、R@5 34/38；开重排 R@1 **30/38**、R@3 35/38、R@5 36/38。pool=10/15/20 时 R@1 为 28/31/30（差异在噪声内），取 15。
- 延迟（5 个查询，单篇）：中位 93ms → 338ms（+245ms，15–20 个候选的 CPU 推理）。
- 端到端：`/chat` GPU 题 evidence `#1 p7 score=4.91676 type=rerank channels=[dense, hash, bm25, rerank]`，回答 `【结论】训练使用 1 台机器、8 块 NVIDIA P100 GPU。[来源: 1706.03762v7.pdf 第7页]`。
- `75 passed`、`ruff check app tests scripts` 全绿。

## 2026-10-24（下午·修）

修的是"切块把词切断"和"检索对了但答不出来"两类硬问题：

- `app/rag/pipeline.py`：`_overlap_tail` 不再从半个单词取重叠（此前块首出现 `nguage model`、`eatly improves`）；`_RECALL_DEPTH` 可配（默认 50）。
- `app/agent.py`：数值类问题补页文本 2 页 → 3 页，且改为**按"含数字的行"取样**（`_numeric_excerpt`，单页上限 1600 字）——实测 `61.4% accuracy` 在第 1980 个字符，按字符截会切掉。
- `data/eval/qa_multi.json`：2 道 GPT-3 参数题加 `pages_alt: [8]`（p8 原句同样直接回答），评测脚本合并 `pages` 与 `pages_alt`。
- 新索引（修好切分后重建）：`论文数: 40，切块大小: 300`、`切块: 32182`、`入库: 25995 / 453s`；旧索引移出仓库到 `D:\alonely\deepseek\_verify\chroma_old_20261024`。
- 评测（新索引，38 题 / 3 篇）：关重排 R@1 **28/38**（旧索引 25）、R@3 32/38；开重排 R@1 **30/38**、R@3 35/38、R@5 36/38。
- 端到端抽查（此前检索未中的题）：`训练 GPT-3 实际用了多少 token？` → 300B（第 9 页）；`FlashAttention 在长序列上的 accuracy 是多少？` → 61.4%（Path-X，第 1 页）；`GPT-3 一共训了几个不同规模的模型？` → 仍答"没有明确总数"（p8 Table 2.1 可数出 8 个规模，属表格计数缺口）。
- 提交：`78dc127`、`bdcbd2e`、`ca13284`、`e59bde1`；`77 passed`、ruff 全绿。
- 剩余 8 题未中 top1 的归因：①同一事实多页出现（token 数 p9/p43），"标准页"本身有歧义；②表格内计数需跨行数；③块跨页（BERT head dimension p28/p29）。

## 2026-10-24（周六·晚）表格计数

修的是"问一共几个/几种，答案在表格里"这一类问题：

- 现象：`GPT-3 一共训了几个不同规模的模型？` 答"没有明确总数"，证据页 `[9,2,6,43]`，p8（Table 2.1，恰好 8 行）从未进证据。
- `app/agent.py` 新增：
  - `_is_count_question()`：只认 `几个/几种/几类/几款/多少个/多少种/多少类/多少款`；`多少块 GPU` 这类问数值的不算。
  - `_table_pages(source, question, top_n=3)`：按"含问题术语且像表格行"的行数给页打分，返回候选页；**表题命中 `sizes/parameters/architectures` 时 +20**（p8 表题 `Table 2.1: Sizes, architectures...` 因此从第 3 升到第 1）；只扫表题行，扫整页会被正文里的 parameters 误伤（实测 p13/p18/p26 各多算 10 分）。
  - `_rag_context_with_evidence`：数量类单篇问题时把候选表格页（每页 2400 字）补进上下文，**并把这几页追加进 evidence**（`channels=["table"]`、`score=0.0`），否则回答引第 8 页而证据卡片里没有。
  - `SYSTEM_PROMPT`【依据】加规则：先逐行列条目再给总数，并说明数的是哪一列。
- 实测：`_table_pages` 结果 `[(8, 29), (26, 16), (13, 11)]`；`/chat` 回答 `GPT-3 一共训练了 8 个不同规模的模型…`，证据 `[9,2,6,43,8(table),26(table),13(table)]`；回归 `训练用了多少块 GPU？`→ p7、`食堂三楼今天供应什么菜？`→ 拒答。
- 新增 `tests/test_table_count.py` 4 例（数量词识别、表题加权排序、无术语返回空、注入上下文且 evidence 带表格页）。
- `81 passed`、`ruff check app tests` 全绿。

## 下一步

1. 官方 5 问端到端验收（第 6 作者 / Adam β1β2 / GPU 数量型号 / BLEU 对比 / LayerNorm 引用），记录页码与引用格式
2. 公网演示地址（隧道或云平台）+ 技术文档 PDF（≤30 页）+ 5–8 分钟视频
3. 多篇对比问答、阅读页与引用图谱（P1/P2）
4. 缴费：10/31 15:00 前必须完成（预指导门槛）
