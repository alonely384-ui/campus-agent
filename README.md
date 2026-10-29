# 科研小助手（research-agent）

> 面向学术论文全流程的 AI 助手：PDF 管理 + 细粒度解析 + 抗幻觉 RAG 问答（逐句溯源）+ 阅读翻译 + 文献发现 + 引用图谱分析 + 写作助手
>
> 第九届传智杯 · AI大模型创新应用挑战赛 · 个人赛 · B 组参赛作品

<!-- ⚠️ 下面两行是你要填的。项目没上线之前先留着占位，别删。 -->
**🔗 在线体验**：（待部署后填入）
**🎬 演示视频**：（待上传后填入）

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

一个能"读论文、找原文、做对比、写综述"的助手，覆盖论文工作的完整链路（官方赛题 1.1 ~ 1.6 六个模块）：

| 模块 | 能力 | 页面 |
|---|---|---|
| 1.1 | PDF 上传、**内容级去重**（同篇不同版本自动识别）、细粒度解析（章节树/表格/图/公式/参考文献） | 论文库 |
| 1.2 | 单篇 / 多篇 RAG 问答，**逐句带原文出处与页码**，检索不到就拒答 | 对话 |
| 1.3 | PDF 阅读器：章节树、引用双向跳转、版式识别、划词翻译、**悬停翻译**、整篇翻译、中英对比 | 阅读页 |
| 1.4 | 基于 arXiv/GitHub 的文献发现与推荐，支持反馈调优与定时刷新 | 发现 |
| 1.5 | 引用关系图谱（PageRank / 中介中心性 / 社区发现）、基石节点识别、自动综述、前沿探索 | 论文分析 |
| 1.6 | 写作助手：框架生成（**候选集约束，结构性杜绝编造引用**）、架构图（Mermaid / Graphviz / TikZ）、CSV 数据可视化、中译英 | 写作助手 |

## 创新点

> 每一条都对应可核查的代码位置与已通过的真实测试，不是概念包装。

### 1. 抗幻觉 RAG：引用从生成结构上就不可能是假的

**问题**：让大模型写综述/框架时，它会凭空捏造参考文献——这是学术场景最致命的错误，事后用正则校验也拦不住（模型可以编出格式完全正确的假条目）。

**做法**（`app/writing.py`）：**候选集约束生成**。

1. 先用 idea 走真实检索（本地库 + arXiv + Crossref），得到候选池，编号 `C1..Cn`；
2. 候选池连同编号塞进 prompt，强制模型**只能引用池内的 key**，只写 `[C3]` 这样的标记；
3. **参考文献列表由代码从候选池取**（`format_reference` 是唯一能产生条目的地方，模型碰不到），作者、年份、DOI、arXiv 号全部来自真实元数据；
4. 事后 `check_citations` 校验悬空 key（`unknown_keys`），并报告未被用到的候选（`unused_candidates`）；
5. 候选池为空时**不硬生成**，直接返回「没有检索到任何真实文献，为避免编造引用，未生成框架」。

**为什么这个设计够硬**：模型根本没有"写完整条目"的能力，它只能写编号。结构上不可能编造，而不是靠提示词祈祷它别编。

配套细节：
- 作者超过 6 位按 IEEE 惯例截断加 `et al.`（`MAX_REF_AUTHORS = 6`），且处理了 `et al.` 与 `n.d.` 自带句点导致的 `et al..` 双句点；
- 本地文献作者名单从 `data/meta/structures.json` 补齐（`load_local_authors`），而不是显示"作者信息缺失"；
- 候选池会跨源去重：同一篇论文的 arXiv 版与本地 PDF 版按「归一化标题 ∪ arXiv 基础号」双键判重，不会在池里出现两次。

### 2. 引用图谱：合并副本 + 三层匹配 + 低置信度不入图

**问题**：本地库里同一篇论文常有多个版本（本库 39 个 PDF 里 《Attention Is All You Need》就有 4 份：v1/v7/两个扫描副本）。直接建图会把它的被引数拆成四份，PageRank 严重失真。

**做法**（`app/graph.py`）：

