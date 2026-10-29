# 交接文档 · 2026-10-29 晚 · 赛题 1.6 写作助手完成

> 本文档写给下一个拿着仓库的会话。读完这篇你就能接续干活，不用翻聊天记录。
> 前情见 `HANDOFF-1.5.md`（引用图谱）和 `writing-assistant-research.md`（1.6 调研）。

## 1. 当前状态一句话

赛题 **1.1 ~ 1.6 六个模块全部实现并跑通**，`pytest tests -q` → **319 passed**，`ruff check .` → All checks passed。

| 模块 | 状态 | 主文件 | 页面 |
| --- | --- | --- | --- |
| 1.1 PDF 管理 | 完成 | `app/papers.py` `app/structure.py` `app/ocr.py` | index.html 论文库 |
| 1.2 知识问答 | 完成 | `app/agent.py` `app/rag/` | index.html 对话 |
| 1.3 阅读与翻译 | 完成 | `app/layout.py` `app/translate.py` | reader.html |
| 1.4 下载与推荐 | 完成 | `app/online.py` `app/recommend.py` | index.html 发现 |
| 1.5 论文分析 | 完成 | `app/graph.py` | graph.html / analysis.html |
| 1.6 写作助手 | **本轮完成** | `app/writing.py` `app/diagram.py` `app/chart.py` `app/zh2en.py` | writing.html |

## 2. 本轮新增的四个模块

### 2.1 `app/writing.py` — 框架生成（1.6.1）

**核心机制叫「候选集约束生成」**，是这一块最值得在答辩里讲的设计：

1. 用研究 idea 去检索真实文献，得到 12~20 篇候选池，每篇编号 `C1`…`Cn`
2. 候选池（标题/作者/年份/摘要）+ 编号规则塞进 prompt，明确要求**只能引用池内编号**
3. 模型只写 `[C3]` 这样的标记，**碰不到参考文献条目**
4. 参考文献列表由 `build_reference_list()` 从候选池数据生成
5. 事后 `check_citations()` 校验有没有池外编号

这样「出现不存在的论文」在结构上不可能发生，不依赖模型自觉。

关键函数：`generate_framework(idea, total_candidates=16)`、`check_citations(text, pool)`、`format_reference(c)`。

实测：idea「利用稀疏注意力降低长文档问答中 Transformer 的计算开销」，池 12 篇，正文引用 12 篇，**悬空引用 0 条**。

### 2.2 `app/diagram.py` — 架构图（1.6.2）

两个渲染器：Mermaid（流程图）+ Graphviz（论文风格方框图）。TikZ 需编译兜底，暂未开放。

踩过的坑（都已修，别改回去）：
- **不能用 `shutil.which("mmdc")`**：Windows 的 PATHEXT 里 `.CMD` 在 `.EXE` 前，且 `%APPDATA%\npm\mmdc` 有个无扩展名的 Git Bash 脚本会先命中，报 `OSError: [WinError 193] %1 不是有效的 Win32 应用程序`。现按 `.cmd → .exe → .bat → 无后缀` 顺序手动找。
- **绕开 `mmdc.cmd` 直接调 `node cli.js`**：cmd 里硬编码用 `node` 名字调 node.exe，PATH 没 node 就报 `'"node"' is not recognized`。
- **mermaid-cli 12 已移除 `-w` 参数**，用 `-s 2`（scale）。
- **Graphviz 默认字体不含 CJK**，中文渲染成一排方框。`_ensure_font()` 在 `digraph` 后注入 `fontname="Microsoft YaHei"`；模型自己指定了 fontname 就不覆盖。

渲染失败会**只回喂错误让模型改语法、不改结构**，重试一次，不无限重试。

### 2.3 `app/chart.py` — 数据可视化（1.6.3）

双路设计：**ECharts 负责网页交互预览，matplotlib 负责投稿 PNG 导出**。

自动选型 `choose_chart()` 的分支（顺序即优先级）：
- 有严格单调递增且唯一值占比 >0.8 的列 → 当自变量，画 `line`/`scatter`
- 多数值列无自变量 → `radar`（每维归一化到 0-1）
- 两数值列 → `scatter`
- 单数值 + 类别：行数 >12 → `line`，否则 `bar`
- 全文本 → `pie`

**本轮修的三个真 bug**（都有测试锁住）：
1. 见数值列多就选雷达图，`params,accuracy,loss` 这种被选成雷达图 → 改为先找自变量列
2. `y=axis_col` 把因变量画成了自己 → 改为 `ys = [c for c in nums if c != axis_col]`
3. **对数轴只在 `export_png` 里生效，`build_echarts` 完全没有** → 导致网页上 5 个点挤在原点、下载的 PNG 却舒展。现两边统一走 `_needs_log()`。

`_needs_log` 阈值 100 倍：参数 0.34B~540B 是 1600 倍必须对数；准确率 86.7~93.1 不跨数量级，用对数轴会误导。

**`_padded_range()`**：ECharts 数值轴默认从 0 起，matplotlib 是自适应。加了 5% 余量的贴数据范围，两张图并排看着才一致。

