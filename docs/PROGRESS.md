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

## 2026-10-23（周五·晚）重排（Cross-Encoder）落地

- 新增 `app/rag/rerank.py`：交叉编码器 `ms-marco-MiniLM-L-6-v2`（ONNX 量化 22.1MB + `tokenizer.json`，放 `data/models/`，已进 `.gitignore`）；懒加载，`score_pairs()` 返回 logits，`rerank()` 按分数重排并追加 `rerank` 通道；模型缺失抛 `RerankUnavailable`。
- `app/rag/pipeline.py`：新增 `_RERANK_ENABLED`（`RAG_RERANK=0` 关闭）、`_RERANK_POOL=15`；`retrieve_scored` 先融合取 15 个候选、再重排取 top_k；重排不可用时自动回退融合顺序并记 warning。
- `app/agent.py`：`Evidence.to_dict()` 的 `score_type` 在通道含 `rerank` 时输出 `"rerank"`。
- 证据（38 题 / 3 篇，`scripts/eval_rerank_ab.py`）：关重排 R@1 25/38、R@3 34/38、R@5 34/38；开重排 R@1 **30/38**、R@3 35/38、R@5 36/38。pool=10/15/20 时 R@1 为 28/31/30（差异在噪声内），取 15。
- 延迟（5 个查询，单篇）：中位 93ms → 338ms（+245ms，15–20 个候选的 CPU 推理）。
- 端到端：`/chat` GPU 题 evidence `#1 p7 score=4.91676 type=rerank channels=[dense, hash, bm25, rerank]`，回答 `【结论】训练使用 1 台机器、8 块 NVIDIA P100 GPU。[来源: 1706.03762v7.pdf 第7页]`。
- `75 passed`、`ruff check app tests scripts` 全绿。

## 2026-10-23（周五·深夜）修切分与摘录

修的是"切块把词切断"和"检索对了但答不出来"两类硬问题：

- `app/rag/pipeline.py`：`_overlap_tail` 不再从半个单词取重叠（此前块首出现 `nguage model`、`eatly improves`）；`_RECALL_DEPTH` 可配（默认 50）。
- `app/agent.py`：数值类问题补页文本 2 页 → 3 页，且改为**按"含数字的行"取样**（`_numeric_excerpt`，单页上限 1600 字）——实测 `61.4% accuracy` 在第 1980 个字符，按字符截会切掉。
- `data/eval/qa_multi.json`：2 道 GPT-3 参数题加 `pages_alt: [8]`（p8 原句同样直接回答），评测脚本合并 `pages` 与 `pages_alt`。
- 新索引（修好切分后重建）：`论文数: 40，切块大小: 300`、`切块: 32182`、`入库: 25995 / 453s`；旧索引移出仓库到 `D:\alonely\deepseek\_verify\chroma_old_20261024`。
- 评测（新索引，38 题 / 3 篇）：关重排 R@1 **28/38**（旧索引 25）、R@3 32/38；开重排 R@1 **30/38**、R@3 35/38、R@5 36/38。
- 端到端抽查（此前检索未中的题）：`训练 GPT-3 实际用了多少 token？` → 300B（第 9 页）；`FlashAttention 在长序列上的 accuracy 是多少？` → 61.4%（Path-X，第 1 页）；`GPT-3 一共训了几个不同规模的模型？` → 仍答"没有明确总数"（p8 Table 2.1 可数出 8 个规模，属表格计数缺口）。
- 提交：`78dc127`、`bdcbd2e`、`ca13284`、`e59bde1`；`77 passed`、ruff 全绿。
- 剩余 8 题未中 top1 的归因：①同一事实多页出现（token 数 p9/p43），"标准页"本身有歧义；②表格内计数需跨行数；③块跨页（BERT head dimension p28/p29）。

## 2026-10-23（周五·深夜）表格计数

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

## 2026-10-24（周六）学习材料站点与保活

- 做了本地学习站点：`web/project-tour.html`（PyCharm 风格：文件树 + 行号 + 语法高亮，60 个文件段落），生成脚本放 `_verify/build_tour.py`，页面写进 `.git/info/exclude` 不入库。
- 加了 12 步学习路线 + 28 道自测题 + 视频清单 + 4 天排期；另做 `scripts/check_all.py`（8 项体检）与 `scripts/keep_alive.ps1`（uvicorn + 隧道自愈，开机自启）。
- 证据：`check_all.py` 8 项全 PASS；`81 passed in 4.30s`；提交 `360296b`、`db834c0`。

## 2026-10-25（周日）学习测验与隧道故障排查

学习者侧（自述，不算代码变更）：

- 学习测验做到第 11 题；自己动手跑过 FastAPI 流式接口 `/chat/stream`（能收到事件流）、论文去重（v1/v7 相似度 0.873）、Function Calling（calculator 工具调用）。
- 结论：RAG 七步与 FastAPI 请求链路到了"能讲清、能跑通"的程度，后面改成在项目里改参数、看效果，不再新看视频。

