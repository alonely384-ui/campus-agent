# 1.6.1 论文框架生成：开源项目怎么处理「文献查找 + 引用约束」

调研日期 2026-11-11。范围只限「根据一段研究 idea 找候选文献，再让模型写正文并挂引用」这条链路。
四个项目均读真实源码，不是读 README。行号基于各仓库 main 分支（PaperQA2 @ 2026-11-09）。

---

## 0. 先给结论

1. **四个项目没有一个真正解决「引用关系错配」**（编号真实但论断不被该文支持）。我们遇到的
   `[C8] FlashAttention 分块计算` 实际指向 `ERNIE-Doc` 这类问题，它们要么不检测，要么只删编号。
2. 它们共同守住的底线只有一条：**模型能引用的编号集合 = 事后校验用的编号集合**（PaperQA2 做得最干净）。
   这一条我们已经有，不是差距。
3. 真正的差距在**候选池召回的准确率**——池子里没有那篇论文，模型就只能硬凑编号。
   这一点四个项目都没给我们答案，**反而 PaperQA2 的做法是反面教材**。
4. 因此本报告的最后一段（第 5 节）给的是我们自己的实测数据与改法，不是抄谁。

---

## 1. GPT Researcher（assafelovic/gpt-researcher）

### 怎么构造检索查询
- `gpt_researcher/skills/researcher.py` `ResearchConductor.plan_research()`：先做一次首搜拿 context，
  再交给 `gpt_researcher/actions/query_processing.py` `generate_sub_queries()` 让 LLM 出子查询，
  提示词在 `gpt_researcher/prompts.py:248`，只有一句「Write {max_iterations} search queries ...」。
- 子查询 `asyncio.gather` 并行、**各自独立检索**，最后 `combined_context = " ".join(context)`（researcher.py:369-373）。
- **没有**查询改写、同义词扩展、RRF 融合、rerank。甚至 `random.shuffle(new_search_urls)`（researcher.py:808）
  连检索分数序都不保留。

### 怎么保证引用真实
- 基本不保证。`prompts.py:309` 只有一句软约束让模型写 markdown 超链接。
- `prompts.py:555` `pretty_print_docs()` 给模型的上下文**只写 URL 不编号**。
- `gpt_researcher/actions/markdown_processing.py:94` `add_references(report_markdown, visited_urls)`
  把**所有访问过的 URL 全量**追加成 References，不与正文核对。
- 无编号白名单，无原文一致性校验。

### 怎么控制上下文规模
- `gpt_researcher/skills/context_manager.py` `ContextCompressor.__get_contextual_retriever()`：
  `RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)` →
  `EmbeddingsFilter(similarity_threshold=self.similarity_threshold)`（`SIMILARITY_THRESHOLD` 默认 0.35）→ top_n 截断。
- `async_get_context` 有短路：total_chars < 8000 且篇数 ≤ max_results 时直接返回。
- **没有 rerank**。

### 对我们的价值
- 可抄：**确定性的前置压缩**（先按字符数判短路，再截断，别让模型自己决定读多少）。
- 反面：它连引用校验都没有。我们的 `[C1]…[C16]` 白名单 + `check_citations` 已经比它强。

---

## 2. STORM（stanford-oval/storm）

### 怎么构造检索查询
- `knowledge_storm/storm_wiki/modules/knowledge_curation.py`：
  `class QuestionToQuery(dspy.Signature)` 的提示词是「You want to answer the question using Google
  search. What do you type in the search box?」→ LLM 出多条 query。
- `TopicExpert.forward` 里 `queries = queries[: self.max_search_queries]`，
  检索结果 `info += "\n".join(f"[{n + 1}]: {s}" for s in r.snippets[:1])` —— **每篇只取 1 个 snippet**，
  再 `limit_word_count_preserve_newline(info, 1000)`。

### 怎么保证引用真实
- 提示词层：`class AnswerQuestion(dspy.Signature)` 约束「Use [1], [2], ..., [n] in line」
  「every sentence is supported by the gathered information」「DO NOT need to include a References」。
