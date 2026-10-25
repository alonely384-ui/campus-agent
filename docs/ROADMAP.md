# 路线图（ROADMAP）

> 两条线：
> **A. 功能路线**——按评分权重重排，只做加分高的，其余进 backlog。
> **B. 全量陪跑路线**——把项目里**全部代码**（约 4800 行）按 6 场、每场 90 分钟走一遍，边走边跑。

---

## 一、原则

1. **按评分权重排活**：技术深度 35（Agent 9 / 抗幻觉 RAG 8 / 多模态解析 7 / Function Calling 6 / Guardrails 5）、创新 25、工程 10、实用 10、UX 10、学术价值 10。
2. **一次只加一个模式**，加完立刻跑：`pytest` + `38 题回归` + `官方 5 问`。
3. **每个模式配 2 道真实题目**，没有例子的功能不上线（答辩会被问穿）。
4. **不承诺做不完的**：对导师/评委说"已做 3 个、2 个在 backlog"，不说"支持十种模式"。

## 二、现状（2026-10-25）

- 已交付：单篇问答（带 `[来源: 文件 第N页]`）、拒答、去重（0.873）、表格计数、多篇对比表、证据卡片、交叉编码器重排。
- 数字：`81 passed`；38 题（3 篇）开重排 R@1 30/38、R@5 36/38；官方 5 问 5/5；索引 25995 块 / 39 篇。
- 缺：视频、技术文档 PDF、稳定公网地址（现为临时隧道）。

---

## 三、功能路线（A 线）

| 优先级 | 功能 | 对应评分 | 工时 | 验收题目（必须答对） |
|---|---|---|---|---|
| **P0** | 模式壳：问答 / 对比 / 评价 / 综述（前端切换 + 后端参数） | 交互创新 7 | 3h | 4 个模式各问 1 题，答案结构不同 |
| **P0** | 专业化：学术语气、术语保留英文、引用格式统一 | Guardrails 5 | 1h | 同题两版对比，专业版无口语化表达 |
| **P1** | DOCX 读取（`.docx` 走 python-docx） | 多模态 7 | 2h | 上传一份 docx，能答其中一节 |
| **P1** | 选段收藏：选中原文→存到 `data/notes`，可列出/删除 | 实用 10 | 3h | 收藏 3 段，重启后仍在 |
| **P2** | 网页/URL 摄入（粘贴链接→抽正文→切块入库） | 多模态 7 | 2h | 粘贴一篇 arXiv abs 页，能答标题与摘要 |
| **P2** | 对比可视化：对比表旁加一组柱状图（参数/层数/上下文长度） | 交互创新 7 + UX 3 | 4h | 两篇对比出现图 + 表 |
| **P2** | arXiv 搜索→自动入库（标题/作者/摘要/PDF 链接） | 自动检索追踪（模块四） | 4h | 搜 "Chain-of-Thought"，入 1 篇并标注来源 |
| **P3** | 论文评价评分卡（创新/严谨/可复现/写作） | 场景创新 10 侧 | 3h | 给 1 篇打 4 维分 + 依据引用 |
| **P3** | 数据分析：结果表→统计结论（均值/对比/趋势） | 实用 5 | 3h | 问"Table 2 里哪个模型 BLEU 最高"能答对 |
| **P3** | 指导建议：研究空白/Future Work 提取 | 学术价值 5 | 3h | 从 1 篇提 3 条可做的方向，各带出处 |
| **P3** | 图片 OCR（扫描件） | 多模态 7 | 4h | 上传扫描页，答出其中一句话 |

**时间表**：P0 在 10/26–10/28 完成（预指导前）；P1 在 11/3 前；P2 在 11/10 前；P3 只在 11/14 之后有余量才动，否则全部进 backlog。

**明确不做**（写进 backlog，答辩时主动说是取舍）：多用户/权限、Docker 化、微调、图表生成器、手机原生 App、自动爬全站。

---

## 四、全量陪跑路线（B 线）

目标：**每一行代码你都看着它跑一遍**，不是读，是"改一个值→看输出变化"。
方式：我讲一段、你复述一句、跑一条命令看结果；卡住就停下来换比喻。
节奏：6 场 × 90 分钟；每场结束你能用 3 句话讲清"这层解决什么问题"。

### 场 1 · 项目入口（约 90 分钟）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `README.md`、`pyproject.toml`、`requirements.txt`、`.env.example`、`LICENSE` | — | `pip install -r requirements.txt`（已装） | 项目解决什么问题、依赖里哪个是重排、哪个是向量库 |
| `app/config.py` | 36 | `python -c "from app.config import settings; print(settings.app_port)"` | 配置从哪来、怎么改端口 |
| `app/logging_config.py` | 32 | 起服务看日志格式 | 日志打到哪里、`logger.exception` 与 `HTTPException` 的区别 |
| `app/llm.py` | 84 | 看 `get_client()` 懒加载 | 为什么懒加载、超时/重试在哪 |
| `app/main.py` | 263 | `curl /health`、`curl /papers/files`、空 message 看 422 | 6 道门、路由清单 |