维护侧：

- 现象：手机端页面打不开；日志里 `cloudflared` 在 20:40–20:43 每分钟退出一次，而 uvicorn 一直正常（8000 监听、`/health` 200）。
- 根因一：保活脚本只在 `logs/cloudflared.log`（stdout）找隧道地址，cloudflared 实际把地址打在 **stderr** → `_public/public-url.txt` 一直停在旧地址。
- 根因二：quick tunnel 重连后地址必换。
- 修复：`scripts/keep_alive.ps1` 改为同时扫 stdout/stderr、启动加 `--protocol http2`、失败时记录 stderr 尾巴；手动起隧道后公网 `/health` 200、页面 200（1398 KB），新地址写入 `_public/public-url.txt`。
- 遗留：quick tunnel 地址不稳定，评审期要换学生机或命名隧道。

## 2026-10-27（周二）网页版多轮记忆修复（前端）

- 现象：连续追问时模型"失忆"，上一轮的建议接不上。
- 根因（代码行）：后端早已支持——`app/main.py:75`/`:108` 把 `req.history` 传给 Agent，`app/agent.py:571-572`/`:668-669` 用 `messages.extend(history)` 拼进对话；但 `web/index.html` 每轮都发 `history: []`，等于把记忆这一半关掉了。
- 修复（只改前端）：`web/index.html` 维护 `history`（最近 6 轮 / 单条截断 1000 字），每轮随请求发出，成功回答后入栈；存 `localStorage['campus-agent-history-v1']`，刷新恢复；新增「新对话」按钮清空。后端零改动。
- 顺带修 `scripts/check_all.py`：页面检查由"只看 200"改成"200 + 关键标记存在"（`/ui/` 查 `科研小助手`，tour 页查 `project-tour`），此前 `key` 变量未使用被 ruff 报 B007。
- 证据：`/ui/` 返回 15774 字节且含 `campus-agent-history-v1`/`history.slice()`/`newChat`；`node --check` 对抽出的 `<script>` 退出码 0；`81 passed in 3.62s`；`ruff check app tests scripts` → All checks passed。

## 2026-10-27（周二）全库清单：让"一共有多少篇"答得对（A3）

- 现象（复现原文）：问「这里一共有多少篇论文？」，答「检索片段中一共出现 4 篇论文：LLaMA、2001.08361v1、2005.14165v4、333078981_…_n.pdf」——把 top-4 片段当成了全库。
- 根因：检索只回 top-k 片段，模型没有"全库视角"；`data/papers` 有 40 个 PDF，但任何时候进上下文的只有 4–8 个片段。
- 修复：
  - 新增 `scripts/build_catalog.py` → 生成 `data/catalog.json`（40 条：文件名/标题/页数/arXiv 编号/年份/类别/是否已索引），标题来自 `app/structure.py` 的版式解析，是否已索引由 `data/chroma/bm25.json` 的来源集合判定。
  - `app/agent.py` 新增 `load_catalog()`（按 mtime 缓存）、`_is_catalog_question()`、`_catalog_block()`（篇数 + 年份分布 + 类别分布 + 前 10 个标题，≤1200 字）；全库类问题注入清单，并追加一条 `channels=["catalog"]` 的证据；SYSTEM_PROMPT 加规则 7（必须依据清单，不许用片段数推断；"未分类"要如实说）。
  - 检索为空时也能回答篇数（不再直接返回空上下文）。
- 证据：`85 passed in 3.40s`；`ruff check app tests scripts` → All checks passed；e2e「这里一共有多少篇论文？」→「共 40 篇（其中 39 篇已建立检索索引…）」，证据首条 `channels=['catalog']`；「这些论文都是什么类型？」→ 如实答"40 篇全部未分类，暂无分类元数据"；38 题回归 **R@1 30/38、R@3 35/38、R@5 36/38，与改前一致（无退化）**。
- 下一步（B1+B2）：用 arXiv API 给 34 篇补 `primary_category`，剩下 6 篇（非 arXiv 文件名）用 LLM 兜底打标签。

## 2026-10-27（周二）论文"类型"元数据：arXiv 反查 + LLM 兜底（B1+B2）

- 新增 `scripts/enrich_catalog.py`：① 有 arXiv 编号的按 20 个/批调 `https://export.arxiv.org/api/query?id_list=…`（限速 3 秒），取 `arxiv:primary_category`；② 没有编号的（`N19-1423.pdf`、`language_understanding_paper.pdf` 等 6 篇）用 DeepSeek 按标题归到 5 个粗类；③ arXiv 主分类里没有"强化学习"，按标题关键词（Reinforcement Learning / Policy Optimization / Preference Optimization / RLHF）补 3 篇。
- 结果：40 篇全部有类别——自然语言处理 22、机器学习 10、强化学习 3、计算机视觉 3、理论与其他 2；0 篇"未分类"。
- 端到端：「这些论文都是什么类型的？」→ 直接给出五类分布（证据首条 `channels=['catalog']`）；「有多少篇是自然语言处理的？」→ 22 篇。
- 证据：`85 passed in 3.37s`；`ruff check app tests scripts` → All checks passed。