- 校验层只有**编号存在性**：`knowledge_storm/utils.py` `ArticleTextProcessing.clean_up_citation(conv)`：
  ```python
  max_ref_num = max([int(x) for x in re.findall(r"\[(\d+)\]", ...)])
  # 若 max_ref_num > len(turn.search_results)，把超出的 [i] 字符串直接删掉
  ```
- `knowledge_storm/storm_wiki/modules/retriever.py` 只做 Wikipedia 域名黑名单
  （GENERALLY_UNRELIABLE / DEPRECATED / BLACKLISTED，其中把 arXiv 也列为不可靠）。

### 怎么控制上下文规模
- `knowledge_storm/collaborative_storm/modules/collaborative_storm_utils.py`
  `format_search_results(searched_results, info_max_num_words=1000, mode="brief")` 按 snippet 轮转装箱。
- `extract_cited_storm_info(response, index_to_storm_info)` = `re.findall(r"\[(\d+)\]", response)` 取交集。

### 对我们的价值
- 与我们的 `check_citations` 同类（编号白名单 + 越界编号删除），**不提供新东西**。
- 唯一值得注意：它把 arXiv 当「不可靠域名」，说明学术界对预印本的态度，我们答辩时可以用。

---

## 3. PaperQA2（Future-House/paper-qa）

