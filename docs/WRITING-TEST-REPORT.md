# 写作助手（赛题 1.6）测试与整改报告

- 日期：2026-11-17
- 代码版本：`1501d29` 之后的工作区（本报告随提交一并入库）
- 服务：`http://127.0.0.1:8012`（uvicorn，未开 `--reload`，每次改后端都重启后复测）
- 浏览器：Playwright + 本机 Chrome `C:\Program Files\Google\Chrome\Application\chrome.exe`

## 一句话结论

四项功能在真实接口与真实浏览器下全部可用；本轮修掉 3 个真 bug（架构图缺 Matplotlib、数据可视化选图型不生效、引用校验把整篇正常引用误判），边界输入全部给出明确错误或如实降级。`ruff` 与 `pytest` 全绿。

## 一、赛题原文对照

| 赛题原句 | 现有实现 | 状态 |
| --- | --- | --- |
| 1.6.1「给定一个idea，自动生成 Abstract、Introduction、Related Work 和 References，确保 References 真实没有幻觉」 | `app/writing.py` `generate_framework()`：检索 32 条候选 → 约束生成 → 引用校验 → 参考文献由**代码**从候选池拼装（模型碰不到） | 达标（本轮修复误判） |
| 1.6.2「生成 TikZ / Graphviz / Mermaid / Python Matplotlib 绘图脚本」 | `app/diagram.py`：四种渲染器 | **本轮补齐 Matplotlib** |
| 1.6.3「上传 CSV → 表格/折线/柱状/三维雷达，自动写学术规范 Figure Caption 并提炼核心趋势」 | `app/chart.py`：统计画像 → 选型 → ECharts + PNG + caption + 数字核对 | 达标（本轮修复选型失效与 caption 夹中文） |
| 1.6.4「自动将中文摘要或全文翻译成英文」 | `app/zh2en.py`：7 条硬规则 + 分块翻译 + 术语表 + 磁盘缓存 | 达标 |

## 二、本轮修掉的真 bug

### BUG-1 架构图缺 Matplotlib（赛题点名项）

- 现象：`GET /writing/renderers` 只返回 `{'mermaid': True, 'graphviz': True, 'tikz': True}`，赛题第 2 条点名的 `Python Matplotlib` 不可选。
- 修法（`app/diagram.py`）：
  - 新增 `_DIAGRAM_KINDS = ("mermaid", "graphviz", "tikz", "matplotlib")`，`generate_diagram()` 白名单改用该常量；
  - `renderer_available()` 增加 `importlib.util.find_spec("matplotlib")` 探测；
  - 新增 `render_matplotlib()`：**在子进程里执行模型生成的绘图脚本**（`MPLBACKEND=Agg`、60 秒超时、`OUT` 环境变量约定），避免模型写出死循环或 `plt.show()` 卡死整个服务；脚本跑完没有产出时再用 `plt.savefig(OUT)` 兜底。
  - `build_prompt()` 增加 matplotlib 分支，要求 `fig.savefig(OUT, dpi=200)`、不调用 `plt.show()`、不读本地文件、设置中文字体与负号显示。
- 前端（`web/writing.html`）：渲染器下拉增加 `Matplotlib（Python 绘图脚本，赛题点名）`。
- 验证：四种格式全部 `ok=True`，见第三节。

### BUG-2 数据可视化选图型完全不生效

- 现象：`POST /writing/chart` 无论表单里传 `kind=bar / pie / radar / radar3d / scatter / line`，返回的 `spec.kind` **一律是 `line`**，六次请求返回同一个 `png_url`。
- 定位过程（关键）：先在进程内直接调 `make_chart(CSV, kind="bar")`，**结果完全正常**（`bar` 就是 `bar`），说明纯函数层没问题；再走 HTTP，服务端收到的却是空串。差异只能在端点签名上。
- 根因：`campus-agent/app/main.py` 里端点签名写成

  ```python
  async def writing_chart(file: UploadFile, kind: str = "", note: str = "") -> dict:
  ```

  裸的 `str = ""` 在 FastAPI 里被当成**查询参数**，前端塞进 multipart 表单的 `kind` 永远读不到。同文件里其他上传端点（`web_upload`、`local_graph_upload`）用的是 `Form(default=...)`，唯独这里漏了。
- 修法：

  ```python
  async def writing_chart(
      file: UploadFile,
      kind: str = Form(default=""),
      note: str = Form(default=""),
  ) -> dict:
  ```

