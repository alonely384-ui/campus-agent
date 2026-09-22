# campus-agent

> 校园智能助手 Agent：RAG 文档问答 + 手写工具调用 + 流式输出

<!-- ⚠️ 下面两行是你要填的。项目没上线之前先留着占位，别删。 -->
**🔗 在线体验**：（待第 3 周部署后填入）
**🎬 演示视频**：（待上传后填入）

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

一个能"查资料 + 用工具"的校园助手：

- 上传课件/培养方案，用自然语言提问，**基于原文回答并给出引用来源**
- 涉及计算时，自己调用计算器工具，不靠模型心算
- 回答以流式（打字机效果）输出，工具调用过程对用户可见

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
# （如果用 uv：uv sync）

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

## 架构

```
用户浏览器 (web/index.html)
   │  POST /chat/stream  (SSE)
   ▼
 FastAPI (app/main.py)
   │
   ▼
 Agent 主循环 (app/agent.py)  ← 本项目最核心的文件
   │
   ├─► LLM (app/llm.py) ──► DeepSeek / 任意 OpenAI 兼容接口
   │
   └─► 工具层 (app/tools/)
          ├── calculator  计算器（已实现，带 AST 白名单防注入）
          ├── schedule    查课表（待实现）
          └── note        生成笔记（待实现）
```

## 关键指标

> ⚠️ 下表的数字目前都是**占位符**。等你测出真实数据后逐个替换。
> 数字难看也要写真实的 —— 编的数字在面试追问下撑不过两轮。

| 指标 | 数值 | 测量方式 |
|---|---|---|
| 首 token 延迟 P95 | 待测 | `python scripts/bench_latency.py` |
| 检索命中率 | 待测 | `python eval/run_eval.py`（100 题评测集） |
| 单次对话平均成本 | 待测 | 调用记录汇总 |
| 测试用例数 | 见 `pytest` 输出 | `pytest --collect-only -q` |
| 并发 QPS | 待测 | 部署后压测 |

## 技术决策记录

> 面试官问"为什么这么做"时，答案都在这里。**每一条都要能当面讲出来。**

| 决策 | 选择 | 理由 | 代价 / 何时会改 |
|---|---|---|---|
| 向量库 | Chroma | 数据量 <10 万条、单机部署，接入成本最低 | 已预留抽象层，触到瓶颈切 Qdrant 只改配置 |
| Agent 实现 | 先手写，不依赖框架 | 先搞懂框架解决了什么问题，避免只会调包 | 后续若上 LangChain 是为了维护性，不是不会写 |
| 流式方案 | SSE | 单向流式足够；比 WebSocket 轻，走 HTTP/2，断线自动重连 | 需要双向实时交互时才换 WebSocket |
| 计算工具安全 | AST 白名单解析 | 直接 `eval()` 会被注入任意代码执行 | 只支持四则/乘方/取余，够用 |
| 客户端创建时机 | 懒加载 | 在 import 时创建会拿到尚未加载的空 API Key | 无 |
| 前端 | 原生 HTML/JS | 现阶段目标是"能公网访问"，不是"前端好看" | 核心功能稳定后再考虑框架 |

## 项目进度

- [x] **第 1 周目标** 工程化骨架：分层目录、配置中心、日志、测试、CI
- [x] **第 4 周目标** 手写 Agent 循环（工具调用 + 三种失败处理）
- [x] 计算器工具（含安全防护）
- [x] FastAPI 接口：`/health` `/chat` `/chat/stream` `/upload`
- [x] 极简前端：SSE 流式渲染 + 工具调用可见
- [ ] **第 2 周目标** RAG：PDF 解析 → 切分 → Chroma → 检索问答带引用
- [ ] **第 2 周目标** 100 题评测集 + 命中率统计
- [ ] **第 3 周目标** Docker + 云服务器 + Nginx + HTTPS 公网部署
- [ ] 第 4 周：再补 2 个工具（课表、笔记）

## 已知问题

- 长文档跨章节提问时召回效果差（切分策略待优化）
- 尚未接入 RAG，`/chat` 目前只能回答通用问题
- 对话历史暂存在前端，服务端未持久化（第 4 周接 Redis）

## 目录结构

```
campus-agent/
├── app/
│   ├── main.py            # FastAPI 入口 + 接口定义
│   ├── agent.py           # ⭐ 手写工具调用循环（核心）
│   ├── llm.py             # LLM 客户端封装
│   ├── cli.py             # 命令行入口
│   ├── config.py          # 配置中心（读 .env）
│   ├── logging_config.py  # 日志
│   ├── rag/               # 检索增强（待第 2 周实现）
│   └── tools/             # 工具
│       ├── base.py        # 工具基类
│       ├── calculator.py  # 计算器
│       └── registry.py    # 工具注册表
├── tests/                 # 测试（不需要联网/API Key）
├── eval/                  # 评测集与评测脚本
├── scripts/               # 辅助脚本
├── web/index.html         # 前端页面
├── data/uploads/          # 上传的文档
├── .env.example
├── requirements.txt
└── pyproject.toml
```

## License

MIT