## 2026-10-27（周二·下午）推荐/对比为什么答不出：清单缺名单、对比没锁篇、参考文献被当正文

- 症状（用户手机截图）：「推荐一篇强化学习的论文」→ 只能搜到别的论文参考文献里的综述条目；「三篇对比」→ 只覆盖 1 篇。
- 根因三条：①清单只给"强化学习 3 篇"这种统计，不给"是哪 3 篇"；②多篇对比只在界面勾选 ≥2 篇时触发，问句里说"三篇"没人接；③References 列表片段被当作正文证据。
- 修：①`_catalog_block` 按类别列"文件名（短标题）"，`CATALOG_MAX_CHARS` 1200→3200（实测 1567 字，40 篇全列出）；②新增 `_COMPARE_QUESTION`/`_wants_compare`/`_plan_multi_sources`，问句里点名类别或论文时自动锁定篇并各检一次；补 `_TERM_ALIASES`（PPO→Proximal Policy Optimization 等）解决"标题里没有缩写"；③`pipeline._looks_like_citation` + `RAG_CITATION_PENALTY=0.6` 给参考文献片段降权并打 `citation` 通道，上下文加"不能当正文依据"前缀，prompt 加规则 8。
- 端到端：「给我推荐一篇强化学习的论文」→ 点出 3 篇并推荐 PPO；「对比 PPO 和 DPO」→ 自动锁 2 篇、出 4 列对比表、每格带来源页码；「三篇对比，强化学习方向」→ 3 篇表。
- 证据：`92 passed in 3.36s`；`ruff check app tests scripts` → All checks passed。
- 坑：清单类问题的 `catalog` 证据会插到第 0 条，断言要按来源找而不是看 `evidence[0]`。

## 2026-10-27（周二·晚）论文精华卡：摘要/研究问题/方法/贡献一次抽好，摘要类问题不再靠检索碰运气

- 病理：问「三篇的主旨/研究问题」失败——摘要页和参考文献列表里都有 `reinforcement learning` 这类词，检索器分不清，召回的是别的论文 References。
- 做法（模板化，加论文只需重跑脚本）：
  - `app/cards.py`：固定字段 `title/abstract/problem/method/contributions/results/limitations/keywords`，`load_cards/get_card/render_card`，mtime 缓存，先写临时文件再替换。
  - `scripts/build_cards.py`：从第 1 页切 `Abstract`（到 Introduction/Keywords 为止，切不到退化为前 1200 字），DeepSeek `temperature=0` + JSON 模式生成卡片；失败或扫描件走规则兜底（`generated_by=rule`，写明"扫描件无文本层"），不静默丢篇。支持 `--rebuild/--only/--limit`，默认增量。
  - `app/agent.py`：`_EXPLAIN_QUESTION` 命中且能确定篇目时，注入「精华卡（来自论文第 1 页）」并加 `channels=["card"]` 证据；SYSTEM_PROMPT 规则 9 约束"优先依据精华卡、引用标第 1 页、摘要未提及就如实说"。
- 结果：40 篇全部有卡片（39 篇 LLM 生成、1 篇扫描件规则卡片）；全量测试 `97 passed`，ruff 全绿。
- 端到端：「对比 PPO、DPO、DeepSeek-R1 三篇的研究问题和方法」→ 证据前三条都是 `page=1, channels=['card']`，回答出三列对比表，每格带 `[来源: 文件名 第1页]`。
- 以后加论文：PDF 丢进 `data/papers/` → `python scripts/build_cards.py`（增量，只跑新篇）→ 问答侧自动生效。
- 补丁：问题里点名类别（"强化学习是哪三篇，他们的主旨是？"）时，卡片目标按 `catalog` 的类别元数据取，并在上下文里写死"该类共 N 篇（文件名/标题/年份）"，防止模型拿检索片段里的其他论文凑数（实测修复前会把 Understanding Deep Learning 混进来）。`98 passed`。
- 补丁：问题里点名具体论文时（"总结 1707.06347v2.pdf"、"PPO 那篇讲了什么"），`_named_sources()` 按文件名/标题词匹配并展开缩写（PPO→Proximal Policy Optimization），命中就读该篇精华卡，不再落到别的论文的参考文片段。`100 passed`。

## 下一步