- 修复后实测（同一条命令、同一个服务）：

  ```
  发送 kind=None       -> status=200 spec={'kind': 'scatter', ...} png=/writing/chart/514505b07c19775c.png
  发送 kind='bar'      -> status=200 spec={'kind': 'bar', ...}     png=/writing/chart/719c768f8b0a96fc.png
  发送 kind='radar3d'  -> status=200 spec={'kind': 'radar3d', ...} png=/writing/chart/60e18a29757a260e.png
  ```

### BUG-3 引用校验把整篇正常引用判成可疑

- 现象：1.6.1 真实返回 `suspicious_keys: ["C1","C2","C3","C4","C5","C6","C7","C24"]`——8 条引用**全部**被判可疑，等于校验形同虚设。
- 根因：模型会在正文末尾自写一段 `## References`，按行罗列 `[C1] ⏎ [C2] ⏎ [C3]`。校验时对这些标记取「所在句子」，而标记独占一行，取出来是空串，与候选语料的重叠度必然是 0，于是集体定罪。
- 修法（`app/writing.py` `check_citations`）：先用已有的 `_split_references()` 切出正文段，再删掉纯标记行，最后才逐句比对；取到空句子直接跳过，不再凭空句子定罪。
- 验证：构造用例 A（正文引用 + 末尾 References 列表）→ `[]`（修复前 `['C1','C2']`）；用例 B（真的跑题引用）→ 仍然 `['C1']`，没有放宽到误放过。
- 真实接口复测：`suspicious_keys=[]`，`cited_keys` 11 条全部可溯源，`unknown_keys=[]`。

### BUG-4 caption 里夹中文统计键

- 现象：单列文本 CSV 的 caption 是
  `Distribution of records across the three name categories. The chart comprises 样本行数 = 3 entries in total.`
- 根因：`_key_numbers()` 把行数拼成中文串 `样本行数 = 3`，而 caption 要求英文，模型原样照抄。
- 修法（`app/chart.py`）：改为 `row_count = 3`，并加注释说明为什么必须是英文。
- 验证：修复后同一份 CSV → `The chart comprises row_count = 3 entries in total.`，全句无汉字。

## 三、四项功能的实测输出

### 1.6.2 架构图：四种格式

| 格式 | ok | 字节数 | 耗时 | 截图 |
| --- | --- | --- | --- | --- |
| Mermaid | True | 36131 | 3.1s | `_shot_diagram_mermaid.png` |
| Graphviz | True | 16210 | 2.6s | `_shot_diagram_graphviz.png` |
| TikZ | True | 33327 | 4.2s | `_shot_diagram_tikz.png` |
| Matplotlib | True | 32926 | 5.0s | `_shot_diagram_matplotlib.png` |

边界：

| 用例 | 结果 |
| --- | --- |
| 描述为空 | 422 `String should have at least 5 characters` |
| 描述纯空白 | 400 `描述不能为空` |
| `kind=plantuml` | 400 `不支持的图类型: plantuml` |
| 描述 1096 字 | 200，`ok=True`，410497 字节 |

真浏览器（选 Matplotlib 并点生成）：状态「渲染成功 · 53.6 KB」，图片 1745×810，源码含 `savefig` 且不含 `plt.show()`，`pageerror: []`。

### 1.6.3 数据可视化

| 用例 | 结果 |
| --- | --- |
| 4×4 数值 CSV（不指定图型） | 200，自动选 `line`，caption 237 字含 `r = 1.0`，`suspect_numbers: []` |
| `kind=bar` | 200，`spec.kind=bar`（修复前是 `line`） |
| `kind=radar3d` | 200，`spec.kind=radar3d`，y 轴自动取 |

### 1.6.4 中译英

正常摘要（含公式 `$\mathcal{L} = -\sum_i y_i \log p_i$`、引用 `[12]`、URL）：

> This paper proposes a retrieval-augmented method for academic question answering. ... As shown in the formula $\mathcal{L} = -\sum_i y_i \log p_i$, the training objective is consistent with that of conventional classification tasks [12]. The code and data are open-sourced at https://github.com/example/repo.

- 译文残留汉字：0
- 保留 `[12]`：True；保留 URL：True；保留 LaTeX：True
- 术语表：`注意力机制=attention mechanism` / `知识蒸馏=knowledge distillation` → 译文中两个术语都按指定英文出现
- 长文本 3238 字：分 2 块，`chunks=2`，耗时 7.0s，译文残留汉字 0