1. **文件 → 逻辑节点**：按「规范化标题 ∪ arXiv 基础号 ∪ 元数据交叉核验」合并副本为一个 `canonical_id` 节点，`copies` 保留全部文件名与访问链接，**不删任何原始 PDF**；
2. **三层参考文献匹配**：arXiv 号（归一前缀/空格/版本）> 完整规范化标题（大小写、空白、标点、Unicode、**连字**）> 标题近似（可配阈值）；
3. **低置信度不出边**：相似度落在 `[FUZZY_REVIEW, FUZZY_ACCEPT)` 之间（默认 `0.75 ~ 0.92`）的只记进「待人工核验」列表，**不生成边**——宁可少连，不可错连；
4. **三个指标分开统计**，不混为一谈：引用边数量 / 被引论文数量 / 不同引用论文数量。

归一化里踩过的真实坑，都在代码注释里写明了原因：
- 不能把标题里的空格全删（`A Survey of X` → `asurveyof…` 会和 `surveyof…` 撞键）；
- 自引必须先显式跳过（`self_hit`），否则条目精确命中自己时会被静默丢弃，再掉进模糊层撞上标题相近的别家论文，**凭空造边**；
- 精确匹配必须同时比词袋，只比归一化字符串会误连。

### 3. 多智能体协作：四个角色显式命名 + 状态回显，而不是四个模型互相聊天

**取舍**：参照 MA-RAG、paper-qa 的做法，但**没有**让四个 Agent 各自调用 LLM 对话。原因很实际——一次问答已经从 20 秒量级起，四角色互聊会涨到 2 分钟，演示直接超时，而收益（多角色讨论）在本场景并不成立。

**做法**（`app/agent.py`）：把本来就在跑的四个阶段**显式命名、各留可核验产物**，前端渲染成角色时间线：

| 角色 | 产物 |
|---|---|
| Planner 规划 | 检索计划 + 检索词（单篇聚焦 / 多篇对比 / 全库检索） |
| Retriever 检索 | 召回片段数、覆盖论文数、每篇取多少段 |
| Extractor 抽取 | 加工出的结构化材料（卡片/表格行/正文段）条数 |
| Verifier 校验 | 证据条数、其中多少条能定位到具体页码；异常时标记失败 |
| Writer 写作 | 轮次、字数 |

每步带 `duration_ms` 与 `ok`，失败会标红。顶部一行状态回显：`证据=4 · 覆盖论文=2 · 页码=2,11`——用户一眼知道这次回答有没有依据，而不用读完全文再判断。

### 4. 中译英面向投稿，不是随便翻一遍

`app/zh2en.py` 与阅读用的英译中（`app/translate.py`）**方向相反、提示词完全另写**：目标产物是能直接塞进 IEEE/ACM 模板的英文段落。七条硬规则（只输出译文 / 正式学术语域禁缩写 / 术语一致 / 长句拆短 / 数字单位引用标记与 LaTeX 原样保留 / 时态规矩 / 不增不减），并支持用户自定义术语表（`parse_glossary`），分块翻译时把上一块末尾 400 字作为上下文传递，保证术语连贯。

### 5. 工程细节：中文渲染与真实浏览器验证

- **Graphviz 中文全是豆腐块**的问题：默认字体 Times-Roman 不含 CJK 字形，渲染前注入 `graph/node/edge [fontname="Microsoft YaHei"]`（`_ensure_font`）；且不覆盖用户自己写 `fontname` 的代码。
- **mermaid-cli 的两处坑**：Windows 上 `shutil.which` 会先命中无扩展名的 Git Bash 脚本导致 `WinError 193`，改成手动按 `.cmd → .exe → .bat` 顺序找；12 版移除了 `-w` 参数，改用 `-s 2`。
- **渲染失败只回喂错误让模型改语法、不改结构**，重试一次即止。
- **图表自动选型**：数值列跨度 ≥ 100 倍才上对数轴（参数量 0.34B~540B 是 1600 倍，线性轴下小模型全挤原点；而准确率 86~93 不跨数量级，用对数轴反而误导）；雷达图必须逐列归一化到 0-1，否则大数值维度会吃掉整张图。
- **Fig Caption 只给模型统计摘要、不给图**，避免它描述出图上不存在的趋势；实测生成的 caption 里每个数字都能在统计摘要里对上。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
copy .env.example .env
#   然后编辑 .env，填入你的 LLM_API_KEY

# 3. 跑测试（不需要联网、不花钱）
pytest -v

# 4. 命令行体验
python -m app.cli "23*47 等于多少"
python -m app.cli            # 交互模式