1. 官方 5 问端到端验收（第 6 作者 / Adam β1β2 / GPU 数量型号 / BLEU 对比 / LayerNorm 引用），记录页码与引用格式
2. 公网演示地址（隧道或云平台）+ 技术文档 PDF（≤30 页）+ 5–8 分钟视频
3. 多篇对比问答、阅读页与引用图谱（P1/P2）
4. 缴费：10/31 15:00 前必须完成（预指导门槛）

## 2026-10-27（周二·上午）去重与类别容错

- 删除重复论文 209_Emergent_Abilities_of_Larg.pdf（与 2206.07682v2.pdf 同为 Wei et al.《Emergent Abilities of LLM》，TMLR 2022）：
emove_source 清掉 401 块，docs/dense 25995→25594；catalog/cards/structures 同步删，现共 39 篇（38 篇已建索引）。
- 类别名容错：用户把「能力评测与规模」写成「能力测评与规模」，旧的精确匹配锁不到篇、卡片为 0；改为最长公共子串兜底（≥max(3, len/2)），	ests/test_category_match.py 3 例。
- 章节定位：问「第 3 节」时按 structures.json 的章节表补该页正文（	ests/test_section.py 3 例）；指代上一轮论文时给显式上下文映射。
- 验收：107 passed、ruff 全绿；e2e「共 39 篇（38 已索引）」「能力评测 3 篇」；提交 1380bc3、0d8be5、ce61ae1、db43b28 已 push。

## 2026-10-27（周二）表格行结构化 + 数量题/口语名

- pp/structure.py：_table_rows 改为「PyMuPDF 线框 bbox 裁剪页面 → pdfplumber 文本策略 → 质量校验（≥2 行/≥3 列/宽度一致≥70%/单字符≤35%）」，_pymupdf_table_boxes 做位置交叉验证，剔掉正文误检。39 篇重建 256s：**34/39 篇有数据行，共 2651 行**（表题覆盖 36/39）。
- pp/agent.py：_table_rows_text() 把结构化行渲染进上下文，数量题优先按「点名论文」取表（新增 _PAPER_ALIASES：GPT-3/PPO/DPO/LoRA/BERT/LLaMA 等口语名 → 文件名），证据新增 	able-rows 通道。
- 端到端：GPT-3 一共训了几个不同规模的模型？ → 「8 个规模，125M 到 175B」，证据 #5 p8 table-rows；总结一下 1707.06347v2.pdf → 走精华卡，#1 p1 card。
- 123 passed、
uff 全绿；提交 8b0908 已 push。

## 2026-10-27（周二·深夜）阅读页 1.3.2：章节树 + PDF 渲染 + [n] 跳 References

- 交付：`web/reader.html`，入口 `/ui/reader.html?paper=<文件名>&page=<页>`——左侧章节树（来自 structures.json，Abstract/1..7/References 逐条带页码）、正文连续滚动、页码框、100% 适合宽度、± 缩放、换论文下拉；侧栏顶部是这篇的作者与摘要。
- 后端 `app/reader.py` + 三条路由：`GET /reader/papers`（39 篇清单）、`GET /paper/{filename}/structure`（标题/作者/摘要/章节/references 页码，页码统一 1 基）、`GET /paper/{filename}`（`FileResponse`，自带 Range，实测 `bytes=0-99` → 206 + `bytes 0-99/612446`）。路径校验 `safe_paper_path()`：拒绝 `..`、子目录、绝对路径、非 `.pdf`（`..%2Fmain.py`、`notes.txt`、`ghost.pdf` 均 404）。
- 引用跳转走粗粒度：正文里 `[38, 24, 15]` 这类引用被识别成可点区域，点了翻到 References 页并浮出「返回原位置」，Esc 也能退；References 页本身不再叠一层引用高亮。`web/index.html` 证据卡片新增蓝色「打开原文」，直接带 `page=` 定位（`page` 为空时退到第 1 页）。
- 前端依赖本地 vendor：`web/vendor/pdfjs/pdf.min.js`（377,116 字节）+ `pdf.worker.min.js`（1,133,660 字节），版本 3.11.174 legacy 构建，**不引 CDN**；`scripts/fetch_pdfjs.py` 三个镜像源可重下（会先试系统代理，失败再直连）。
- 证据：
  - `pytest -q -p no:cacheprovider` → `137 passed, 10 warnings in 28.35s`；`ruff check app tests scripts` → `All checks passed!`
  - 无头 Edge（CDP）实测 1706.03762v7.pdf（Attention Is All You Need）：第 3 页识别出 6 处引用（样例 `[38, 24, 15]`），点击后页码 3→10（References），返回按钮浮出，再点回到 3；`state.refPage=10` 与 `structures.json` 一致。
  - 章节树点「Experimental results」→ 页码 1→6，当前节点高亮。
  - 390×844：`docScrollWidth=390 = docClientWidth`，无横向溢出；侧栏变抽屉（`position: fixed`），点「三章」按钮 `open` + 遮罩生效。
  - 证据卡片「打开原文」：`page=8` → `/ui/reader.html?paper=1607.06450v1.pdf&page=8`（可访问 200），`page=null` → `page=1`。
  - 不带 `paper` 参数进阅读页 → 论文目录页，列出 39 篇。