PyPI 最新 2026.8.12，本报告基于 main @ 2026-11-09（9583 stars）。本地镜像在 `D:\alonely\deepseek\_研究\paperqa_src\`。

### 怎么从学术数据源检索
**它不做「按 idea 在线检索」**。全包 grep `api.arxiv|export.arxiv` 0 命中。

- 主检索 = 本地 tantivy 全文索引：`src/paperqa/agents/search.py` `SearchIndex.query(query, top_n=10,
  offset=0, min_score=0.0, ...)`（L399-434）。查询串只过一遍 `CLEAN_QUERY_REGEX = re.compile(r'[*\[\]:(){}~^><+"\\]')`（L397），
  **无同义词扩展、无 HyDE、无 LLM 改写、无多查询改写、无跨源融合**。
- Crossref / Semantic Scholar / OpenAlex 三个 client 只做「已知 DOI 或标题 → 补元数据」，
  全部走**标题精确匹配 + 自算 Jaccard 阈值 0.75**：
  `crossref.py:338-347`、`openalex.py:162-171`、`semantic_scholar.py:278-292`，
  相似度算法 `utils.py:105-114 strings_similarity` = 词集合 Jaccard。
- **没有 RRF**：grep `reciprocal|RRF|fusion|fuse(` 全包 0 命中。多源只做字段级合并
  `types.py:1267-1330 DocDetails.__add__`（`PREFER_OTHER = self.publication_date <= other.publication_date`），
  且 `clients/__init__.py:156-213 DocMetadataClient.query` 在请求字段齐了就 `break` 短路。
- Semantic Scholar 的 topic 检索与引用图（`FUTURE_CITATIONS` / `PAST_REFERENCES` /
  `PAPER_RECOMMENDATIONS`）代码存在但**生产路径只调 MATCH**（semantic_scholar.py:248 唯一调用点）。

### 怎么保证引用真实
- 引用单位是 `types.py:238-316 class Context`，其 `id` 是 `populate_id`（L304-316）
  `sha` 自 **(question + LLM 自己写的 summary)[:500]** —— **绑定的是模型摘要，不是原文**。
- 提示词契约 `prompts.py:38-50 CITATION_KEY_CONSTRAINTS`（合法形 `pqac-d79ef6fa`，
  并逐条列非法形）；`prompts.py:52-69 qa_prompt`：「Only cite from the context above and only use the
  citation keys from the context.」
- 唯一一层校验：`types.py:474-526 populate_formatted_answers_and_bib_from_raw_answer()`，
  在 `docs.py:719` 的 `Docs.aquery` 末尾调用：
  ```python
  id_to_name_map = {c.id: c.text.name for c in self.contexts}     # L480
  for hallucinated_key in set(included_keys) - set(id_to_name_map):   # L510-515
      formatted_without_references = formatted_without_references.replace(hallucinated_key, "")
  ```
  key 正则 `utils.py:191-194 get_citation_ids` = `re.findall(r"\bpqac-[a-zA-Z0-9]{8}\b", text)`。
  **越界只删字符串，不重试、不重写。**
- 「valid_keys 闭环」这条是真功夫：`settings.py:1179-1250 Settings.context_serializer` 里
  `valid_keys=", ".join([c.id for c in filtered_contexts])`（L1247-1250）与事后校验的 `id_to_name_map`
  **是同一份 filtered_contexts**。模型既看不到池外编号，事后又能精确定位越界。
- 一手术前防御：`utils.py:127-131 strip_citations` 在把原文喂给模型前，
  先用 `\b[\w\-]+\set\sal\.\s\([0-9]{4}\)|\((?:[^\)]*?[a-zA-Z][^\)]*?[0-9]{4}[^\)]*?)\)`
  剥掉原文自带的 (Smith 2023) 类引用，防与自己的编号打架（`core.py:357-359` 调用）。

### 怎么控制上下文规模
三段漏斗，**没有 cross-encoder rerank**（grep `CrossEncoder|cross_encoder|rerank|Rerank` 全包 0 命中）：
- (a) 向量召回 `docs.py:456-490 Docs.retrieve_texts`：`fetch_k = 2*k`，MMR 由
  `settings.py:814-816 texts_index_mmr_lambda: float = 1.0` 控制 —— **默认 1.0 等于关掉 MMR**
  （`llms.py:144 if len(texts) <= k or self.mmr_lambda >= 1.0: return texts, scores`）。
- (b) LLM 逐条打分 `docs.py:492-586 Docs.aget_evidence` → `core.py:178-380 _map_fxn_summary` 要求模型回
  `{"summary": ..., "relevance_score": 0-10}`（`prompts.py:108-119`，非相关则 score=0），
  `L579-585` 只保留 `c.score > 0`。
- (c) 装填 `settings.py:1179-1250`：**先按分降序取前 `answer_max_sources`（默认 5），再按
  `evidence_relevance_score_cutoff`（默认 1）筛** —— 顺序是先取 N 再筛，不是先筛再取 N。
  相关默认值：`evidence_k=10`、`agent_evidence_n=1`、`search_count=8`、`max_concurrent_requests=4`。
- 重试 `core.py:383-400 map_fxn_summary`：`LLMBadContextJSONError.retryable=True`（只重试一次，
  把失败消息塞回 prompt）；超时/请求失败 `retryable=False`。
- 证据不足明确拒答：`prompts.py:28 CANNOT_ANSWER_PHRASE = "I cannot answer"`，`docs.py:649-653` 兜底。

### 对我们的价值
- 可抄：「prompt 给的编号集 == 事后校验的编号集」做成**单一真相源**（我们已经是）。
- 可抄：「逐条打分 → 0 分丢弃 → 取前 N → 阈值筛 → 空了就明确拒答」的漏斗，宁可少引不硬凑。
- 可抄：喂给模型前先剥掉原文自带引用（`strip_citations`），我们摘录 abstract 时也可能带 `[12]`。
- **反面教材**：它把远程源限死在标题 Jaccard 0.75，等于完全放弃语义召回。我们绝不能学。

---

## 4. AI-Scientist（SakanaAI/AI-Scientist）

### 怎么构造检索查询
- `ai_scientist/generate_ideas.py` `search_for_papers(query, result_limit=10, engine="semanticscholar")`：
  `params={"query": query, "limit": result_limit, "fields": "title,authors,venue,year,abstract,citationStyles,citationCount"}`，
  **查询原样传给 S2**，靠上游 relevance 排序，`time.sleep(1.0)` 限速。
- `check_idea_novelty()`：最多 10 轮，每轮 LLM 自己出 `"Query"` 字段去搜，top10 拼成
  `"{i}: {title}. {authors}. {venue}, {year}.\nNumber of citations: {cites}\nAbstract: {abstract}"` 喂回，
  模型说「Decision made: novel / not novel」就停。**检索词由 LLM 自由写，无任何构造约束。**

### 怎么保证引用真实
- `ai_scientist/perform_writeup.py` 的 `citation_system_msg`：「only add citations that you have found using
  the API」「DO NOT ADD A CITATION THAT ALREADY EXISTS」。
- 流程：LLM 出 `Description` + `Query` → 搜 top10 → LLM 选哪篇 → 取 `papers[i]["citationStyles"]["bibtex"]`
  写进 `references.bib`。**候选池由模型自己决定，每轮只有 10 篇。**
- 唯一的事后校验：`generate_latex()` 里
  ```python
  cites = re.findall(r"\\cite[a-z]*{([^}]*)}", tex_text)
  bib_text = re.search(r"\\begin{filecontents}{references.bib}(.*?)\\end{filecontents}", ...)
  for cite in cites:
      if cite not in bib_text: coder.run(prompt)   # 让 LLM 修或删
  ```
  即「正文引用的 key 必须存在于 bib」。

### 对我们的价值
- 它的「每轮少量检索 + 模型挑选 + 立刻落 bib」循环，比我们「一次性给 16 篇再让它写」更灵活；
  但代价是多次 LLM 调用，且没有 RRF/去重，同一篇会被反复搜到。
- 它的 bib 合法性校验我们已有（`unknown_keys`），无新意。

---

## 5. 我们自己实测出来的东西（四个项目都没覆盖）

### 5.1 症状
同一篇 idea 生成两次，模型引用关系看着对，但换个 idea 就错配。核心不是模型不听话，
而是**候选池里没有它想引的那篇**，它只好把编号挂给池里「最像」的一篇。

### 5.2 根因：arXiv 查询构造把召回打死了
`app/literature.py:285 _arxiv_expr(q)` 现在的拼法是「首词限标题 + 其余 AND」：
```python
words = [w for w in re.split(r"[\s,;，、]+", q)
         if len(w) >= 3 and w.lower() not in _ARXIV_STOPWORDS][:5]
