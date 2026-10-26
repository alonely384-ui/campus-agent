# 三个问题的根因与业界做法调研（先不改代码）

> 日期：2026-10-27　状态：只调研 + 复现，未修改任何 `.py`
> 结论先写在最前面：这三个问题都不是"模型不行"，而是**入口层缺了三样东西**——全库统计入口、文档元数据、会话历史传递。三个都能在 1 天内补齐。

---

## 0. 复现证据（原文）

用 `POST /chat`、`history=[]`、不指定 `source` 实测：

**提问 1：这里一共有多少篇论文？**

```
【结论】检索片段中一共出现 4 篇论文：LLaMA、2001.08361v1、2005.14165v4，以及 333078981_693988129081760_4712707815225756708_n.pdf
```

`rounds=1 steps=0 evidence=4`。它**没有撒谎**——它说的是"检索片段中"，只是用户问的是"库里"。

**提问 2：这些论文都是什么类型的？**

```
【结论】论文库中没有找到相关信息。
```

`evidence=4`。检索到了片段，但片段里没有"类型"这种字段。

**提问 3：回答末尾的【下一步】能不能接着问？** → 不能，下一轮是全新对话。

---

## 1. 问题 A：「有多少篇论文」答成 3–4 篇

### 真实表现

问"全库有多少篇"，回答的是"这次检索到的 4 段里提到几篇"。把 4 段当成了全世界。

### 根因（代码行）

| 位置 | 事实 |
|---|---|
| `app/rag/pipeline.py` `retrieve_scored(...)` | 每次固定 `top_k=4` 返回**片段**，从不返回"全库有多少文档" |
| `app/agent.py` `_rag_context_with_evidence(...)` | 把这几段拼成"参考资料"塞进系统消息，模型只能看到这几段 |
| 全项目 | **没有任何接口/函数**回答"库里有多少篇"；`GET /papers/files` 只给前端下拉，不进 prompt |

一句话：**向量检索是"找相关段落"，不是"查数据库计数"。**用检索回答聚合问题，必然只见树木。

### 别人怎么做（GitHub / 官方文档）

1. **文档级摘要索引**（LlamaIndex `DocumentSummaryIndex` / `SummaryIndex`）：把"每篇文档的摘要"建成一层索引，专门回答"文档级"问题，而不是 chunk 级。
   - 文档：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/doc_summary/DocSummary.html>
2. **结构化查询**：把"有多少篇/按类型分组"当成 SQL 问题，交给 `count(*)` / `group by`，而不是向量库。
   - LlamaIndex SQL Index：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/struct_indices/SQLIndexDemo.html>
   - LlamaIndex Pandas Index：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/struct_indices/PandasIndexDemo.html>
3. **自查询（Self-Querying）**：让 LLM 先把问题拆成"过滤条件 + 语义查询"，元数据过滤走结构化路径。
   - LangChain Self-query retriever：<https://python.langchain.com/docs/how_to/self_query/>
4. **路由（Router）**：检索前先判断问题类型，聚合类走工具、事实类走向量。
   - LlamaIndex RouterQueryEngine：<https://llama-index.readthedocs.io/en/stable/examples/query_engine/RouterQueryEngine.html>
   - 参考实现 RAG-LCC 的"检索前 gate"：<https://github.com/HarinezumIgel/RAG-LCC>

### 可选方案（不写代码，只列做法与工时）

| 方案 | 做法 | 工时 | 代价 |
|---|---|---|---|
| **A3（推荐）** | 建库时顺手写一份 `catalog.json`（39 篇：文件名/标题/页数/类别/年份）；问题和"多少篇/哪些"沾边时，把这份**全量清单摘要**直接塞进上下文（约 1KB） | 0.3 天 | 清单变长要截断；但 39 篇完全够 |
| A1 | 新增 `GET /stats`（篇数、按类别计数），prompt 里命中计数类问题就先查它 | 0.5 天 | 多一个接口要维护 |
| A2 | 上 `DocumentSummaryIndex` 式的一层摘要索引 | 1 天+ | 要为每篇生成 LLM 摘要，39 篇约 39 次调用，成本与重建时间都要算 |