### 2.4 `app/zh2en.py` — 中译英（1.6.4）

**和 `app/translate.py` 方向相反**：后者是英译中（阅读用），这里是中译英（目标「能直接塞进 IEEE/ACM 模板」），只复用分块/缓存骨架，提示词完全另写。

7 条硬规则：只输出译文 / 正式学术语域禁缩写 / 术语一致 / 长句拆短 / 数字单位和引用标记原样保留 / 时态规矩 / 不增不减。

支持术语表（`中文=English` 一行一条），逐块翻译时把上一块末 400 字作 context 保证术语连贯。

### 2.5 前端 `web/writing.html`

四个 Tab 对应四个子项。入口在 `web/index.html` 头部的「写作助手」链接。

ECharts **已本地化**到 `web/vendor/echarts/echarts.min.js`（1,030,855 字节），不走 CDN——演示环境可能断网。

## 3. Caption 数字校验（值得注意的设计）

生成 Figure Caption 时**只给模型统计摘要、不给图**，避免它描述出图上不存在的趋势。

但实测发现模型会把 `min=0.34` / `max=540` 粘成 `"0.348 to 5408"`。两处加固：

1. `_key_numbers()` 把关键数字提前格式化成可整段复制的字符串（`params.min = 0.34`），提示词里明令「一个字符都不许改、不许拼接」
2. `unsupported_numbers()` 事后对比 caption 里的数字与统计摘要，对不上的标出来给用户看（不拦截，只提醒）

`_canon_num()` 有个陷阱：直接 `rstrip("0")` 会把整数 `540` 削成 `54`，反而让正确的数字被判成可疑。只在有小数点时才裁尾零。

修完后实测 caption：`Accuracy ranges from 86.7 to 93.1 across the 0.34 to 540.0 params_B span, with a moderate positive correlation (r = 0.701).` —— 数字全对，可疑列表为空。

## 4. 接口清单（1.6 新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/writing/renderers` | 本机可用的渲染器 `{"mermaid":true,"graphviz":true}` |
| POST | `/writing/framework` | 框架生成，body `{idea, candidates}` |
| POST | `/writing/diagram` | 架构图，body `{description, kind}` |
| GET | `/writing/paper-figures?paper=&keyword=` | 从库内 PDF 找已有架构图 |
| POST | `/writing/chart?kind=&note=` | CSV 上传，multipart |
| GET | `/writing/chart/{name}` | 导出的图表 PNG |
| GET | `/writing/diagram/{name}` | 渲染的架构图 PNG |
| POST | `/writing/zh2en` | 中译英，body `{text, glossary}` |

## 5. 数据产物与 .gitignore

以下目录**都是生成物，随时可重建，不入仓库**：

```
data/meta/charts/       1.6.3 导出的图表 PNG
data/meta/diagrams/     1.6.2 渲染的架构图 PNG
data/meta/zh2en/        1.6.4 中译英分块缓存
data/meta/_preview/     前端调试中间产物
data/meta/figures/      1.3 抽出的论文插图
data/meta/translations/ 1.3.3 整篇翻译缓存
```

**踩过的坑**：`.gitignore` 不支持行尾注释。写成 `data/meta/charts/        # 说明` 整行会被当成字面路径，规则静默失效。`#` 必须独占一行。

## 6. 测试

`tests/test_writing.py`，50 条。**只测确定性逻辑，不测 LLM 输出质量和渲染质量**——那两件事每次跑都不一样，写进测试只会变成不稳定用例。

覆盖：引用校验（单标记/组标记/池外/重复/未用）、参考文献生成、prompt 构造、检索词抽取、字体注入、围栏剥离、CSV 编码（UTF-8/BOM/GBK）、截断、相关性、选图六分支、对数轴、贴数据范围、雷达归一化、分块不劈句子、术语表解析、数字校验。

## 7. 下一步可以做的（按性价比排序）

1. **演示视频**（官方要求 5-8 分钟 MP4 ≥1080p 普通话）——`docs/DEMO-SCRIPT.md` 目前是 3 分钟版讲稿，要扩写
2. **技术文档 PDF ≤30 页**——还没写
3. **稳定公网地址**——快隧道重启会换址
4. **38 题评分复测**——`2ac2884` 之后没跑过
5. TikZ 渲染（需加编译失败兜底）
6. 悬停翻译（官方是「划词**或**悬停」，划词已满足，优先级低）

## 8. 环境备忘

- 虚拟环境：`D:\alonely\deepseek\campus-agent\.venv`
- 启动：`.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 页面：`http://127.0.0.1:8000/ui/`，写作助手 `/ui/writing.html`
- 无 Playwright、无 node CLI，但可执行文件在 PATH；Edge DevTools 可用 `--remote-debugging-port=9333` + `websocket-client` 驱动截图
- `scripts/` 下的独立脚本必须 `sys.path.insert(0, 仓库根)`，否则 `ModuleNotFoundError: No module named 'app'`