clauses = [f'ti:"{words[0]}"'] + [f'all:"{w}"' for w in words[1:]]
return " AND ".join(clauses)
```

**实测（同一批关键词，GOOD 指 CG-RAG / VeriCite / LitFM / Citation-Grounded / SciRAG 等契合本课题的论文）**：

| 查询形态 | 表达式 | 结果 | 命中 GOOD |
|---|---|---|---|
| 现实现（首词 ti: + AND 5） | `ti:"retrieval" AND all:"augmented" AND all:"generation" AND all:"citation" AND all:"graph"` | 12 | 5 ✅（**这次反而最好**） |
| 去掉 ti: 全 all: AND | `all:"retrieval" AND ... all:"graph"` | 12 | 5 |
| 短语 AND 短语 | `all:"RAG" AND all:"citation graph"` | 4 | 3 |
| 加 hallucination | `all:"citation graph" AND all:"RAG" AND all:"hallucination"` | 0 | 0 |
| 混合（ti 首 + 短语） | `ti:"retrieval" AND all:"augmented" AND all:"citation graph" AND all:"hallucination"` | 1 | 1 |
| 脚本文 freetext 6 词 | `retrieval augmented generation citation graph hallucination` | 12 | 2 |
| 脚本文 freetext 9 词 | `citation graph constrained retrieval augmented generation ...` | 12 | 2 |
| 概念簇 OR 组合 | `(all:"retrieval augmented generation" OR all:"RAG") AND (...)` | 0 | 0 |

**结论修正**：早先我判断「`ti:` 首词限定是召回杀手」是**错的**——那次失败的真实原因是查询尾部的
`hallucination` 这类窄词把 AND 链卡死。同一组词去掉 `ti:` 命中数一样（5），说明 `ti:` 不是瓶颈，
**AND 链上的每个词都必须真实出现在目标论文里，这才是瓶颈**。

代价上更划算的是：**放宽词数上限不如放宽「必须全中」**。`all:"RAG" AND all:"citation graph"` 只用
2 个概念就拿到 4 条里 3 条 GOOD，精度远高于 5 词 AND 的 12 条里 5 条。

### 5.3 现在的代码改到哪了（未提交）
- `app/literature.py`：新增 `_RRF_K = 60`、`_rrf_fuse(per_query)`、`_concept_terms()`、
  `_ZH_CONCEPT_MAP`（14 条中文概念→英文字根）、`_covered()`、`pool_coverage_gaps()``；
  `build_candidates` 改为逐查询召回 + RRF 融合。
