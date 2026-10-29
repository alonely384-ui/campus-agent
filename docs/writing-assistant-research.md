# 写作助手（1.6）技术调研

> 官方赛题模块 1.6，四子项。本文档给出各子项的主流实现对照、**本机实测可行性**、推荐方案与工作量。
> 环境实测时间 2026-10-29，所有版本号与渲染结果均为现场跑出来的，不是查资料抄的。

---

## 0. 先说结论

**四子项全都可以做，本机环境已经全部就位。** 原本最大的拦路虎是「生成 TikZ / Graphviz / Mermaid 代码之后，评委要看效果怎么办」——现在本机 LaTeX、Graphviz、Mermaid、matplotlib 都装好且**实测渲染通过**，四条路都能当场出图。

如果时间紧到只能选一两个，按这个顺序：
**1.6.3 CSV 数据可视化 > 1.6.4 中译英 > 1.6.1 论文框架生成 > 1.6.2 拓扑图生成**。
理由见第 6 节。

---

## 1. 本机环境现状（实测）

### 已经装好并验证可用的

| 工具 | 版本 | 验证方式 | 结果 |
|---|---|---|---|
| MiKTeX / pdflatex | MiKTeX 25.12，pdfTeX 4.23 | 真编译一段带 TikZ 的 tex | **1 页 PDF，14879 字节** |
| Graphviz / dot | 16.1.0 | `dot -Tpng` 渲染 digraph | **PNG 3883 字节** |
| Node.js / npm | 26.13.0 / 11.20.0 | `node -v` / `npm -v` | 正常 |
| mermaid-cli / mmdc | 12.0.0 | `mmdc -i a.mmd -o a.png` | **PNG 3663 字节** |
| pandas | 3.1.0 | `import pandas` | 正常 |
| matplotlib | 3.11.2 | `import matplotlib` | 正常 |
| numpy | 2.5.4 | 原本就有 | 正常 |
| networkx | 3.7 | 原本就有 | 正常 |

### 安装时踩的坑（接手时会再遇到）

1. **Graphviz 安装程序不会自己进 PATH**。装完 `dot` 找不到，要手动把 `C:\Program Files\Graphviz\bin` 写进用户 PATH。已处理并验证。
2. **`mmdc` 的 `.ps1` 被 PowerShell 执行策略拦住**。必须调 `C:\Users\alonely\AppData\Roaming\npm\mmdc.cmd`，不能直接敲 `mmdc`。
3. **`dot -V` 把版本打到 stderr**。PowerShell 会当成 `NativeCommandError` 抛出来，看着像报错其实是正常的。要判断真的成功，看**有没有产出文件**，别看退出码。
4. **MiKTeX 首次编译 TikZ 会自动下载宏包**，第一次慢（几秒到几十秒），之后快。演示前务必预热一次，否则现场等下载。
5. npm 装 mermaid-cli 时 puppeteer 的 postinstall 脚本被 npm 的 `allowScripts` 策略跳过，但 Chromium 仍然可用（实测渲染成功），暂时不用管。

### PATH 状态

| 工具 | 是否已在用户 PATH |
|---|---|
| MiKTeX bin | ✓ 安装程序自动加的 |
| Graphviz bin | ✓ **手动补的** |
| nodejs | ✓ |
| npm 全局目录 | ✓ |

---

## 2. 子项 1：论文框架生成

### 官方原文

> 给定一个 idea，让论文自动生成论文的框架，包括：Abstract、Introduction、Related Work 和 References 部分。确保 References 是真实没有幻觉。

**注意**：赛题把「References 真实没有幻觉」写进了要求里，这在评分时是对得上的（AI 技术深度 35% 里含 RAG 架构，创新性 15%）。这是本子项的真正难点，不是框架本身。

### 主流实现对照