### 场 2 · Agent 与工具（约 90 分钟）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `app/agent.py` 1–120（常量、SYSTEM_PROMPT、Evidence） | 120 | 读 prompt | 五条规则各治什么病 |
| `agent.py` 120–300（`_parse_arguments`/`_execute_tool`） | 180 | 问"23×47 等于多少"看工具被调用 | 模型怎么"决定"调工具 |
| `agent.py` 300–520（`_rag_context`/元数据/表格计数/页级补页） | 220 | 问 GPU 题、第六作者、表格计数 | 为什么检索为空还要补页文本 |
| `agent.py` 520–742（`run_agent`/`run_agent_stream`） | 222 | `curl /chat/stream` 看事件流 | 流式与一次性返回的区别 |
| `app/tools/base.py`、`calculator.py`、`registry.py` | 45+75+35 | 改 calculator 一个算子再跑测试 | 加一个新工具要改几处 |

### 场 3 · 论文库（约 90 分钟）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `app/papers.py` 1–180（路径、索引、去重评分） | 180 | `GET /papers` | 索引文件在哪、dup 怎么算 |
| `app/papers.py` 180–339（保存/删除/结构） | 159 | 上传 v1 副本看 `indexed` 与 `duplicate_of` | 为什么重复不拦截只标记 |
| `app/structure.py` | 178 | `python scripts/build_structure.py`（看输出） | 标题/作者/章节怎么抽 |
| `DELETE /papers/{id}` | — | 删一次看 `removed_chunks` | 删除要清哪三处 |

### 场 4 · 检索与重排（约 120 分钟，最重要）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `pipeline.py` 1–300（切块：递归分隔符、表格块、重叠） | 300 | `python scripts/debug_retrieve.py` | 300/30 是怎么定的、表头为什么要重复 |
| `pipeline.py` 300–560（分词、embed、IDF） | 260 | 打印 `_tokens("注意力机制")` | 为什么英文用整词、为什么要 IDF |
| `pipeline.py` 560–760（BM25、RRF 融合、召回深度） | 200 | `python scripts/grid_rrf.py --cases data/eval/qa_multi.json` | RRF 为什么只比名次 |
| `pipeline.py` 760–994（重排接入、检索入口） | 234 | `python scripts/eval_rerank_ab.py` | 重排把 R@1 从 25 提到 30 的原因 |
| `app/rag/rerank.py` | 129 | 关掉重排再跑一次（`RAG_RERANK=0`） | 双塔 vs 交叉编码器 |

### 场 5 · 前端与运维脚本（约 90 分钟）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `web/index.html` | 332 | 浏览器勾选 2 篇→对比表 | 勾选怎么变成 `sources` |
| `scripts/build_index.py`、`build_structure.py` | 55+43 | 只跑 `--help` 或小样本 | 重建索引要多久、会不会清库 |
| `scripts/eval_min.py`、`grid_rrf.py`、`eval_rerank_ab.py`、`debug_retrieve.py` | 128+152+74+66 | 各跑一次 | 评测指标怎么算 |
| `scripts/check_all.py`、`serve_public.ps1`、`keep_alive.ps1` | 92+85+66 | `python scripts/check_all.py` | 8 项体检各查什么 |

### 场 6 · 测试与总验收（约 90 分钟）

| 看什么 | 行数 | 跑什么 | 要能回答 |
|---|---|---|---|
| `tests/` 12 个文件 | 1170 | `pytest -q -v` 挑 3 个看 | 测试怎么替换外部依赖（monkeypatch） |
| 官方 5 问 | — | `python scripts/verify_official5.py` | 5 题各考哪一块 |
| 38 题回归 | — | `python scripts/eval_rerank_ab.py` | 30/38 是哪 8 题没中 |
| 复述 10 题 | — | 口述 | 一句话讲清每层 |

**陪跑完成的标准**：6 场结束后，你能不看文档用 10 分钟把"上传→切块→三路检索→融合→重排→拼 prompt→带引用回答"讲一遍，并且能指出每一步对应哪个文件哪一段。

---

## 五、提交与日志规则

- 每天至少 1 次提交，提交信息写清"改了什么"，不用 `git add .`。
- `docs/PROGRESS.md` 写工程改动（做了什么/证据/坑/下一步）；`docs/LEARNING-LOG.md` 写学习（学了什么/能讲清/没搞懂）。
- 提交时间如实记录；**不伪造历史**（赛规要求"体现真实开发过程"）。

## 六、Backlog（明确不做）

多用户与权限、Docker 化部署、模型微调、图表自动生成、手机 App、全站爬虫、多语言界面、SSO。