- 坑：
  - `msedge --headless --screenshot/--dump-dom` 是**一次性快照**，pdf.js 的异步渲染还没完成就截图，会看到「正在加载 PDF…」和 `/ 0`，误判成页面卡死；必须走 CDP（`Runtime.evaluate` 等够时间）再判断，布局尺寸也要靠 `Emulation.setDeviceMetricsOverride` 才能量准。
  - `main#viewer` 用 `overflow: auto` 时，「先按 clientWidth 算页宽 → 页面撑出竖滚动条 → 可用宽度变窄」会多出一条横向滚动条；改 `overflow-y: scroll` 让它一开始就占位，再减 1px 兜住取整误差。
  - 论文的章节树里本来就有 `References` 一节，再追加一条"§ References"入口会出现两个同名条目；改为先找已有条目并打上 refs 样式，找不到才追加。
  - 老论文（LayerNorm）用的是 `[Ioffe and Szegedy, 2015]` 作者-年份式引用，`[n]` 正则识别不到——这类论文只能靠左栏 References 手动跳，属已知限制。
- 下一步：题注/图表的定位入口、阅读页里"引用图谱"（谁引用了这篇 / 这篇引用了谁），以及移动端手势（双击缩放）。

## 2026-10-27（周二·深夜二轮）1.3.2 精确化：可折叠章节树 + [n]↔References 双向跳转 + 视图开关

- 章节树数据：`structures.json` 原有 28/39 篇只有一级标题（Attention、LayerNorm、LLaMA2 都在内）。改 `app/structure.py` 的 `_sections`，现在认三类版式——
  - 编号与标题同一行：`3.2 Model Architecture`；
  - 编号单独一行、字号与正文相同、只加粗：Attention 的 `3.2.2` + `Multi-Head Attention`（旧规则只认"字号 ≥ 正文 ×1.05"，全漏）；
  - ICLR/ACL 的 run-in 小标题：加粗的 `Datasets.` 后面直接接正文。
  同时压掉三类误判：目录页（GPT-3 的目录与正文同字号，会伪造出重复的 L1）、表格单元格（`36 MWh`、`68.0 T5-11B`）、图内标注（`Encoder:` / `Decoder:`）。
- 新增 `scripts/refresh_sections.py`（前 25 页估字号基线、正文扫到 200 页；`--all` 全量、`--dry-run` 预览、写前备份 `structures.json.bak`）。重算结果：**二级标题 721 个、三级 174 个**（原先二级 35 个），39 篇里仍有 3 篇扫描件抽不出二级。
- 阅读页 `web/reader.html`：
  - 左栏改成**可折叠嵌套树**（父级带 ▾，点击折叠子级；当前章节自动展开祖先）；
  - 正文 `[n]` 精确跳到 References 的**第 n 条**并高亮，条目上的 `[n]` 点回正文原处，浮出「↩ 返回第 N 页」；
  - 顶栏加 **宽 / 1:1 / 双页** 三个视图开关（1.3.1 的排版考虑：双页用来核对双栏版式）。
- 证据（原文）：
  - `145 passed, 10 warnings in 32.14s`；`ruff check app tests scripts` → `All checks passed!`
  - CDP 实测（Attention 第 3 页）：`tree {"secs":27,"withKids":4,"carets":4,"nested":["▾3Model Architecturep2","▾3.2Attentionp3","▾5Trainingp7","▾6Resultsp8"]}`
  - 折叠：`{"title":"▾3.2Attentionp3","before":false,"after":true,"kidsVisible":false,"leafCount":3}`
  - 引用：`{"n":"38","beforePage":"3","afterPage":"12","found":true,"hit":true,"backBtnShown":true,"backPage":"2","bodyHit":true}`
  - 视图：`two {"a":{"l":289,"w":553},"b":{"l":856,"w":553},"sameRow":true,"overflow":0}`、`actual {"pageW":612,"natural":612}`、`fit {"pageW":1120,"viewerW":1153}`
  - 390×844：`{"innerW":390,"scrollW":390,"viewerScrollW":390,"viewerClientW":390,"toggleVisible":true}`
- 坑：
  - 懒加载会同时渲染相邻页，点击到的 `.cite-n` 不一定在当前页——记「原处」必须取 `span.closest('.page')` 的页号，否则回跳落到别的页；
  - `ensureRendered` 只看 `dataset.ready` 会在重排后拿到空文本层，要额外检查 `.textLayer span`；
  - 无头 Edge 会缓存旧 HTML，CDP 里必须 `Network.setCacheDisabled`，否则改了看不到；
  - `_left_margin` 用 `min(x)` 会被一条 x≈10 的怪行带偏，改成"按整数 x 聚类取最左一档"。