- `app/writing.py`：`_english_queries` 末尾插入 `_merge_english_queries(out)` 作为第 1 条查询；
  `build_framework_prompt(idea, pool, gaps)` 新增【覆盖提示】段；`check` 新增 `coverage_gaps`。

**实测效果不佳**：中文 idea 翻出来的合并查询是
`retrieval-augmented generation academic question answering`，被 `_ARXIV_MAX_WORDS = 5` 截断，
丢掉了 `citation` / `graph`——**最重要的概念被截掉了**；`pool_coverage_gaps` 返回 `[]`（判据太宽松，
SCiRAG/SF-RAG 标题里都有 retrieval/graph 等词，全被判为「已覆盖」）。

---

## 6. 可落地改法（按代价从低到高）

### P0：合并查询改成「概念簇」，别再拼散词（改 ~40 行，1 天）
现状 `_merge_english_queries` 去重后取前 5 个**单词**，等于随机挑前 5 个。改成：
- 让英译 prompt 直接产出 **2-4 个概念短语**（`retrieval augmented generation`、`citation graph`、
  `hallucination`），而不是一堆单词；
- 合并查询拼成 `all:"RAG" AND all:"citation graph"`（2-3 个概念 AND），**不截断短语**；
- 依据：上表实测 2 概念 AND 精度 3/4，5 散词 AND 只有 5/12。

### P1：`pool_coverage_gaps` 判据收紧（改 ~15 行，半天）
现在只要池里任意标题含 `retrieval` 就算「已覆盖」。改成：**概念必须出现在同一篇文献的标题里**
（不是全池拼起来算），并只用 `_ZH_CONCEPT_MAP` 里权重最高的 3-5 个概念判。否则这功能永远是 `[]`。

### P2：逐字支撑校验（改 ~80 行 + 每条引用多一次 LLM 调用，2-3 天）
给模型的 prompt 里要求：每挂一个 `[Ck]`，后面紧跟一段来自该文 abstract 的**逐字片段**
（`<q>...</q>`）。代码侧用 `normalized in abstract` 校验，不通过就把该编号降级为「无支撑」并渲染成灰色，
**不删编号**（删了句子会留空洞，PaperQA2 的教训）。
代价：token 涨约 30%，生成时间涨 20-40%。

### P3：接入 Semantic Scholar topic 检索作为第三源（改 ~60 行，1-2 天）
arXiv 的索引质量是瓶颈。S2 `/graph/v1/paper/search` 支持自然语言查询且带 `relevanceScore`，
PaperQA2 有这个 client 但没用（死代码）。**这是唯一能实质提升「一篇正主都没召回到」概率的办法。**
代价：需要 S2 API key（免费额度够用），引入一个新的外部依赖；限速要处理。

### 不建议做
- **cross-encoder rerank 候选池**：论文级重排需要把 abstract 全喂进模型，16 篇 × 300 词 = 4800 词/次，
  而 `app/rag/rerank.py` 现有的模型是段落级的，拿来判「标题+abstract 与 idea 的相关性」不可靠。
- **让模型自己查库**（AI-Scientist 的做法）：多轮 LLM 调用，且候选池不可控，与我们「候选集约束生成」
  的设计哲学冲突，答辩时也不好讲。

---

## 7. 一句话回答「别人怎么搞的」

别人（GPT Researcher / STORM / PaperQA2 / AI-Scientist）**在引用溯源上都比我们弱**——
它们最多保证「编号存在」，没有一个校验「论断是否被该文支持」。
它们的强项是**检索管线的工程化**（短路压缩、逐条打分、明确拒答），这部分值得抄。
但我们要解决的问题（候选池召回不准 → 模型硬凑引用）**没人直接回答**，
必须靠自己在 arXiv 查询构造上做实验，第 5 节那张表就是起点。