# 科研小助手 · Research Agent

> 面向学术论文全流程的 AI 助手：PDF 管理与细粒度解析、可溯源 RAG 问答、阅读与翻译、文献发现、引用图谱分析、写作辅助。
>
> 后端 FastAPI，前端原生 HTML/JS 无构建步骤；论文、索引、对话数据全部落地本地文件，不引入外部数据库。

**在线实例**：<https://research-agent.top/ui/>（公开演示页无需登录；配置与库管理入口需管理员口令）

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-930%20passed-brightgreen)

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能模块](#功能模块)
- [技术要点](#技术要点)
- [快速开始](#快速开始)
- [架构](#架构)
- [工程规模](#工程规模)
- [技术决策记录](#技术决策记录)
- [安全与部署](#安全与部署)
- [已知问题](#已知问题)
- [目录结构](#目录结构)
- [依赖与许可](#依赖与许可)
- [License](#license)

## 它解决什么问题

读一篇论文时，真正花时间的往往不是读，而是这些事：这篇的公式在第几页、那张架构图出自哪篇、这个说法原文到底怎么写的、这十篇之间谁引了谁、写综述时哪些文献是真实存在的。

大模型能代劳其中大部分，但学术场景对错误零容忍——一段编造出来的参考文献足以毁掉一份工作。因此本项目的三个核心取舍是：

1. **答案必须能指回原文**：每条结论标注来源文件与页码，检索不到就不生成，而不是生成一段"看起来像"的话。
2. **引用必须在结构上不可能伪造**：模型只能写候选编号，参考文献条目由代码从真实元数据生成。
3. **低置信度不硬连**：引用图谱中相似度落在待核验区间的条目不生成边，宁可少连不可错连。

## 功能模块

| 模块 | 能力 | 页面 |
|---|---|---|
| 1.1 | PDF 上传、内容级去重（同篇不同版本自动识别）、细粒度解析（章节树 / 表格 / 图 / 公式 / 参考文献） | 论文库 |
| 1.2 | 单篇与多篇 RAG 问答，逐句带原文出处与页码，检索不到即拒答 | 对话 |
| 1.3 | PDF 阅读器：章节树、引用双向跳转、版式识别、划词翻译、悬停翻译、整篇翻译、中英对照 | 阅读页 |
| 1.4 | 基于 arXiv / GitHub 的文献发现与推荐，支持反馈调优与定时刷新 | 发现 |
| 1.5 | 引用关系图谱（PageRank / 中介中心性 / 引用关系簇划分）、基石节点识别、自动综述、前沿探索 | 论文分析 |
| 1.6 | 写作助手：框架生成（候选集约束）、架构图（Mermaid / Graphviz / TikZ，并可从库内 PDF 直接提取已有架构图）、CSV 数据可视化、中译英 | 写作助手 |

除上述六个模块外，另提供设置中心（配置在线热更新）、研究计划页、多篇对比与快速上手导览。

## 技术要点

> 每个要点后标注对应代码位置，便于核查。

### 1. 候选集约束生成：让模型在结构上无法编造引用

**问题**：让大模型写综述框架，它会凭空捏造参考文献；事后用正则或相似度校验拦不住——模型可以编出格式完全正确、DOI 与 arXiv 号一应俱全的假条目。

**做法**（`app/writing.py`）：

1. 先用研究主题走真实检索（本地库 + arXiv + Crossref），得到候选池，编号 `C1..Cn`；
2. 候选池连同编号写入 prompt，要求模型**只能引用池内的 key**，只产出 `[C3]` 这类标记；
3. **参考文献列表由代码从候选池组装**，`format_reference` 是唯一能产生条目的函数，模型无法接触到它；作者、年份、DOI、arXiv 号全部取自真实元数据；
4. 生成后由 `check_citations` 校验悬空 key（`unknown_keys`），并回报未被使用的候选（`unused_candidates`）；
5. 候选池为空时**不硬生成**，直接返回「没有检索到任何真实文献，为避免编造引用，未生成框架」。

这套约束的意义在于：模型的输出空间里根本不含"写一条完整文献条目"这个动作，防伪造靠的是生成结构，而非提示词。

配套细节（均在 `app/writing.py`）：

- 作者超过 6 位按 `MAX_REF_AUTHORS = 6` 截断并加 `et al.`，同时处理了 `et al.` 与 `n.d.` 自带句点导致的 `et al..` 双句点；
- 本地文献作者名单从 `data/meta/structures.json` 补齐（`load_local_authors`），不显示"作者信息缺失"；
- 候选池跨源去重：同一篇论文的 arXiv 版与本地 PDF 版按「归一化标题 ∪ arXiv 基础号」双键判重，池内不重复出现。

### 2. 引用图谱：副本合并、三层匹配与低置信度不入图

**问题**：本地库里同一篇论文常有多个版本（例如 *Attention Is All You Need* 在库内有 v1、v7 与扫描副本）。直接按文件建图会把它拆成多个节点，被引数随之分裂，PageRank 严重失真。

**做法**（`app/graph.py`）：

1. **文件 → 逻辑节点**：按「规范化标题 ∪ arXiv 基础号 ∪ 元数据交叉核验」把副本合并为一个 `canonical_id`，`copies` 字段保留全部文件名与访问链接，**不删除任何原始 PDF**；
2. **三层参考文献匹配**：arXiv 号（归一前缀、空格与版本号）> 完整规范化标题（大小写、空白、标点、Unicode、连字）> 标题近似（阈值可配）；
3. **低置信度不出边**：相似度落在 `[FUZZY_REVIEW, FUZZY_ACCEPT)`（默认 `0.75 ~ 0.92`，见 `data/meta/citation-graph.json` 的 `thresholds`）区间内的匹配只记入「待人工核验」列表，不生成边；
4. **三个指标分别统计**：引用边数、被引论文数、不同引用论文数，不合并为一个"引用次数"。

归一化过程中遇到并已在代码注释中写明原因的问题：

- 不能把标题中的空格全部删除（`A Survey of X` 会归一成 `asurveyof…`，与 `surveyof…` 撞键）；
- 自引必须先显式跳过（`self_hit`），否则条目精确命中自身时会被静默丢弃，随后落入模糊层匹配到标题相近的其他论文，**凭空造出一条边**；
- 精确匹配必须同时比较词袋，仅比较归一化字符串会误连。

### 3. 多智能体：四角色显式命名 + 状态回显 + 校验回环

**取舍**：参考 MA-RAG、paper-qa 的阶段划分，但**未**让四个 Agent 各自调用 LLM 互相对话。原因是一次问答已在数秒到二十秒量级，四角色互聊会放大到分钟级，演示与实用价值都不成立；多角色辩论的收益在本场景也不明显。

**做法**（`app/agent.py`）：把本就在执行的四个阶段显式命名，各留可核验产物，前端渲染为角色时间线。

| 角色 | 产物 |
|---|---|
| Planner 规划 | 检索计划与检索词（单篇聚焦 / 多篇对比 / 全库检索） |
| Retriever 检索 | 召回片段数、覆盖论文数、每篇取多少段 |
| Extractor 抽取 | 加工出的结构化材料（卡片 / 表格行 / 正文段）条数 |
| Verifier 校验 | 证据条数，其中多少条能定位到具体页码；异常时标记失败 |
| Writer 写作 | 轮次与字数 |

每步携带 `duration_ms` 与 `ok`，失败标红。顶部状态回显形如 `证据=4 · 覆盖论文=2 · 页码=2,11`，用户无需读完全文即可判断本次回答有无依据。

**校验回环**（`app/agent.py` 的 `_repair_unverifiable()`）：Verifier 判定证据不合格时驱动 Retriever 重检，按成本从低到高依次尝试，命中即止：

| 顺序 | 策略 | 动作 | 代价 |
|---|---|---|---|
| 1 | `page_fill` | 按 source 取该论文第一页正文，确认片段确实位于第 1 页才标页码 | 零（读本地缓存） |
| 2 | `rerank` | 更换排序路径（RRF 名次 ↔ 交叉编码器打分），把带页码的整页片段提上来 | 极低（复用已加载模型） |
| 3 | `widen` | `top_k` 翻倍重新检索 | 数秒 |

三条策略全部失败时如实上报：时间线保留 `ok=False` 并注明「试过 page_fill/rerank/widen 仍未补回」，不静默降级。补页码的标准同样保守——只有第一页正文确实包含该片段前 60 字才标注。

**边界**：当前库的检索结果均带页码，正常路径下该回环不触发；它是防御性设计，触发条件与三条策略由 `tests/test_rag_agent.py` 覆盖。

### 4. 中译英面向投稿，与阅读翻译分开实现

`app/zh2en.py`（中译英，面向投稿）与 `app/translate.py`（英译中，面向阅读）方向相反、提示词独立编写，不共用一个"翻译"提示词。中译英的目标产物是可直接放入 IEEE/ACM 模板的英文段落，约束为七条：只输出译文、正式学术语域、术语前后一致、长句拆分、数字单位与引用标记及 LaTeX 原样保留、时态规范、不增不减。支持用户自定义术语表（`parse_glossary`）；分块翻译时把上一块末尾 400 字作为上下文传递，保证术语连贯。

### 5. 图表生成：从类型选择到渲染兜底

- **Graphviz 中文渲染**：默认字体 Times-Roman 不含 CJK 字形，渲染前注入 `graph/node/edge [fontname="Microsoft YaHei"]`（`_ensure_font`），且不覆盖用户自行指定的 `fontname`。
- **mermaid-cli 的两处兼容问题**：Windows 上 `shutil.which` 会先命中无扩展名的 Git Bash 脚本导致 `WinError 193`，改为按 `.cmd → .exe → .bat` 顺序手动查找；12 版移除 `-w` 参数，改用 `-s 2`。
- **渲染失败只回喂错误信息让模型修正语法，不动结构**，重试一次即止。
- **轴类型自动判选**：数值列跨度达 100 倍以上才使用对数轴（参数量 0.34B~540B 跨 1600 倍，线性轴下小模型全部挤在原点；而准确率 86~93 不跨数量级，用对数轴反而误导）；雷达图逐列归一化到 0-1，避免大数值维度压缩其他维度。
- **图题生成只传统计摘要、不传图**，避免模型描述出图上不存在的趋势；实测生成的 caption 中每个数字都能在统计摘要中对应上。

### 6. 架构图不止生成，也可从库内 PDF 提取

让模型绘制架构图存在一个隐患：图能画出来，但模块名可能是编的。因此写作助手另有一路反向能力（`GET /writing/paper-figures`）：按论文名与关键词（默认 `architecture`）从**库内 PDF 中定位已有的架构图**，连同页码一并返回。被问到"这张图哪来的"时可直接指回原文第几页。

### 7. 设置中心：配置在线修改、即时生效

科研场景中换模型或调整检索参数往往需要登录服务器改 `.env` 并重启，而重启会中断进行中的翻译与写作任务，改错一个键还会导致服务起不来。

**做法**（`app/settings_api.py` + `app/config.py`），13 项配置在线可改：

1. **行级原子写 `.env`**：只修改目标行，其余内容原样保留；写临时文件后 `os.replace()`，避免写出半截文件；
2. **两处同步更新**：`os.environ` 优先级高于 `.env`，只写文件的话本进程读到的仍是旧值，因此 `_reload_setting()` 同时更新两处；
3. **密钥脱敏**：`GET /settings` 只返回 `sk-****abcd` 形式的掩码，前端拿不到明文；
4. **失败整体回滚**：逐项经 `canonical()` 归一化并通过类型与枚举校验后才落盘，中途失败整体回滚，不留半套配置；
5. **单项恢复默认**：`POST /settings/restore` 只重置指定 key，不提供"一键全清"。

热更新过程中遇到过的一个静默故障值得记录：`RAG_RERANK` 按规则推导出的属性名是 `_RERANK`，而管道中真实属性名为 `_RERANK_ENABLED`——推导错误不会报错，只会静默改为默认值。现在 `Setting` 增加了显式 `attr` 字段，`_apply_runtime` 中做 `hasattr` 检查，对不上即抛异常，不允许静默。

### 8. 前端设计系统：单一事实源

各页面原先各写各的颜色，改一次主题需要改多处且必然遗漏。现收敛为单一事实源：

- `web/design-system.css` 定义**192 个 CSS 变量**（品牌色、语义色、间距、圆角、阴影、字号、角色色、图表色）；
- `web/chart-colors.js` 让 ECharts 直接读取 CSS 变量，图表配色随主题变化，不再手写十六进制值；
- 迁移后主要功能页（对话 / 论文库 / 阅读器 / 发现 / 图谱 / 分析 / 写作 / 设置）硬编码色值仅剩 19 处，且均为 `#fff` 与带 fallback 的写法；
- 迁移过程中修正了两个真实缺陷：`index.html` 引用了未定义的 `--line`（改为 `--border-strong`）；`--role-isolated-fg` 对比度仅 2.31:1（改为 `#5c6672`，5.30:1）；
- 十二个角色标签对比度实测均 ≥ 4.5:1（cornerstone 6.37 / hub 7.15 / bridge 6.41 / foundation 6.78 / fringe 5.30 / isolated 5.30）。

### 9. 同步阻塞移出事件循环

FastAPI 是异步框架，但接口中一旦直接调用同步阻塞函数（读盘、跑模型、同步 HTTP），整个事件循环会被卡住，此时所有请求都在排队——不仅是慢接口，连 `/health` 也要等待。

将同步调用搬入 `run_in_threadpool` 后，并发场景下 `/health` 响应时间由 **2263 ms** 降至 **9 ms**。此类问题在单人功能测试中不会暴露，上线即显现。

### 10. 桌面形态

整套系统（含全部后端模块与前端页面）可打包为 Windows 单文件可执行程序，双击运行，无需安装 Python 或配置环境。

| 指标 | 值 |
|---|---|
| 单文件体积 | 452.9 MB |
| 冷启动 | 8.7 秒 |

三个关键设计：**数据目录外置**（`CAMPUS_DATA_DIR`）与**配置目录外置**（`CAMPUS_ENV_FILE`），保证更新程序时不触碰用户论文与对话记录，用户 API Key 既不被打进可执行文件、也不会在覆盖安装时丢失；此外补齐日志文件与启动横幅，出错不再是无提示的静默退出。

大模型接入层走标准 OpenAI 兼容协议，因此既可直接接云端厂商，也可将 `LLM_BASE_URL` 指向本机推理服务（Ollama / vLLM 等）。已实测接入 Ollama 跑通完整 Agent 链路（检索 + 四角色 + 写作），模型、检索、渲染全在本机，论文与对话不出内网。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备配置
copy .env.example .env
#    然后编辑 .env，填入 LLM_API_KEY

# 3. 跑测试（不需要联网、不消耗 API 额度）
pytest tests -q

# 4. 启动 Web 服务
python -m app.main
#    页面  http://127.0.0.1:8000/ui/
#    接口文档 http://127.0.0.1:8000/docs（APP_ENV=prod 时关闭）
```

### 接入哪些模型

大模型接入层走标准 OpenAI 兼容协议，不锁定单一厂商。设置中心内置七家厂商预设，选中后写入 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 三项并立即生效，无需重启：

| 厂商 | 接口地址 | 可选模型 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat`、`deepseek-reasoner` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini`、`gpt-4o`、`gpt-4.1-mini` |
| 通义千问（阿里云百炼） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`、`qwen-turbo`、`qwen-max`、`qwen-long` |
| Kimi（月之暗面） | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` / `-32k` / `-128k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus`、`glm-4-flash`、`glm-4-air` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `DeepSeek-V3`、`Qwen2.5-72B-Instruct` 等 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | 本机已 pull 的任意模型 |

该表非白名单——任何 OpenAI 兼容接口均可使用，手填 `LLM_BASE_URL` 与模型名即可（新模型的出现频率高于程序发版）。本地 Ollama 不需要真实 Key，占位串即可。

API Key 的三条约定：

- **只存本地**：Key 写在 `.env`，不进 git、不打进可执行文件；桌面版另有 `CAMPUS_ENV_FILE` 指向用户目录，覆盖安装不会丢失；
- **读不回来**：`GET /settings` 只返回掩码，前端拿不到明文；
- **改不坏**：切换厂商时三项一次写入，中途失败整体回滚，不会留下"A 厂商地址配 B 厂商 key"的无效组合。

配置完成后可点**测试连接**（`POST /settings/test-connection`）：使用当前生效配置真实发起一次最小请求（`max_tokens=1`），返回连通性、真实延迟与上游响应；失败时把上游原文转为可读说明（key 错误、余额不足还是模型名不存在），错误信息中不拼接 key 本身。用于排查"保存显示成功、实际连不上"的情况。

### 使用 Docker 运行

```bash
docker build -t research-agent .
docker run -p 8000:8000 --env-file .env research-agent
```

> 本机未安装 Docker，Dockerfile 已提供但未实跑验证。

## 架构

```
用户浏览器（web/*.html，原生 HTML/JS）
   │  SSE / REST
   ▼
安全中间件 security_guard（app/main.py）
   │  认证 → 限流 → 体积
   ▼
FastAPI 路由层（app/main.py）
   │
   ├─► Agent 主循环（app/agent.py）        四角色：规划 → 检索 → 抽取 → 校验 → 写作
   │     └─► LLM（app/llm.py）             DeepSeek / 任意 OpenAI 兼容接口
   │
   ├─► RAG 管道（app/rag/）                三路召回（哈希向量 + 稠密向量 + BM25）→ RRF 融合 → 交叉编码器重排
   ├─► 文档解析（app/structure.py, ocr.py, layout.py）
   ├─► 引用图谱（app/graph.py）            副本合并 → 三层匹配 → PageRank / 中介中心性 / 引用关系簇
   ├─► 写作助手（app/writing.py, writing_evidence.py, diagram.py, chart.py, zh2en.py）
   ├─► 论文库（app/papers.py, buckets.py）  内容级去重 + 版本归并
   └─► 文献发现（app/literature.py, online.py, recommend.py, topics.py, scheduler.py）
```

**存储形态**：不引入关系型数据库。论文 PDF 存于 `data/papers/`，解析结果、目录、图谱、推荐结果均为 `data/` 下的 JSON；向量检索使用 Chroma（`data/chroma/`），同时维护 `bm25.json` 与 `idf.json` 支撑关键词通道。

## 工程规模

所有数字均来自本仓库当前状态，附复现命令。

| 项 | 数据 | 复现方式 |
|---|---|---|
| 代码规模 | `app/` 25589 · `web/` 15376 · `tests/` 14043 · `scripts/` 5380，合计 **60388 行** | `python scripts/count_lines.py` |
| 文件分布 | `app/` 42 个 py；`web/` 10 个 html + 6 个 js/css；`tests/` 55 个 py；`scripts/` 35 个 py | 同上 |
| 自动化测试 | **930 passed, 1 skipped**（104.55s） | `pytest tests -q` |
| 静态检查 | 全部通过 | `ruff check .` |
| 安全路由对账 | 管理 14 条 · 公开 49 条 · 按方法区分 1 条 | `app/security.py` 的 `ADMIN_PATHS` / `PUBLIC_PATHS` / `METHOD_ADMIN_PATHS`；启动时双向校验，未登记路由拒绝启动 |
| 论文库 | 44 个 PDF；解析记录 46 条，共 2096 页（最大单篇 *Understanding Deep Learning* 541 页） | `ls data/papers` / `data/meta/structures.json` |
| 解析产物 | 章节 2314 · 表格 560 · 图 569 · 公式 3438 | `data/meta/structures.json` |
| 论文分类 | 40 条目录记录，7 个类目（基础模型与预训练 14 / 模型架构 8 / 对齐与推理 5 / 训练效率与微调 5 / 多模态模型 4 / 能力评测与规模 3 / 教材与理论 1），全部已建索引 | `data/catalog.json` |
| 引用图谱 | 41 条索引记录（唯一文件 40）→ **38 个逻辑节点 / 210 条边**；被引论文 31 / 引用论文 34 / 孤立节点 2；合并副本 3 处 | `data/meta/citation-graph.json` 的 `summary` |
| 参考文献匹配 | 3681 条条目：arXiv 号命中 91 · 精确标题命中 120 · 模糊命中 3（已入图）· 待人工核验 1295（其中标题近似 23）· 库内无此篇 1272 | 同上，`summary.refs` |
| 前端设计系统 | 192 个 CSS 变量；主要功能页硬编码色值 19 处 | `web/design-system.css` |

## 技术决策记录

| 决策 | 选择 | 理由 | 代价 / 何时会改 |
|---|---|---|---|
| 抗幻觉 | 候选集约束生成 | 事后校验拦不住格式正确的假引用，只能从生成结构上堵死 | 候选池质量依赖检索；检索不到就不生成 |
| 多智能体 | 四阶段显式命名 + Verifier 驱动 Retriever 回环，不让模型互聊 | 单次问答已在秒级，四角色互聊放大到分钟级，收益不成立 | 需要多角色辩论收益时再上完整 multi-agent |
| 引用图谱匹配 | 三层匹配 + 低置信度不入图 | 宁可少连不可错连，错边会让整个 PageRank 失真 | 现有 23 条标题近似分数不足，留待人工核验 |
| 副本处理 | 合并为逻辑节点，不删文件 | 删文件会破坏原始数据完整性，且用户可能需要对比版本 | 图中节点数少于文件数属设计如此 |
| 三维雷达 | ECharts 2D `radar` 三角坐标，不引入 echarts-gl | 本地 vendor 为标准版无 GL；走 CDN 则断网白屏 | 真需 3D 时单独引入 GL 包 |
| 图表坐标轴 | 跨度 ≥ 100 倍才用对数轴 | 参数量跨 4 个数量级必须对数；准确率不跨数量级用对数是误导 | 阈值在 `_needs_log` 中可调 |
| 向量库 | Chroma | 数据量小于 10 万条、单机部署，接入成本最低 | 已预留抽象层，触到瓶颈切 Qdrant 只改配置 |
| Agent 实现 | 先自研，不依赖框架 | 先弄清框架解决了什么问题，避免只会调包 | 后续若引入 LangChain 是为维护性，非能力缺口 |
| 流式方案 | SSE | 单向流式足够；比 WebSocket 轻，走 HTTP/2，断线可重连 | 需要双向实时交互时再换 WebSocket |
| 计算工具安全 | AST 白名单解析 | 直接 `eval()` 会引入任意代码执行 | 只支持四则、乘方、取余 |
| 前端 | 原生 HTML/JS，无构建步骤 | 目标是可公网访问且能演示；无构建步骤则部署即用 | 复杂度上升后再考虑框架 |
| 存储 | JSON 文件 + Chroma，不引入数据库 | 单机、单用户、数据量可控，减少部署依赖 | 多用户或并发写入需求出现时需重新评估 |

## 安全与部署

### 启动即对账的路由安全模型

公开演示与管理保护需要同时成立：仓库公开、服务可演示，但任何人都能打开配置页改 API Key、删论文库显然不行。做法不是关闭入口，而是让"漏配"在启动时立即暴露。

**路由对账**（`app/main.py` 的 `_assert_route_coverage()`）：启动时遍历全部路由，逐条归类为「管理」或「公开」。任何一条路由既不在管理组也不在公开组，**服务直接拒绝启动**。新增接口时忘记加鉴权是此类漏洞的首要来源，把"人可能忘"的事变成"机器必须过"的关。

**中间件顺序**（`app/main.py` 的 `security_guard()`）：认证 → 限流 → 体积。顺序有实际后果：先认证可以让未授权请求尽早被拒，不必为它做限流记账；先限流再校验体积，可以避免攻击者用超大请求体消耗解析开销。

**未登记路径返回 404 而非 401**：返回 401 等于告知对方"这里确实有接口"。安全层未映射的路径一律 404。

**管理员认证**：`ADMIN_TOKEN` 与 `ADMIN_COOKIE_SECRET` 必须同时配置，缺一即管理接口返回 503（而非放行）。会话 Cookie 为 `HttpOnly` + `SameSite=Strict`，`APP_ENV=prod` 时附加 `Secure`，有效期 2 小时。

### 公网实例实测

以下结果来自 `https://research-agent.top`，2026-11-17 实测：

| 请求 | 结果 |
|---|---|
| `GET /docs` `/redoc` `/openapi.json` | `404`（`APP_ENV=prod` 下文档端点关闭） |
| `GET /settings`（未登录） | `401` |
| `GET /admin` | `404` |
| `POST /chat`（2 MB 请求体） | `413` |
| `GET /ui/graph.html` | `200` + `Cache-Control: no-cache` |
| `GET /graph`（`Accept-Encoding: gzip`） | `Content-Encoding: gzip`，未压缩体积 100464 字节 |
| `GET /health` | `200`，响应头含五项安全头 |

响应头实测值：

```
Strict-Transport-Security: max-age=31536000
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline';
                         style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'
Server: nginx（版本号已隐藏）
```

CSP 中不含任何外部域名——前端 JS、CSS、ECharts、pdf.js 全部本地化（`web/vendor/`），不依赖 CDN，断网环境下功能不受影响。

### 资源限额

按 IP 令牌桶限流（分档标定：对话 12 次/分钟、突发 5 次）+ 登录失败锁定 + 每日调用次数闸（`LLM_DAILY_BUDGET=800`）+ 并发上限（`LLM_MAX_CONCURRENCY=4`）。请求体按类型分档（JSON 1 MB / 上传 24 MB），体积检查在接收流上边读边计，超限立即中断而非读完再判断。

已知边界：限流状态存于进程内存，服务重启即清空，多 worker 部署时不共享。

## 已知问题

- **引用图谱有 23 条低置信度匹配待人工核验**（标题相似度落在 0.75~0.92 之间，未计入图谱，避免误连）；另有 1272 条条目的 arXiv 号库内没有、也找不到同标题论文，属正常缺口。
- 跨章节提问时召回效果仍有优化空间（切分按段落，未按语义切）。
- 对话历史只存在浏览器 `localStorage`，服务端未持久化——换浏览器或换设备看不到历史。这是有意为之（不引入用户体系），非遗漏。
- 扫描件 PDF 依赖 OCR，速度比文本型 PDF 慢一个量级。
- 限流状态与每日预算计数存于进程内，多 worker 部署时不共享。
- Dockerfile 已提供但未实跑验证。

## 目录结构

```
campus-agent/
├── app/
│   ├── main.py            # FastAPI 入口 + 路由层 + security_guard 中间件
│   ├── security.py        # 路由归类、令牌桶限流、每日预算、IP 解析
│   ├── agent.py           # 自研 Agent 循环 + 四角色时间线 + 校验回环
│   ├── llm.py             # LLM 客户端封装
│   ├── llm_retry.py       # 错误分类与指数退避重试
│   ├── config.py          # 配置中心（读 .env + 行级原子写回）
│   ├── settings_api.py    # 设置中心：热更新 / 密钥脱敏 / 单项恢复默认
│   ├── kb_health.py       # 知识库健康检查与幽灵 source 清理
│   ├── rag/               # RAG：pipeline（三路召回 + RRF）/ rerank（交叉编码器）
│   ├── structure.py       # 细粒度解析：章节 / 表格 / 图 / 公式 / 参考文献
│   ├── ocr.py             # 扫描件 OCR（RapidOCR）
│   ├── layout.py          # 版式识别（单栏 / 双栏）
│   ├── graph.py           # 引用图谱：副本合并 / 三层匹配 / PageRank / 引用关系簇 / 综述 / 前沿
│   ├── local_graph.py     # 本地图谱计算
│   ├── writing.py         # 写作助手：候选集约束的框架生成
│   ├── writing_evidence.py# 写作证据抽取
│   ├── diagram.py         # 架构图：Mermaid / Graphviz / TikZ
│   ├── chart.py           # CSV → ECharts + matplotlib
│   ├── zh2en.py           # 中译英（投稿向）
│   ├── translate.py       # 英译中（阅读向）
│   ├── papers.py          # 论文库：上传 / 列表 / 删除 / 内容查重
│   ├── buckets.py         # 大类管理与归属
│   ├── literature.py      # 文献发现
│   ├── online.py          # arXiv / GitHub 在线检索
│   ├── recommend.py       # 推荐与反馈
│   ├── topics.py          # 主题分组
│   ├── scheduler.py       # 定时刷新
│   ├── reader.py          # 阅读器后端
│   └── tools/             # 工具注册（calculator / registry）
├── web/                   # 前端页面（原生 HTML/JS，无构建）
│   ├── index.html         # 对话（+ 侧栏历史）
│   ├── library.html       # 论文库（分页 + 按主题分组）
│   ├── discover.html      # 文献发现
│   ├── reader.html        # 阅读器
│   ├── graph.html         # 引用图谱
│   ├── analysis.html      # 论文分析
│   ├── writing.html       # 写作助手
│   ├── compare.html       # 多篇对比
│   ├── settings.html      # 设置中心
│   ├── admin-login.html   # 管理员登录
│   ├── guide.html         # 快速上手
│   ├── study-plan.html    # 研究计划
│   ├── project-tour.html  # 项目导览
│   ├── design-system.css  # 设计系统（192 个 CSS 变量）
│   ├── chart-colors.js    # ECharts 读 CSS 变量，图表配色与主题联动
│   ├── sidebar.js         # 侧栏会话历史与折叠
│   ├── chat-core.js       # 对话核心逻辑
│   ├── quick-translate.js # 划词 / 悬停浮钮组件（阅读页、分析页、写作助手复用）
│   └── vendor/            # pdf.js、ECharts（本地化，不依赖 CDN）
├── tests/                 # 测试（不需要联网 / API Key）
├── scripts/               # 构建与辅助脚本（解析、索引、评测、打包）
├── docs/                  # 交接文档、安全审查、评测基线、进度记录
├── data/                  # 论文库、解析结果、索引（不进 git）
├── Dockerfile
├── .env.example
├── requirements.txt
└── pyproject.toml
```

## 依赖与许可

| 库 | 用途 | 许可 |
|---|---|---|
| FastAPI | Web 接口 | MIT |
| uvicorn | ASGI 服务器 | BSD-3-Clause |
| pydantic / pydantic-settings | 配置与请求校验 | MIT |
| openai | LLM 客户端（兼容 DeepSeek / 通义等） | Apache-2.0 |
| chromadb | 向量存储 | Apache-2.0 |
| pypdf / pymupdf | PDF 文本抽取 | BSD-3-Clause / AGPL-3.0 |
| pdf.js | 前端 PDF 渲染 | Apache-2.0 |
| ECharts | 图表可视化 | Apache-2.0 |
| numpy / pandas / matplotlib | 数据处理与图表导出 | BSD-3-Clause |
| sse-starlette | SSE 流式输出 | MIT |
| pytest / ruff | 测试与静态检查 | MIT |

本项目自研部分：Agent 工具调用循环与四角色时间线（`app/agent.py`）、RAG 检索管道（`app/rag/`）、安全中间件与路由对账（`app/security.py` + `app/main.py`）、细粒度解析（`app/structure.py`）、引用图谱与论文分析（`app/graph.py`）、写作助手四个子模块（`app/writing.py` `app/diagram.py` `app/chart.py` `app/zh2en.py`）、论文库与内容查重（`app/papers.py`）、前端页面与设计系统（`web/`）。

> 若后续引入第三方开源项目代码，会在 `THIRD_PARTY.md` 中注明来源、修改点与遵循的协议。

## License

MIT