### 1.6.1 框架生成

- 耗时 188.2s（本轮第一次），正文 3455 字，候选 32 条，参考文献 11 条
- 小节：`## Abstract` / `## Introduction` / `## Related Work` / `### ×3` / `## References`
- `check`: `{"cited_keys": [11 条], "unknown_keys": [], "unused_candidates": [21 条], "suspicious_keys": [], "ok": true}`

## 四、边界与异常（跨功能）

| 用例 | 结果 |
| --- | --- |
| 1.6.1 `idea` 为空 / 纯空白 | 422 `String should have at least 5 characters` |
| 1.6.3 CSV 为空 | 400 `上传的 CSV 是空的` |
| 1.6.3 只有表头 | 400 `CSV 没有数据行` |
| 1.6.3 二进制垃圾 | 400 `无法识别 CSV 编码: 'gbk' codec can't decode byte 0xff in position 3` |
| 1.6.3 单行数据 | 200，选 `scatter`，caption 如实写 "no trend can be inferred" |
| 1.6.4 空文本 | 422 `String should have at least 1 character` |
| 1.6.4 纯空白 | 400 `待翻译文本不能为空` |
| 限流 | 同一端点连打 4 次 → `[200, 200, 200, 429]`，第 4 次返回「请求过于频繁，请稍后再试」 |

## 五、回归结果

```
$ .\.venv\Scripts\python.exe -m ruff check app/ tests/
All checks passed!

$ .\.venv\Scripts\python.exe -m pytest -q tests
927 passed, 1 skipped, 13 warnings in 87.23s (0:01:27)
```

本轮新增测试：

- `tests/test_writing.py`（8 条）：
  - Matplotlib 5 条：`test_matplotlib_prompt_injects_out_variable`、`test_matplotlib_render_outputs_png`、`test_matplotlib_render_reports_script_error`、`test_matplotlib_render_flushes_stale_image`、`test_matplotlib_render_strips_bom`
  - caption 中文 1 条：`test_key_numbers_contain_no_chinese`
  - 引用举报 3 条：`test_model_written_references_list_is_not_suspicious`、`test_bare_marker_line_does_not_convict`、`test_genuinely_off_topic_citation_still_flagged`
- `tests/test_chart_api.py`（新文件，3 条）：`test_form_kind_is_honoured`、`test_form_kind_absent_falls_back_to_auto_choice`、`test_caption_never_contains_chinese`——这一组直接钉死 BUG-2，纯函数层测不出来

## 六、验收截图

截图存放在仓库外的 `D:\alonely\deepseek\_evidence_writing\`（属于本地证据，不入库，避免把 PNG 塞进代码仓库）：

| 文件 | 内容 |
| --- | --- |
| `_shot_diagrams_all.png` | 四种架构图格式纵向拼图（900×2168） |
| `_shot_diagram_mermaid.png` / `_shot_diagram_graphviz.png` / `_shot_diagram_tikz.png` / `_shot_diagram_matplotlib.png` | 四种格式各自的渲染结果原图 |
| `_shot_mpl_ui.png` | 写作页里 Matplotlib 实际渲染的架构图（1745×810，中文正常） |
| `_shot_writing_diagram.png` | 写作页 1.6.2 标签，Matplotlib 渲染结果 |
| `_shot_writing_chart.png` | 写作页 1.6.3 标签，caption + 导出 PNG |
| `_shot_writing_zh2en.png` | 写作页 1.6.4 标签，译文 |
| `_shot_writing_framework.png` | 写作页 1.6.1 标签，全文（full_page） |

## 七、已知问题与未做项（如实记录）

1. **1.6.1 首次生成耗时 188.2s**，超过页面提示的「约 20-60 秒」。第二次同 idea 复测 86s（检索缓存生效）。演示时建议提前跑一次预热。
2. `coverage_gaps` 会把 idea 里的要求逐条列出（如「要求引用可溯源」「不出现幻觉」），这是提示「正文未显式覆盖这些要求」，不是错误。
3. 三维雷达图在网页侧（ECharts 标准版不含 GL）只画三角雷达，真 3D 由 matplotlib 导出的 PNG 承担——两处各有分工，已在 `app/chart.py` 文件头写明。
4. 本报告没有覆盖的：并发压测、长时间运行的资源泄漏、多用户同时上传。这三项不在本轮范围。