**判断**：A3 对 39 篇的规模性价比最高；若后续要展示"按年份/类别分布"，再做 A1。

---

## 2. 问题 B：「什么类型的论文」答不出来

### 真实表现

拒答。不是模型拒答，是**资料里根本没有"类型"这个词**。

### 根因（代码行）

| 位置 | 事实 |
|---|---|
| `app/papers.py` `Paper` 数据类 | 字段只有 `paper_id / filename / size / pages / uploaded_at / duplicate_of`，**没有 title / authors / year / venue / category** |
| `app/papers.py` `index.json` | 只记录"上传过的"（当前 2 条），39 篇批量解压的论文不在这里 |
| `app/structure.py` | 已能抽 `title / authors / sections`，但**没有抽 arXiv 类别、会议、年份** |
| 文件名 | 多数是 `1706.03762v7.pdf`、`333078981_..._n.pdf`，模型看不出"这是 NLP 还是 CV" |

一句话：**"类型"是个字段问题，不是检索问题。**没有字段，检索不回来，模型只能拒答。

### 别人怎么做

1. **元数据抽取再建索引**：Metadata-Aware RAG（Self-Querying + metadata filtering，Qdrant 落地）：<https://github.com/nikhilreddy00/Metadata-Aware-Rag>
2. **自动抽元数据**：LlamaIndex `MetadataExtractor`（LLM 读文档 → 输出结构化字段）<https://docs.llamaindex.ai/en/stable/module_guides/loading/documents_and_nodes/usage_metadata_extractor/>
3. **用现成标识符反查**：arXiv id（`1706.03762`）可直接查 `https://export.arxiv.org/api/query?id_list=1706.03762` 拿到 `primary_category`（cs.CL）、标题、作者、年份——**不用 LLM，零成本**。
4. **分类标签分层**：先把 39 篇按 arXiv 大类（cs.CL / cs.LG / cs.CV / cs.AI / stat.ML）分组，再让模型基于该组回答"这类论文讲了什么"。

### 可选方案

| 方案 | 做法 | 工时 | 代价 |
|---|---|---|---|
| **B1（推荐）** | 建库时用 arXiv id 调 API 补 `primary_category / title / year`，写进 `catalog.json`；问"类型"时把分组统计塞进上下文 | 0.5 天 | 需要写一次小脚本（只写元数据，不动索引） |
| B2 | 用 LLM 读摘要打标签（3–5 个自定义类） | 1 天 | 39 次调用；标签口径要人定 |
| B3 | 只按"文件名模式"粗分（arXiv id / 图片型 / 其他） | 1 小时 | 太粗，答辩容易被问穿 |

**判断**：B1 最划算，且答辩时能讲清"元数据来自权威源，不是模型猜的"。

---

## 3. 问题 C：回答末尾的建议在下轮"失忆"

### 真实表现

第一轮说"你可以继续问我 XXX"，第二轮问"接着说"，模型完全不知道在说什么。

### 根因（代码行）——这次是**前端一句话**

| 位置 | 事实 |
|---|---|
| `web/index.html:266` | `const payload = { message: question, history: [] };` ——**历史永远是空数组** |
| `app/main.py:75` / `:108` | 后端**已经**把 `req.history` 传给 `run_agent(...)` / `run_agent_stream(...)` |
| `app/agent.py:571-572`、`:668-669` | `if history: messages.extend(history)` ——后端**已经**会拼历史 |
| `app/cli.py:34/49/58-59` | 命令行版**已经**在维护 history |

也就是说：**后端和 CLI 都能多轮，只有网页版把历史写死成 `[]`。**这是 1 小时能修的问题，不是架构问题。

补充一个真实约束：现在也没有"会话状态"——刷新页面就全丢；想要"关掉再打开还记得"，还需要 `localStorage` 或后端 session。

### 别人怎么做

1. **LangChain `RunnableWithMessageHistory`**：把"消息历史"包成一个可插拔的装饰器，按 `session_id` 读/写历史。
   - <https://reference.langchain.com/python/langchain-core/runnables/history/RunnableWithMessageHistory>