# 5. 启动 Web 服务
python -m app.main
#   接口文档 http://127.0.0.1:8000/docs
#   聊天页面 http://127.0.0.1:8000/ui/
```

### 用 Docker 运行

```bash
docker build -t research-agent .
docker run -p 8000:8000 --env-file .env research-agent
```

## 架构

```
用户浏览器 (web/*.html)
   │  SSE / REST
   ▼
 FastAPI (app/main.py, 33 个接口)
   │
   ├─► Agent 主循环 (app/agent.py)        四角色：规划 → 检索 → 抽取 → 校验 → 写作
   │     └─► LLM (app/llm.py)             DeepSeek / 任意 OpenAI 兼容接口
   │
   ├─► RAG 管道 (app/rag/)                混合检索(dense + bm25) → RRF 融合 → 交叉编码器重排
   ├─► 文档解析 (app/structure.py, ocr.py, layout.py)
   ├─► 引用图谱 (app/graph.py)            去重建节点 → 三层匹配 → PageRank / 中介中心性 / Louvain
   ├─► 写作助手 (app/writing.py, diagram.py, chart.py, zh2en.py)
   ├─► 论文库   (app/papers.py)           内容级去重 + 版本归并
   └─► 文献发现 (app/online.py, recommend.py, topics.py, scheduler.py)
```

## 技术决策记录

> 面试官问"为什么这么做"时，答案都在这里。

| 决策 | 选择 | 理由 | 代价 / 何时会改 |
|---|---|---|---|
| 抗幻觉 | 候选集约束生成 | 事后校验拦不住格式正确的假引用，只能从生成结构上堵死 | 候选池质量依赖检索，检索不到就不生成 |
| 多智能体 | 四阶段显式命名，不让模型互聊 | 一问已 20 秒量级，四角色互聊涨到 2 分钟，演示超时 | 需要多角色辩论收益时再上真 multi-agent |
| 引用图谱匹配 | 三层 + 低置信度不入图 | 宁可少连不可错连，错边会让整个 PageRank 失真 | 有 48 条待人工核验，需人工过一遍 |
| 副本处理 | 合并为逻辑节点，不删文件 | 删文件会破坏原始数据完整性，且用户可能想对比版本 | 图里节点数 < 文件数是设计如此，不是 bug |
| 三维雷达 | ECharts 2D `radar` 三角坐标，不引 echarts-gl | 本地 vendor 是标准版无 GL；走 CDN 断网就白屏 | 真需 3D 时可单独引入 GL 包 |
| 图表轴 | 跨度 ≥ 100 倍才用对数轴 | 参数量跨 4 个数量级必须对数；准确率不跨数量级用对数是误导 | 阈值可在 `_needs_log` 调整 |
| 向量库 | Chroma | 数据量 <10 万条、单机部署，接入成本最低 | 已预留抽象层，触到瓶颈切 Qdrant 只改配置 |
| Agent 实现 | 先手写，不依赖框架 | 先搞懂框架解决了什么问题，避免只会调包 | 后续若上 LangChain 是为了维护性，不是不会写 |
| 流式方案 | SSE | 单向流式足够；比 WebSocket 轻，走 HTTP/2，断线自动重连 | 需要双向实时交互时才换 WebSocket |
| 计算工具安全 | AST 白名单解析 | 直接 `eval()` 会被注入任意代码执行 | 只支持四则/乘方/取余，够用 |
| 前端 | 原生 HTML/JS | 舞台是"能否公网访问且能演示"，不是"前端好看"；无构建步骤，部署即用 | 稳定后再考虑框架 |

## 项目进度

> 逐日开发记录（做了什么 / 证据 / 踩的坑）见 [docs/PROGRESS.md](docs/PROGRESS.md)。

- [x] 工程化骨架：分层目录、配置中心、日志、测试、CI
- [x] 手写 Agent 循环（工具调用 + 三种失败处理）+ 计算器工具（AST 白名单）
- [x] RAG 管道：PDF/TXT 载入 → 重叠切段 → Chroma 入库 → 混合检索 → 重排
- [x] 论文库：上传/列表/删除 + **内容级去重**（真实 v1/v7 实测 0.873 判重）
- [x] 细粒度解析：章节树、表格、图、公式、参考文献（39 篇实测 961 个章节）
- [x] PDF 阅读器：章节树、引用双向跳转、版式识别、划词/悬停/整篇翻译、中英对比
- [x] 文献发现：arXiv + GitHub 检索、推荐反馈、定时刷新
- [x] 引用图谱：副本合并、三层匹配、PageRank、中介中心性、社区发现、基石节点
- [x] 论文分析：自动综述、前沿探索（新近且被引 / 跨社区桥接 / 未被接入）
- [x] 写作助手：框架生成（候选集约束）、三种架构图渲染、CSV 可视化、中译英
- [x] 多智能体四角色时间线 + 状态回显
- [x] 真实浏览器端到端验证（六个模块全走一遍，控制台除 favicon 外无报错）
- [ ] Docker 镜像构建验证（本机未安装 Docker，Dockerfile 已提供但未实跑）
- [ ] 公网部署 + HTTPS

## 已知问题

- **引用图谱有 48 条低置信度匹配待人工核验**（相似度落在阈值之间，未计入图谱，避免误连）
- 跨章节提问时召回效果仍可优化（切分策略按段落，未按语义切）
- 对话历史暂存在前端，服务端未持久化
- `/favicon.ico` 返回 404（未放图标文件，不影响功能，浏览器会自动请求）
- 扫描件 PDF 依赖 OCR，速度比文本型 PDF 慢一个量级

## 目录结构

```
campus-agent/
├── app/
│   ├── main.py            # FastAPI 入口 + 33 个接口
│   ├── agent.py           # ⭐ 手写 Agent 循环 + 四角色时间线
│   ├── llm.py             # LLM 客户端封装
│   ├── config.py          # 配置中心（读 .env）
│   ├── rag/               # RAG：pipeline（混合检索+RRF）/ rerank（交叉编码器）
│   ├── structure.py       # 细粒度解析：章节/表格/图/公式/参考文献
│   ├── ocr.py             # 扫描件 OCR（RapidOCR）
│   ├── layout.py          # 版式识别（单栏/双栏）
│   ├── graph.py           # ⭐ 引用图谱：去重/三层匹配/PageRank/社区/综述/前沿
│   ├── writing.py         # ⭐ 写作助手：候选集约束的框架生成
│   ├── diagram.py         # 架构图：Mermaid / Graphviz / TikZ
│   ├── chart.py           # CSV → ECharts + matplotlib
│   ├── zh2en.py           # 中译英（投稿向）
│   ├── translate.py       # 英译中（阅读向）
│   ├── papers.py          # 论文库：上传/列表/删除/内容查重
│   ├── online.py          # arXiv / GitHub 在线检索
│   ├── recommend.py       # 推荐与反馈
│   ├── scheduler.py       # 定时刷新
│   ├── reader.py          # 阅读器后端
│   └── tools/             # 工具注册（calculator / registry）
├── web/                   # 前端页面（原生 HTML/JS，无构建）
│   ├── index.html         # 对话
│   ├── reader.html        # 阅读器
│   ├── graph.html         # 引用图谱
│   ├── analysis.html      # 论文分析
│   ├── writing.html       # 写作助手
│   ├── quick-translate.js # 划词浮钮组件（分析页/写作助手复用）
│   └── vendor/            # pdf.js、ECharts（本地化，不依赖 CDN）
├── tests/                 # 测试（不需要联网/API Key）
├── scripts/               # 构建与辅助脚本
├── docs/                  # 交接文档、调研报告、差距清单
├── data/                  # 论文库、解析结果、索引（不进 git）
├── Dockerfile
├── .env.example
├── requirements.txt
└── pyproject.toml
```

## 依赖与许可

本项目基于以下开源库构建（均为宽松许可）：

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
| networkx | 图算法（PageRank / 社区发现） | BSD-3-Clause |
| sse-starlette | SSE 流式输出 | MIT |
| pytest / ruff | 测试与静态检查 | MIT |

本项目自研部分：Agent 工具调用循环与四角色时间线（`app/agent.py`）、RAG 混合检索管道（`app/rag/`）、细粒度解析（`app/structure.py`）、引用图谱与论文分析（`app/graph.py`）、写作助手四个子模块（`app/writing.py` `app/diagram.py` `app/chart.py` `app/zh2en.py`）、论文库与内容查重（`app/papers.py`）、前端页面（`web/`）。

> 若后续引入第三方开源项目代码，会在 `THIRD_PARTY.md` 中注明来源、修改点与遵循的协议。

## License

MIT