| 项目 | 定位 | 优点 | 缺点 | 对本项目的价值 |
|---|---|---|---|---|
| [mmlong818/open-academic-paper-gen](https://github.com/mmlong818/open-academic-paper-gen) | 多 Agent 流水线：主题 → 真实文献支撑的初稿，自带引用幻觉检测 | **和赛题要求几乎一对一**，有 hallucination detection 和 claim-support 检查 | 体量偏大，整套搬进来不划算 | 流程设计可直接借鉴 |
| [vedangvatsa/research-paper-framework](https://github.com/vedangvatsa/research-paper-framework) | 论文框架生成的轻量实现 | 结构简单，易读 | 未见引用真实性校验 | 框架模板可参考 |
| [Future-House/paper-qa](https://github.com/Future-House/paper-qa) | agentic RAG + 文中引用 + LLM 重排 | 引用溯源做得最扎实 | 依赖外部 API（S2/Crossref/Unpaywall） | 引用校验思路可借鉴 |
| [wanshuiyin/Auto-claude-code-research-in-sleep](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep) | 含 `tools/verify_papers.py` 论文校验脚本 | 有一个现成的「验证论文是否真实存在」脚本 | 是 Skill 形态，非库 | `verify_papers.py` 逻辑值得读 |

### 引用真实性怎么保证（关键设计）

**三层校验，从强到弱：**

| 层 | 方法 | 判定 | 成本 |
|---|---|---|---|
| 1 | 查本库 | 本项目已有 `data/catalog.json`（39 篇）+ `data/meta/citation-graph.json`。命中的直接给真实元数据 | 0 成本，本地 |
| 2 | 查 arXiv API | `http://export.arxiv.org/api/query?search_query=ti:"..."` 实测可达（本会话验证过返回正常 XML） | 免费，需网络 |
| 3 | 查 Crossref | `https://api.crossref.org/works?query.bibliographic=...` 覆盖非 arXiv 的期刊/会议 | 免费，需网络 |

**输出要求**：LLM 只能从「候选中挑」，不能自己写参考文献。流程是：
```
idea → LLM 提取关键词 → 检索（本库 + arXiv）→ 拿到 N 篇真实文献
     → LLM 只负责从中挑选并组织 Related Work 叙述
     → 输出时每条引用附 [verified: local / arxiv / unverified]
```
**凡是没有通过校验的，一律不进 References**，宁可少几条。

这个「候选集约束生成」的做法是抗幻觉的核心，比事后校验更可靠——因为模型根本没机会编。

### 复用点

- `data/catalog.json`：39 篇本地元数据的现成来源
- `data/meta/citation-graph.json`：能从已引文献里反查「这篇引过谁」，作为候选来源
- `app/agent.py` 的 Function Calling 机制：可以把「检索文献」做成一个 tool

### 工作量

**2–3 人天**。框架生成本身 0.5 天，引用校验链 1.5 天（含 arXiv/Crossref 检索、去重、缓存），前端 0.5–1 天。

### 风险

- arXiv API 有速率限制，连续请求会被 429。需加缓存 + 间隔。
- 「Related Work 写得像人写的」比「引用是真的」难得多。建议不要追求华丽，追求**每条论断都能指到具体文献**。

---

## 3. 子项 2：论文架构拓扑图生成

### 官方原文

> 让 Agent 可以根据论文逻辑，直接生成高质量的 TikZ (LaTeX 绘图代码)、Graphviz (DOT) 或 Mermaid / Python Matplotlib 绘图脚本。

四选一，或者全给。**本机四条路现在都能出图**，这是本次装环境最大的收益。

### 四条路对比（本机实测）

| 方案 | 本机可用 | 出图方式 | 优点 | 缺点 | 演示友好度 |
|---|---|---|---|---|---|
| **Mermaid** | ✓ mmdc 12.0.0 | `mmdc -i x.mmd -o x.png` | **LLM 最擅长写**，语法简单不易错；渲染最快；也能直接在浏览器里用 JS 渲染（不用装 CLI） | 图形能力弱，画不出复杂架构图 | ★★★★★ |
| **Graphviz DOT** | ✓ dot 16.1.0 | `dot -Tpng x.dot -o x.png` | 自动布局，节点多也不乱；LLM 写得较稳 | 样式偏「工程图」，学术海报感弱 | ★★★★ |
| **TikZ** | ✓ MiKTeX 25.12 | `pdflatex x.tex` → PDF | **学术论文标配**，出图最专业 | LLM 写 TikZ 容易语法错；编译慢；排版调参费时 | ★★★ |
| **Matplotlib** | ✓ 3.11.2 | 直接存 PNG | 图表类（曲线/柱状）质量高 | 不适合画「架构框图」 | ★★★ |

### 推荐方案

**主推 Mermaid，Graphviz 备用，TikZ 作为「学术模式」加分项。**

理由：
1. **Mermaid 是 LLM 生成成功率最高的**。语法宽容、结构简单，模型很少写错。演示时「点一下立刻出图」的概率最高。
2. **Mermaid 可以在浏览器端直接渲染**，不需要后端跑 CLI。虽然有 CLI 也能用，但纯前端方案少一次进程调用、少一次超时风险。
3. TikZ 一定要留，因为赛题点名了它，而且「能直接贴进 LaTeX 论文」是真需求。但**演示时别以它为主路径**——LLM 写 TikZ 出错率高，现场翻车成本太大。

### 一个具体可行的实现思路

```
用户描述逻辑 → LLM 生成 (mermaid 源码, dot 源码, tikz 源码) 三份
            → 后端分别渲染成 PNG
            → 前端并列展示三份代码 + 三张图
            → 用户挑一个下载
```
能力上「四选一」变成「全给」，答辩时是加分项。渲染失败的就标红显示错误，不假装成功。

### 复用点

- `app/tools/` 的工具注册机制：`render_diagram` 可以做成一个 `BaseTool` 子类，注册两行即可
- 渲染产物可落到 `data/meta/figures/`（注意：该目录目前没被 gitignore，见 1.5 交接文档 7.2）

### 工作量

**1.5–2 人天**。接三种渲染器各 0.3 天，LLM prompt 调优 0.5 天，前端 0.5 天。

### 风险

- **TikZ 编译可能超过 HTTP 超时**。首次编译要下宏包。必须缓存宏包（编译一次预热）+ 设置超时降级。
- **LLM 生成的代码必须做安全过滤**：TikZ 的 `\input` / `\write18`、Mermaid 的 `click` 回调、DOT 的 `image=` 都可能被用来读文件。生成后要正则拦截这些指令再编译。

---

## 4. 子项 3：CSV 数据可视化

### 官方原文

> 用户上传实验产出的原始 CSV 数据，Agent 自动将其转化为的表格、折线图、柱状图或三维雷达图等。同时，自动为该图生成符合学术规范的 Figure Caption（图注说明），不仅说明图表内容，还自动提炼图中的核心趋势结论。

**两个要求，第二个才是难点**：不只要出图，还要**自动提炼趋势结论**。

### 主流实现对照

| 项目 | 定位 | 优点 | 缺点 | 对本项目的价值 |
|---|---|---|---|---|
| [microsoft/lida](https://github.com/microsoft/lida) | 微软的自动可视化 + 信息图生成 | 有完整的「数据摘要 → 图表规格 → 生成」流程，最成熟 | 体量大，依赖多 | **数据摘要思路最值得抄** |
| [mljar/plotai](https://github.com/mljar/plotai) | 用 LLM 写 matplotlib 代码 | 极轻量，几十行就能借鉴 | 不生成 caption | 代码生成范式可参考 |
| [pleiadian53/agentic-ai-lab/chart_agent](https://github.com/pleiadian53/agentic-ai-lab/tree/7c456daa6a62678e1e7fa376eb3e03a717838f69/chart_agent) | 图表 agent 的示例实现 | 结构清晰，适合读 | 项目影响力小 | 流程参考 |

### 出图路线：ECharts 还是 matplotlib？

| 维度 | ECharts（推荐） | matplotlib |
|---|---|---|
| 本机依赖 | **零新增**（已本地化 1MB 副本） | 已装，但要配中文字体，否则中文全是方块 |
| 三维雷达图 | `radar` 组件**支持** | 要额外配，且 matplotlib 的 3D 雷达不是原生能力 |
| 交互 | 天然支持（悬浮、缩放、导出） | 静态图片 |
| 服务器负载 | 前端渲染，后端不耗 CPU | 后端渲染，要起绘图进程 |
| 演示效果 | **更好看**，可交互 | 看情况 |

**结论：走 ECharts。** 赛题要的「折线图、柱状图、三维雷达图」ECharts 全部原生支持（实测本地副本含 `radar` / `graph` / `sankey` 组件）。

pandas 用在后端做**数据统计**（不是画图）：读 CSV、算均值/最大值/趋势斜率/异常点，这些统计量再喂给 LLM 写 caption。

### 「自动提炼趋势结论」怎么做才不幻觉

**关键：不要让 LLM 直接看原始 CSV 数字。**

```
CSV → pandas 算出统计事实（结构化）
    → 比如 {"列": "Accuracy", "从": 0.71, "到": 0.89, "趋势": "上升", "单调": true, "峰值行": 12}
    → 把「统计事实」而不是「原始数据」喂给 LLM
    → LLM 只负责把这些事实写成通顺的学术 caption
```

这么做的好处：
1. 数字全部来自 pandas 计算，LLM 无法编造
2. caption 里出现的每个数值都能回溯到具体计算
3. 输出后可以**校验**：正则抽出 caption 里的所有数字，逐个比对是否在统计事实里出现过。不在的标红

### 学术 Figure Caption 的结构

标准论文图注一般是这个模式：

> **Fig. 3.** *（一句说这是什么图，什么数据）*. 实验在 X 数据集上比较了 A/B/C 三种方法。可以看出方法 C 在训练步数超过 50k 后显著优于其余两者，最终准确率 89.2%，比次优的 A 高 3.1 个百分点。

即：**图名 → 数据来源 → 核心趋势 → 关键数字**。四段结构写死在 prompt 里，比让模型自由发挥稳定得多。

### 复用点

- **ECharts 本地副本**（1.6 里最大的复用项）
- **pandas**（本次刚装，正是为此）
- `app/tools/` 注册机制：`plot_csv` 可做成工具

### 工作量

**2–2.5 人天**。CSV 解析与统计 0.5 天，图表配置生成 1 天（四类图各一套），caption prompt 与校验 0.5 天，前端 0.5 天。

### 风险

- **CSV 格式千奇百怪**。编码（GBK/UTF-8）、分隔符（逗号/制表符/分号）、表头有无、缺失值。要写健壮解析 + 出错时给清晰提示，不要静默失败。
- **LLM 生成 ECharts option 容易多编字段**。建议不让 LLM 直接写完整 JSON，而是让 LLM 输出**受限的图形规格**（图类型 + 列映射 + 标题），后端代码再去拼 ECharts option。这样 ECharts 配置永远合法。

---

## 5. 子项 4：中译英

### 官方原文

> 自动将中文论文的摘要或者全文翻译成英文。

### 先看清楚一件事：现有翻译模块方向是反的

`app/translate.py` 现在是**英译中**（把英文 PDF 论文译成中文，给阅读页做中英对照）。它的 `SYSTEM_PROMPT` 第一句是「把英文论文片段译成简体中文」。

1.6.4 要的是**中译英**。方向相反，prompt 完全不能复用。

**能复用的是工程骨架**：分块（`CHUNK_CHARS = 6000`）、缓存（按 SHA1 摘要落盘）、术语一致性、错误处理。这些逻辑与语种无关，可以抽出来共用。

### 学术中译英 vs 普通中译英，差别在哪

| 维度 | 普通翻译 | 学术翻译 |
|---|---|---|
| 术语 | 意译即可 | 必须用领域固定译法（注意力机制 → attention mechanism，不能译成 attention system） |
| 句式 | 保持原文语序 | 要按英文学术习惯重组（中文爱用短句排比，英文要主从句结构） |
| 人称 | 随意 | 用被动语态或 "we"，避免 "I" |
| 时态 | 随意 | 方法用过去时/现在时，结论用现在时 |
| 连接词 | 照翻 | 要补英文惯用的 however / moreover / in contrast |
| 数字与单位 | 照翻 | 数字格式（中文「百分之三」→ "3%"） |

**所以这不是「翻译」任务，是「翻译 + 学术润色」两段任务。**

### 主流实现对照

| 项目 | 定位 | 优点 | 缺点 | 对本项目的价值 |
|---|---|---|---|---|
| [ACTAshui/bilingual-academic-writing-assistant](https://github.com/ACTAshui/bilingual-academic-writing-assistant) | 双语学术论文写作与翻译网页助手 | **形态和本项目最像**（网页 + 学术翻译） | 体量小，实现不复杂 | 交互设计可参照 |
| [Haojae/scipilot-writing-skill](https://github.com/Haojae/scipilot-writing-skill) | 学术写作润色助手，带「AI 痕迹检测 + 评审自检」 | **有机器校验的写作质量证据链**，这个思路很值钱 | Skill 形态，非库 | 自检循环的设计值得抄 |
| [Leey21/awesome-ai-research-writing](https://github.com/Leey21/awesome-ai-research-writing) | 学术写作 prompt 合集 | prompt 模板多，可直接拿 | 只是集合，不是实现 | **prompt 素材库** |
| [binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic) | 科研工作台，含翻译/润色/论文解读 | 功能全，中文社区成熟 | 太重，是独立应用 | 参考其 prompt 组织方式 |
| [Byaidu/PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate) | PDF 论文翻译保留排版 | 排版保留做得极好 | 主要做 PDF 级翻译，不是写作用 | 与 1.6.4 关系不大 |

### 推荐方案：两阶段 + 术语表约束

```
中文摘要/全文
   ↓ 阶段 1：抽取术语
   从中译英方向抽术语对（LLM 抽，或从已有译文中反向学习）
   ↓ 阶段 2：翻译 + 润色（一个 prompt 里完成）
   约束：术语表强制使用；数学公式/引用编号原样保留；按英文学术句式重组
   ↓ 阶段 3：自检（可选但有加分）
   检查：术语是否一致、有没有漏译、公式是否被改
```

**术语一致性**是学术翻译最容易被扣分的地方。同一篇里「卷积神经网络」一会儿 cnn 一会儿 convolutional neural network，评审一眼就能看出是机器翻的。做法是：先抽一份术语表，翻译时把它塞进 prompt 的 system 消息里，要求「以下术语必须这样译」。

### 复用点

- `app/translate.py` 的**分块、缓存、错误处理**骨架（`CHUNK_CHARS`、`_digest`、`save_translation`）
- `app/llm.py` 的 `chat()`
- 现有中英对照阅读页的 UI 模式（左右分栏）可以直接套到中译英的展示上

### 工作量

**1.5–2 人天**。prompt 与术语表 1 天（这是质量的决定因素），接口与前端 0.5 天，缓存复用 0.5 天。

### 风险

- **术语抽取质量决定翻译质量**。领域术语抽不准，整篇翻译就废了。
- **全文翻译耗 token**。一篇 8000 字中文摘要 → 英文约 4000 词。要有预算控制，别让用户一次把整本书丢进来。
- 不要承诺「达到母语水平」——如实说这是「AI 辅助初稿，需人工校对」，反而显得专业。

---

## 6. 分阶段实施建议

### 如果时间非常紧（2 天）

| 顺序 | 子项 | 工作量 | 理由 |
|---|---|---|---|
| 1 | **1.6.3 CSV 数据可视化** | 2 人天 | 演示效果最直观（上传 CSV → 出图 → 自动写 caption），零新增依赖，赛题要求的四类图 ECharts 全支持 |
| 2 | **1.6.4 中译英** | 1.5 人天 | 纯 LLM 任务，无外部依赖，最不容易翻车；能直接复用现有翻译骨架 |

这两个做完，1.6 已经有 60% 的完成度，且都**断网可演示**。

### 如果时间充裕（5–6 天）

| 顺序 | 子项 | 工作量 | 说明 |
|---|---|---|---|
| 1 | 1.6.3 CSV 可视化 | 2 天 | 同上 |
| 2 | 1.6.4 中译英 | 1.5 天 | 同上 |
| 3 | 1.6.1 论文框架生成 | 2–3 天 | 引用校验链是主要工作量，也是评分亮点 |
| 4 | 1.6.2 拓扑图生成 | 1.5–2 天 | 环境已就绪，主要是 prompt 与渲染管线 |

合计约 **7–8.5 人天**。

### 可以砍的部分

- **1.6.2 的 TikZ** 可以先不做，只给 Mermaid + Graphviz。赛题是「TikZ / Graphviz / Mermaid / Matplotlib」四选一，给两个也满足。
- **1.6.1 的全文生成**可以只做 Abstract + Related Work，Introduction 砍掉（Introduction 最难写好，投入产出比低）。
- **1.6.3 的三维雷达图**如果数据结构不支持，可以先做二维雷达 + 折线 + 柱状，赛题写的是「等」，不是必须四类全给。

---

## 7. 与现有能力的复用清单

| 现有资产 | 位置 | 1.6 里怎么用 |
|---|---|---|
| ECharts 本地副本 | `web/vendor/echarts/echarts.min.js` | 1.6.3 全部图表 |
| pandas | 本次新装 3.1.0 | 1.6.3 数据统计 |
| 工具注册机制 | `app/tools/base.py` + `registry.py` | 新工具继承 `BaseTool`，注册表加一行 |
| LLM 调用 | `app/llm.py` 的 `chat()` | 全部四子项 |
| 分块与缓存骨架 | `app/translate.py` | 1.6.4 翻译 |
| 论文元数据 | `data/catalog.json` | 1.6.1 引用候选集 |
| 引用图谱 | `data/meta/citation-graph.json` | 1.6.1 反查「这篇引过谁」 |
| LaTeX / Graphviz / Mermaid | 本机已装 | 1.6.2 三种渲染 |

---

## 8. 所有引用链接核验状态

以下 URL 均用 `curl -L` 实测返回 **HTTP 200**（2026-10-29）：

| 项目 | 状态 |
|---|---|
| [mmlong818/open-academic-paper-gen](https://github.com/mmlong818/open-academic-paper-gen) | 200 |
| [vedangvatsa/research-paper-framework](https://github.com/vedangvatsa/research-paper-framework) | 200 |
| [Future-House/paper-qa](https://github.com/Future-House/paper-qa) | 200 |
| [wanshuiyin/Auto-claude-code-research-in-sleep](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep) | 200 |
| [opentikz/opentikz](https://github.com/opentikz/opentikz) | 200 |
| [potamides/detikzify](https://github.com/potamides/detikzify) | 200 |
| [0xE1337/thesis-figure-skill](https://github.com/0xE1337/thesis-figure-skill) | 200 |
| [mermaid-js/mermaid-cli](https://github.com/mermaid-js/mermaid-cli) | 200 |
| [microsoft/lida](https://github.com/microsoft/lida) | 200 |
| [mljar/plotai](https://github.com/mljar/plotai) | 200 |
| [ACTAshui/bilingual-academic-writing-assistant](https://github.com/ACTAshui/bilingual-academic-writing-assistant) | 200 |
| [Haojae/scipilot-writing-skill](https://github.com/Haojae/scipilot-writing-skill) | 200 |
| [Leey21/awesome-ai-research-writing](https://github.com/Leey21/awesome-ai-research-writing) | 200 |
| [binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic) | 200 |
| [Byaidu/PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate) | 200 |
| [hazugi2004/paperlocale](https://github.com/hazugi2004/paperlocale) | 200 |

---

## 9. 下一步

本文档只是调研，**没有写任何 1.6 业务代码**。等你拍板做哪几个子项、按什么顺序，再开工。

建议的下一步动作：
1. 从第 6 节选一个实施顺序
2. 确认「References 是否只用本库 39 篇 + arXiv 检索」这个引用范围
3. 确认 1.6.2 要不要四个渲染器全做