2. **mem0**：给 Agent 的"长期记忆层"，跨会话保存用户事实，检索时召回。
   - <https://github.com/mem0ai/mem0>
3. **LlamaIndex ChatMemoryBuffer / ChatEngine**：短窗口记忆（最近 N 轮），超出就摘要压缩。
   - <https://docs.llamaindex.ai/en/stable/module_guides/deploying/chat_engines/>
4. **工程界通行做法**：历史窗口（最近 6–10 轮）+ 超长时摘要压缩 + 需要跨会话再上向量记忆。

### 可选方案

| 方案 | 做法 | 工时 | 代价 |
|---|---|---|---|
| **C1（推荐）** | 前端把最近 6 轮 `{role, content}` 存 `localStorage`，每轮 `history` 带上 | 1 小时 | 刷新不丢，换设备丢 |
| C2 | 后端按 `session_id` 存最近 N 轮（内存/Redis） | 2 小时 | 要加 session 概念与清理 |
| C3 | 接 mem0 做长期记忆 | 0.5–1 天 | 多一层依赖；对 39 篇论文场景收益有限 |

**判断**：先 C1（1 小时），答辩演示"连续追问"就够；C3 等项目真有多用户再说。

---

## 4. 优先级与总账

| 顺序 | 内容 | 工时 | 对应评分点 |
|---|---|---|---|
| 1 | C1 前端带 history | 1h | 多轮交互（UI/UX、Agent 编排） |
| 2 | A3 全量清单摘要进上下文 | 0.3 天 | 抗幻觉、实用性 |
| 3 | B1 arXiv 元数据补全 | 0.5 天 | 多模态与细粒度解析、数据质量 |
| 合计 | | **约 1 天** | |

**明确不做**：不引入 LangChain/LlamaIndex 框架重写（与现有自研链路冲突，风险大于收益）；不上 DocumentSummaryIndex（39 篇规模用不上）。

---

## 5. 来源清单

- LlamaIndex Document Summary Index：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/doc_summary/DocSummary.html>
- LlamaIndex SQL Index：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/struct_indices/SQLIndexDemo.html>
- LlamaIndex Pandas Index：<https://llama-index.readthedocs.io/zh/stable/examples/index_structs/struct_indices/PandasIndexDemo.html>
- LlamaIndex RouterQueryEngine：<https://llama-index.readthedocs.io/en/stable/examples/query_engine/RouterQueryEngine.html>
- LlamaIndex MetadataExtractor：<https://docs.llamaindex.ai/en/stable/module_guides/loading/documents_and_nodes/usage_metadata_extractor/>
- LlamaIndex Chat Engines：<https://docs.llamaindex.ai/en/stable/module_guides/deploying/chat_engines/>
- LangChain Self-query retriever：<https://python.langchain.com/docs/how_to/self_query/>
- LangChain RunnableWithMessageHistory：<https://reference.langchain.com/python/langchain-core/runnables/history/RunnableWithMessageHistory>
- mem0（GitHub）：<https://github.com/mem0ai/mem0>
- Metadata-Aware RAG（GitHub）：<https://github.com/nikhilreddy00/Metadata-Aware-Rag>
- RAG-LCC（GitHub）：<https://github.com/HarinezumIgel/RAG-LCC>
- doc-qa-assistant（GitHub）：<https://github.com/dongshu710/doc-qa-assistant>

## 6. 我没验证的部分（诚实说明）

- 上面 6 个 GitHub 仓库我**读了 README/文档页面**，没有 clone 下来跑；它们的具体实现细节（参数、存储）不在本文承诺范围内。
- arXiv API 反查类别这条我**没有实测**（要先确认 39 篇里有多少是标准 arXiv id，有多少是裸数字文件名）。如果要做 B1，第一步是先统计文件名命中率。
- 前端 `history` 传空数组是**逐行读代码确认**的；但"带 history 后端就能正确多轮"只在 CLI 上验证过，网页端尚未实测。