- 下一步：1.3.3 翻译（部分 / 整篇 / 中英对照）。

## 2026-10-27（周二·深夜三轮）1.3.2 三个遗留问题收口：结构文件入库 + 扫描件借树 + 作者-年份引用

- 交付：
  - **结构文件搬进可提交位置**：`data/meta/structures.json`（原来在 `data/papers/` 里被 .gitignore 排除，换机器就没章节树）。`app/papers.py` 新增 `META_DIR` / `DEFAULT_STRUCTURES_FILE` / `LEGACY_STRUCTURES_FILE` / `structures_file()` / `save_structures()`：新路径优先、旧路径兜底、老环境第一次 save 自动迁移。
  - **扫描件借树**：新脚本 `scripts/borrow_sections.py`，先按标题、再按 arXiv 编号找同篇文本版；借来的树在侧栏注明「章节树借自同篇论文的文本版（…），页码以它为准」，借不到就明示"没有解析到章节结构"，不静默失败。
  - **作者-年份引用可点**：`[Ioffe and Szegedy, 2015]` 这类不再是死文字——点它跳到 References 对应条目并高亮，条目上浮出的「↩ 返回第 N 页」回到原页且把原来的引用闪一下（和数字引用 `[38]` 同一套体验）。
  - 顺带把章节解析压到 39 篇全有二级标题：`_SECTION_RE` / `_NUMBER_ONLY_RE` 支持 `2.1.` 这种带尾点的 ICLR 写法；`_left_margins()` 多栏左边界（CLIP/Codex 的双栏标题）；`_title_ok` 过滤表格行/题注/公式，并给全大写标题留兜底。
- 证据（原文）：
  - `scripts/refresh_sections.py --all` → `校验：json.load OK，39 篇；二级标题 786 个，三级 151 个；仍无二级标题 1 篇`
  - `scripts/borrow_sections.py` → `borrow 1706.03762v1_img.pdf: 27 节 <- 1706.03762v1.pdf（同 arXiv 编号）` / `共借到 1 篇`
  - `git check-ignore -v data/meta/structures.json` 无输出（可提交）；文件 987,509 字节
  - `153 passed, 10 warnings in 31.75s`；`ruff check app tests scripts` → `All checks passed!`
  - CDP 实测（`.tmp_shot/verify3.py`）：
    - 作者-年份：`{"before":{"page":"3","ref":11},"clicked":{"text":"[Ioffe and Szegedy, 2015]","key":"ioffe|2015"},"after":{"page":"11","hitText":"Sergey Ioffe and Christian Szegedy.","backShown":true,"backLabel":"3"},"back":{"page":"3"}}`
    - 借树侧栏：`{"note":"章节树借自同篇论文的文本版（1706.03762v1.pdf），页码以它为准","count":27,"empty":false}`
    - 数字引用回归：`{"before":{"hostPage":"2","cur":3,"ref":10},"at_ref":{"page":"12","hit":"[38]","refLink":true},"back_entry":{"page":"2","citeHit":1,"hitText":"38"},"back_btn":{"page":"2","hitText":"38"}}`
- 坑：
  - pdf.js 把一条参考文献切成好几块（`Sergey Ioffe and Christian Szegedy.` / `In ICLR, 2015.`），姓氏和年份**不在同一个文本块**里。按单块同时匹配姓氏+年份会全部落空；改成把整页文本块拼成长串、找姓氏后在后面 420 字内找年份，再映射回 span。
  - 拼长串时不能取 `.textLayer span`（会把它内部嵌套的 `.ref-link` / `.cite-n` 也数进去，文字重复、位置错位），要用 `.textLayer > span`。
  - 扫描版 PDF 没有文本层，CDP 等待 `#viewer .textLayer span` 会永久超时——等页面 `.page` 就行。
  - 日志/输出里的中文在 PowerShell 里会变成乱码（GBK 控制台），要么设 `$env:PYTHONIOENCODING='utf-8'`，要么把结果写进 utf-8 文件再读。
- 下一步：1.3.3 翻译（部分 / 整篇 / 中英对照），以及 1.3.1 的公式/表格排版体验（题注定位、表格行高亮）。

## 2026-10-28（周三·凌晨）1.3.3 翻译：部分翻译 / 整篇翻译 / 中英对比阅读

- 新增 `app/translate.py`：以**页**为翻译单位（PDF 与阅读页天然共享的坐标，前端对比滚动只需页码 + 页内比例）。
  - `page_texts()` 用 PyMuPDF 抽块、按 y 排序、去软连字符与断词、清掉 `\x00-\x08` 控制字符（PDF 的项目符号常抽成空字符，会把模型带偏）；
  - `translate_text()` 按 6000 字切块分别请求，避免长页被截断；
  - `translate_pages()` 逐页查缓存（`sha1(原文)[:12]`），命中就不调模型；`pages=None` 才是整篇，**显式空列表就是一页不翻**（防止参数算错把整篇烧掉）；单页失败只影响该页；
  - 缓存写 `data/meta/translations/{filename}.json`（`data/papers/` 被 gitignore，跟着走会丢）。
