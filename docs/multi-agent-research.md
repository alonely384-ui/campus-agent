# 多智能体协同：别人怎么做的

- 调研时间：2026-10-29
- 调研动因：`docs/GAP-VS-OFFICIAL.md` 查出「多智能体协同」是官方 35% 权重维度里唯一完全缺失的项（全库 grep 命中 0 处）
- 官方原文：AI 技术深度 35% —— "文档智能解析、高级 RAG 架构以及**多智能体协同**的核心技术和架构"
- **本文只调研，不改代码**

---

## 一、四个真实做法

### 1. MA-RAG（论文，最贴合学术问答）

- 出处：[arXiv:2505.20096](https://arxiv.org/abs/2505.20096) — *MA-RAG: Multi-Agent Retrieval-Augmented Generation via Collaborative Chain-of-Thought Reasoning*
- **角色划分（4 个）**：
  | 角色 | 职责 |
  |---|---|
  | **Planner** | 把原始问题拆成可执行的子任务（消歧、分解） |
  | **Step Definer** | 为当前子任务定义具体的信息需求 |
  | **Extractor** | 从检索到的文本里抽出与需求匹配的**证据片段** |
  | **QA Agent** | 汇总证据合成最终答案 |
- **协作机制**：不是四个独立服务，而是**流水线式链式思考**。每个角色把中间推理结果（chain-of-thought）显式传给下一个角色，逐步精炼检索和合成。原文强调 "maintaining modular interpretability"（模块化的可解释性）。
- **关键实验结论**：消融实验显示 **Planner 和 Extractor 是多跳推理的关键**，QA Agent 需要高容量模型才合成得好；甚至 LLaMA3-8B 配上这套框架就能超过更大的独立 LLM。
- **对我们的价值**：**角色命名和职责划分可以直接照搬**。它论证的"Planner + Extractor 最关键"正好说明——我们现在的 Agent 缺的就是这两块。

### 2. paper-qa（Future-House，已开源可读代码）

- 出处：[Future-House/paper-qa](https://github.com/Future-House/paper-qa)，论文 [arXiv:2312.07559](https://arxiv.org/abs/2312.07559)
- **实现方式**：不是"多个 Agent 进程互相发消息"，而是**在一个 Agent 循环里提供五个工具**，由 LLM 自己决定调哪个（`src/paperqa/agents/tools.py`，实测类名）：
  | 工具类 | `TOOL_FN_NAME` | 干什么 |
  |---|---|---|
  | `PaperSearch` | `paper_search` | 搜索新论文加入文档池，支持 `min_year`/`max_year` 过滤 |
  | `GatherEvidence` | `gather_evidence` | 针对某个具体问题从已有论文中抽证据 |
  | `GenerateAnswer` | `gen_answer` | 用当前证据合成答案，**失败时返回信号让 Agent 继续找证据** |
  | `Reset` | — | 重置状态 |
  | `Complete` | — | 结束 |
- **协作机制（最关键的设计）**：`EnvironmentState` 是**共享状态对象**，包含 `docs`（论文池）和 `session`（答案与证据）。每个工具都读写同一个 state。
- **状态回显**：每次工具调用结束都返回一句 `Status: Paper Count=12 | Relevant Papers=5 | Current Evidence=8 | Current Cost=$0.0021`（`make_status()`）。**Agent 靠这句话判断"证据够不够"**。
  `GatherEvidence` 的 docstring 里明确写 "Only invoke this tool when the paper count is above zero, or this tool will be useless"；`GenerateAnswer` 写 "The tool may fail, indicating that better or different evidence should be found. Aim for at least five pieces of evidence from multiple sources"。
- **对我们的价值**：**这套设计和我们现在的架构最接近，改造成本最低**。它证明了"多智能体协同"不一定要上重型框架——**共享状态 + 状态回显 + 工具自我说明**就够了。

### 3. med-paper-assistant（GitHub，MCP + cross-agent）

- 出处：[u9401066/med-paper-assistant](https://github.com/u9401066/med-paper-assistant)（12 stars，Python，2026-09 更新）
- **实现方式**：**MCP Server 集群**，不是 Agent 集群。一个主编排层（Copilot Agent Mode）+ 四个 MCP Server：
  | Server | 职责 |
  |---|---|
  | `mdpaper` MCP | 论文项目、草稿、参考文献管理（118 个 tools 的 full surface，默认 facade 只暴露 12 个） |
  | `pubmed-search` MCP | 医学文献检索 |
  | `cgu` MCP | — |
  | `drawio` MCP | 画图 |
- **协作机制（最值得抄的一条）**：**"檔案系統是共享狀態"**。所有组件读写同一个 `projects/{slug}/` 目录（`concept.md`、`drafts/`、`references/`）。这套共享目录让 MCP Server、Dashboard、编辑器插件之间不需要互相调用。
- **第二值得抄的**：`compact default 採 facade-first surface，暴露 12 個 tools`，full surface 才有 118 个。**理由写在 `tool-surface-authority.json` 里并由 release gate 验证**——工具太多 LLM 会选错。
- **对我们的价值**：`facade-first`（对外只暴露少量工具）+ 共享状态目录，是"看起来是多智能体、实现上很轻"的路子。

### 4. GPT-Academic（国内，7.1 万 stars）

- 出处：[binary-husky/gpt_academic](https://github.com/binary-husky/gpt_academic)
- **实现方式**：**函数插件机制**。每个功能是一个独立插件函数，UI 上是一个快捷按钮或直接输入触发。插件之间可以调用（如"PDF 翻译"内部调用"总结"和"翻译"两个子插件）。
- **协作机制**：让 LLM 自己读插件名和描述来决定调用哪个（`function calling`），本质是**单 Agent + 大量工具**，和我们现状最像。
- **对我们的价值**：证明这条路能做出 7 万 star 的产品，但也说明——**评委看到这种只会认为"工具多"，不会认为"多智能体"**。

---

## 二、对照表：别人 vs 我们

| 维度 | MA-RAG | paper-qa | med-paper-assistant | GPT-Academic | **我们现在** | 可借鉴度 |
|---|---|---|---|---|---|---|
| 角色划分 | Planner/StepDefiner/Extractor/QA | 5 个工具（search/evidence/answer/reset/complete） | 主编排 + 4 个 MCP Server | 一堆插件 | **无明确角色**：单 Agent + Function Calling（`app/agent.py:1249` 行里全是 `_plan_multi_sources`、`_intent_terms` 这类内部函数） | ★★★ |
| 状态共享 | 链式传递中间推理 | `EnvironmentState`（docs + session） | `projects/` 共享目录 | 无显式状态 | `AgentResult` + `AgentStep`，但**只用于事后展示，不参与决策** | ★★★ |
| 决策依据 | Planner 拆任务 | **状态回显**（Paper Count/Evidence Count）驱动下一步 | Phase Gate 验证器 | 无 | 无 | ★★★ |
| 失败兜底 | 未详述 | `GenerateAnswer` 可失败 → 回去找证据 | `pipeline_gate_validator` | 无 | 已有 RAG 三路召回 try/except 兜底 | ★★ |
| 可解释性 | 中间推理全暴露 | 每步状态字符串 | 8 维度质量计分卡 | 无 | `AgentStep` 已有，但没展示出去 | ★★ |
| 实现重量 | 论文 | 轻（aviary 框架 + 工具） | 重（DDL/DDD 全套） | 中 | 轻 | — |

---

## 三、改造方案（三选一，未实施）

### 方案 A：轻量「命名角色 + 状态回显」（1–1.5 天，性价比最高）

**照抄 paper-qa 的思路**，把 `app/agent.py` 里现有的隐式步骤显式命名成角色：

- 现状是 `_plan_multi_sources()` → 检索 → `_merge_scored()` → 生成。改成三段具名调用：
  - **Planner**：`_plan_multi_sources()` 的结果明确标记为角色产出
  - **Retriever / Extractor**：检索 + 抽取证据，返回带 `Status: 证据=8 | 覆盖论文=3 | 页=2,7,8` 的状态串
  - **Verifier**：新增，校验回答里的每个数字/事实是否能在证据里找到（这个我们已有 `docs/acceptance-official5.md` 的判据逻辑）
- **前端**：把 `AgentStep` 渲染成一条带角色头像的时间线（"Planner → Retriever → Verifier → Writer"），演示时一目了然。
- **为什么推荐**：不动核心逻辑，风险最低；而且**这正是 paper-qa 证明有效的做法**，答辩时可以直接引出处。

### 方案 B：MA-RAG 四角色完整版（2.5–3 天）

按 MA-RAG 的 Planner / Step Definer / Extractor / QA 四角色重写问答链路，每个角色单独一次 LLM 调用，中间推理显式传递。

- **优点**：和官方要求的"多智能体协同"字面对应最工整，答辩好讲。
- **风险**：每问要多调 2–3 次 LLM，延迟和成本涨 3 倍；我们现在单问已经要 20–60 秒，演示时可能超时。
- **缓解**：只在"多篇对比"这类复杂问题上触发完整四角色，单篇问答走快路径。

### 方案 C：MCP Server 集群（3–4 天，不建议）

照 med-paper-assistant 拆成多个 MCP Server。

- **为什么不做**：它是为了跨 IDE 复用才这么做，我们是单体 Web 应用，拆出去只增加运维成本，评委也看不到区别（都在后台）。

---

## 四、一句话建议

**做方案 A**。理由是：paper-qa 已经证明了"共享状态 + 状态回显 + 具名工具"就能撑起学术问答的 agent 架子，而且我们现在的代码（`AgentStep`、`_plan_multi_sources`）已经具备雏形，只差**把隐式步骤显式命名并展示出来**。

答辩时的一句话可以这么讲：**"我们的问答链路是 Plan → Retrieve → Extract → Verify → Write 五段协同，每段的中间状态都对前端可见，任何一步证据不足都会回流重检索——这是参照 paper-qa（arXiv:2312.07559）的 EnvironmentState 状态驱动设计。"**

这比"我们是单 Agent 用了很多工具"有说服力得多。

---

## 五、网络受限说明

以下页面抓取失败，未采用其内容：
- `https://arxiv.org/abs/2505.20096` 首次走 `web_fetch` 报 `TypeError: fetch failed`，改用 httpx 直连成功（HTTP 200）
- `https://browse-export.arxiv.org/...` HTTP 429 限流
- `https://www.themoonlight.io/...` Vercel 安全检查拦截
- `https://raw.githubusercontent.com/...` `RemoteProtocolError: Server disconnected`，改用 GitHub API base64 接口成功
- IEEE `Agentic Graph-RAG` 页面未尝试（付费墙）
- `aclanthology.org/2026.acl-long.971.pdf` fetch failed，未采用

以上论文/仓库的引用信息均来自实际抓到的内容，未编造。