- 提示词（`SYSTEM_PROMPT`）：学术英文 → 简体中文、术语首次出现附英文括注、**公式/变量/代码/arXiv 编号/引用标记原样保留**、输出 Markdown（赛题 1.3.3.2 的"可读性"方案）、不写导语。
- 后端三条路由（`app/main.py`）：
  - `GET /paper/{filename}/translation` → 已缓存译文的页码与 Markdown；
  - `GET /paper/{filename}/translate?pages=all|1,3,5&force=false` → SSE 逐页推送（`page` / `done` / `error`），`all` 自动截到 References 之前；未配 Key 返回 503，页码解析不出来返回 400；
  - `POST /translate/selection` → 划词/选中段落的即时翻译（带 600 字上下文，提示词里明确"上下文不要翻"）。
- 前端 `web/reader.html`：
  - 顶栏加「译」按钮，右侧 420px 译文栏（`本页 / 全文 / 对比 / 停止`），窄屏 ≤900px 变成浮层；
  - **1.3.3.1 部分翻译**：在正文里选中文字，鼠标旁浮出「译」钮，点一下把原文摘要 + 中文译文置顶到译文栏；
  - **1.3.3.2 整篇翻译**：`全文` 按钮走 SSE，边翻边渲染，已翻过的页直接读缓存；
  - **1.3.3.3 中英对比阅读**：`对比` 打开同步，拖正文按「页码 + 页内比例」带动译文栏，拖译文栏反过来带动正文（程序滚动时用 220ms 锁防互拽）；
  - 译文栏开关会改变正文宽度，所以切完要 `relayoutKeepingPage()`（重排后回到原来那一页）。
- 新增 `scripts/translate_papers.py`：`--pages all|1-9|3,5,7` 批量预翻译（离线演示/换机器后一键补齐），已预翻译 LayerNorm 全篇 10 页。
- 证据（原文）：
  - `168 passed, 11 warnings in 34.38s`；`ruff check app tests scripts` → `All checks passed!`
  - 预翻译：`python scripts/translate_papers.py 1607.06450v1.pdf --pages 1-9` → 9 页新译（1195/1138/1376/1383/1631/1721/1635/2145/1437 字），产物 `data/meta/translations/1607.06450v1.json`；SSE 实测 `/translate?pages=4` 两帧（page + done），再点一次 `cached: true`。
  - CDP 实测（`.tmp_shot/v4.py`，1440×900）：
    - 整篇：`{"open":true,"status":"已译 9 页","pages":[1..9],"firstText":"P1 层归一化 Jimmy Lei Ba 多伦多大学 jimmy@psi.toronto.edu …","viewerW":733}`
    - 划词：`{"hidden":false,"len":101,"text":"vector effectively controls the learning rate for the weight"}`
    - 译文：`"选中的原文 vector effectively controls the learning rate for the weight vector. During learning, it is harder to … 向量有效地控制了权重向量的学习率。在学习过程中，更难的是"`
    - 单页补译（第 10 页）：`{"status":"已译 10 页","pages":[1..10],"page10":"P10 10 -1 10 -2 训练 NLL 10 -3 10 -4 BatchNorm bz128 …"}`
    - 对比同步：`{"pdfPage":3,"transPageAtTop":3,"syncOn":true}`
- 坑：
  - PDF 里的公式会把一页的文字顺序打乱（译文栏顶部出现 `μt = 1 / at = μt / ht + f / ot + b` 这种碎片），属抽取层面的限制，提示词只能尽量保留公式原文；
  - `window.state` 是 `undefined`——`state` 是顶层 `const`，不挂 window；CDP 里直接写 `state` 能取到，判断存在要用 `typeof state !== 'undefined'`；
  - 译文栏开关会触发 `relayout()`（清空并重渲染占位），测试必须在打开后重新 `jumpToPage()` 再等 `.textLayer > span`；
  - 划词要挑最长的 span（第一个 span 常是 `3.1` 这类短串，长度 <4 会被浮钮逻辑忽略）；
  - PowerShell 的 here-string 传给 `python -c` 会吃掉引号（`SyntaxError: invalid decimal literal`）→ 一律写临时 `.py` 文件跑。
- 下一步：1.3.1 的公式/表格排版体验（题注定位、表格行高亮）、1.4.2 arXiv 每日推荐（可选）、1.6.3 CSV→图（可选）。

## 2026-10-28（周三·凌晨二轮）1.3.1 基本阅读功能：单/双栏判定 + 按栏看 + 表格公式清单

赛题 1.3.1 原文：「基本阅读功能：要考虑原文的排版(双栏/单栏)，公式和表格的排版。」PDF 本身不记录栏数，只能从正文块的横向分布推断；表格/公式则靠 `structures.json` 里已有的条目反查页码。

- 交付：
  - `app/layout.py`：`detect_layout(pdf_path)` / `layout_summary(pdf_path)`。取前 40 页，每页把长度 ≥20 的正文行按 `x0` 排序后**按空隙聚类**（间隔 > `max(12, 0.05*页宽)` 起新簇），只留 ≥6 行的簇；不足 2 簇 → 单栏；首尾两簇中心距 < 0.32 页宽 → 单栏；右簇行占比不在 20%~80% → 单栏（挡"单栏 + 右侧图注"）。返回 `mode`（two-column / single-column / mixed / unknown）、逐页 `cols`（两栏的 `[x0, x1]`，`x0` 取 10 分位、`x1` 取 80 分位以避开跨栏题注）、`switches`（连续页栏数变化的页码）。
  - `GET /paper/{filename}/layout`：透传 `detect_layout`，路由里做路径穿越防护（`safe_paper_path`）。
  - `reader_meta()` 新增 `layout`（`layout_summary`）、`tables`（题注+页码，页码 `+1` 成 1 基）、`formula_pages`（按页计数）。
  - `web/reader.html`：顶栏「版式·双栏/单栏/混合/未知」徽章（点开列逐页判定与切换页）；第四种视图「按栏·左 / 按栏·右」（把一栏宽度铺满视口，`scrollLeft` 对到该栏左边界）；侧栏新增「表格」「公式」清单，点条目跳页并给整页套 `.land` 蓝圈；摘要下方两枚胶囊「表 N 张 / 公式 M 页」滚到对应清单。
- 证据（CDP，1440x900）：
  - CLIP `2103.00020v1.pdf` p5：`badge {"text":"版式·混合","cls":"icon-btn two"}`、`cols [[55.1,290.1],[307.4,543.1]]`、`panel {"rows":["双栏第 1 页",…],"hint":"混合 · 采样 40/48 页 · 第 36、37、40 页起版式切换"}`、`fit {"pageW":1120,"viewerW":1153,"scrollW":1153}`、`col_left {"label":"按栏·左","pageW":2916,"scrollLeft":257,"scrollW":2948}`、`col_right {"scrollLeft":1457}`、`tables {"cnt":"7 张","first":"7"}`、`landed {"page":7,"landed":["7"]}`。
  - 单栏 Attention `1706.03762v7.pdf`：`{"badge":"版式·单栏","mode":"single-column","tables":"4 张","first":"6","formulas":"7 页"}`，点第一条题注落到第 6 页并高亮。
  - 390x844 窄屏：`{"docScrollW":390,"docClientW":390,"viewerScrollW":390,"viewerClientW":390,"badgeShown":true}`，无横向溢出。
  - 全量回归：`178 passed, 11 warnings in 29.43s`；`ruff check app tests scripts` → `All checks passed!`。新增 `tests/test_layout.py` 10 例（合成行判双栏/单栏/页码行不成簇/行数不足、`layout_summary` 字段、路由 200/404/路径穿越、`reader_meta` 的 tables 与 formula_pages 页码 ≥1、fitz 合成双栏 PDF）。
- 坑：
  - 第一版用「x<45% 左簇 / x>55% 右簇」判双栏，**39 篇全判成单栏**：双栏右栏的 `x0` 落在两阈值之间，且跨栏题注把 `max x1` 拉到页尾；改「按空隙聚类 + 首尾簇中心距 + 右簇占比」后，T5/CLIP/Codex/GPT-4/ACL/N19-1423 都判成双栏，Attention/LayerNorm/LLaMA2/GPT-3 仍是单栏。
  - `applyPagesLayout()` 在「按栏」时必须 `alignItems: flex-start`（页面在 flex 容器里 `center` 会把溢出部分左右均分，左半边永远滚不到：实测 `scrollW 2058` 而 `pageW 2930`）。
  - `structures.json` 的 `tables[].page`、`formulas[].page` 是 **0 基**，与 `references.page` 一致；前端展示统一 `+1`（Attention 表 1 记 5 → 实际第 6 页）。
  - `_captions()` 会把正文里以 "Table 1 …" 开头的句子也当题注，所以表格清单要求 `rows` 非空。
  - CDP 脚本：`cdp.wait()` 的表达式返回 DOM 元素时 `returnByValue` 得到 `{}`（falsy），必须写 `!!document.querySelector(...)`；`start_edge()` 每端口要独立 `--user-data-dir`，否则新实例把命令行转给老实例后自己退出。
- 下一步：1.4.2 arXiv 每日推荐（可选）、1.6.3 CSV→图（可选）、评分硬线（10/31 15:00 前缴费）、演示视频与技术文档。
