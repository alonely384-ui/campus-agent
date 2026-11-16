# 扫描件解析修复 + 全功能回归报告

日期：2026-11-16
指令：m31099「给他修了，然后对所有全部功能跑一遍测试，我给你所有权限，我先去军训了」

---

## 0. 一句话结论

官方点名的三篇测试文件**全部通过**：标准论文 ✅、541 页书籍 ✅、扫描件 ✅（本轮修复）。
全功能回归：**pytest 848 passed / 1 skipped、ruff 全过、前端 17 个脚本块语法全过、接口冒烟 56/56、浏览器端到端 18/18**。
未提交、未部署（按用户要求）。

---

## 1. 修的是什么

上一轮 `docs/PARSE-112-OFFICIAL-CHECK.md` 的判定里，11 项参考粒度只有一处硬性没过：

> 扫描件 `1706.03762v1_img.pdf` 标题/作者/摘要/表/图/公式/参考文献全空，
> 22~27 个章节是从 `1706.03762v1.pdf` **借**来的（`sections_borrowed_from`）。

根因不是"做不到"，而是**差一步**：

| 事实 | 数字 |
|---|---|
| 该 PDF 文字层字符数（每页 `get_text()`） | 0 |
| OCR 缓存 `data/papers/ocr/1706.03762v1_img.json` | 存在，15 页 / 34491 字 |
| `app/structure.py` 里 `ocr` 出现次数 | **0** |
| RAG 侧是否读 OCR | 读了（`app/rag/pipeline.py:393`、`:1134`） |

也就是说：离线 OCR 早就跑完了、RAG 一直在用，**只有结构解析这一环没接**。

---

## 2. 改了什么

唯一改动文件：`app/structure.py`（+ 测试）。

### 2.1 新增扫描件分支

`parse_structure` 在抽出文字层行之后判断：

```python
# 扫描件：文字层整篇为空（"1706.03762v1_img.pdf" 每页 0 字、只有一张内嵌图）。
# 这种情况走 OCR 缓存那条路——缓存是离线批处理好的，这里只读文件、不做 OCR。
# 判据用 `head` 而不是只测首页：有的 PDF 首页有页眉文字、正文却整页是图。
if not head:
    scanned = _parse_scanned(path, pages, figure_dir)
    if scanned:
        return scanned
    return empty
```

**关键约束：只读缓存，绝不在请求线程里跑 OCR**（单页 OCR 实测 8.6 秒）。

### 2.2 新增的 12 个辅助函数

| 函数 | 作用 |
|---|---|
| `_ocr_pages` | 读 `app.ocr.load_cache`，返回 `{页码: 文字}` |
| `_ocr_lines` | 把纯文字包成与 `_lines()` 同形状的 dict（无版式：`size` 固定、`x=0`、`y` 按行序） |
| `_despace_ocr` | 补回 OCR 丢掉的空格 |
| `_ocr_looks_like_heading` | 无字号可依据时，靠"编号开头 + 大写开头短标题 + 不是句子"认章节 |
| `_parse_scanned` | 扫描件专用解析路径，返回 `"source": "ocr"` |
| `_is_affil_text` | 判机构行（**必须先 `lower()`**，词表全小写） |
| `_clean_ocr_name` | 剥行尾角标 `*†‡∗⋄§¶` |
| `_fix_ocr_leading_i` | OCR 把行首 `I` 认成 `l` / `\|` 的修复 |
| `_looks_like_person` | 2~5 词、姓名词形、非机构词、非功能词 |
| `_is_initial` | 放行 `Aidan N. Gomez` 这种单字母中间名 |
| `_references_ocr` | 给 `[15]Lukasz` 补空格后复用原有 `_references` |
| `_ocr_name_columns` | 按作者尾名的出现顺序重排列，用于单位配对 |

### 2.3 单位配对的做法

扫描件没有坐标，`_author_affiliations()`（靠 x 聚类分列）用不上。改为：作者按列内出现顺序重排 → 单位按出现顺序轮转分配。
Attention 这篇是 4 列 × 2 位作者 + 3 个单位，实测分配：

| 作者 | 单位 |
|---|---|
| Ashish Vaswani | Google Brain |
| Noam Shazeer | Google Research |
| Niki Parmar | University of Toronto |
| Jakob Uszkoreit | Google Brain |

**这是推断，不是原版面直读**。原文实际是按列对齐的（Ashish 和 Llion 都对应第 1 列的 Google Brain），所以部分作者的单位会错位。这一点如实记在报告里，不粉饰。

---

## 3. 效果（决定性输出）

### 3.1 扫描件解析前后

```
           修复前   修复后
title        ''     'Attention Is All You Need'
authors       0      8
affiliations  0      3
auth-affil    0      8
abstract      0    800 字
sections     27*     22      *借自 1706.03762v1.pdf
tables        0      3
figures       0      3
formulas      0      7
references    0     39
appendix      0      0
source        -    "ocr"
```

实测耗时：**2.36 秒**（对比：单页 OCR 8.6 秒 × 15 页 ≈ 2 分钟）。

### 3.2 章节树（前端实拍）

阅读器打开扫描件，侧栏现在显示 22 个节点，不再是"没解析到章节"：

```
1  Introduction                       p1
2  Background                         p2
3  Model Architecture                 p2
  3.1  Encoder and Decoder Stacks     p2
  3.2  Attention                      p3
    3.2.1  Scaled Dot-Product Attention    p3
    3.2.2  Multi-Head Attention            p4
    3.2.3  Applications of Attention in our Model  p4
  3.3  Position-wise Feed-Forward Networks    p5
  ...
5  Training                           p6
6  Results                            p7
7  Conclusion                         p10
5  References                         p10
```

**`#tocNote`（"章节树借自文本版"）不再出现** —— 这是它已经自己解析出结构的直接证据。

### 3.3 全库数据变化

重建前先备份 `data/meta/structures.json.bak-before-ocr`（1884024 字节）。
`scripts/rebuild_structures.py --only 1706.03762v1_img.pdf` 输出：

```
现有 45 条结构记录；data/papers 下 43 个 PDF
[1/1] 1706.03762v1_img.pdf    2.1s  sections 27→22  tables 0→3  figures 0→3  formulas 0→7
已写入 data/meta/structures.json（45 条）
```

逐条比对确认**只有 1 条变化**：

```
记录数 旧 45 新 45
有变化的条目 1: ['1706.03762v1_img.pdf']
```

### 3.4 全库覆盖率（`scripts/check_112.py`）

```
总篇数 45
  标题            45/45   100.0%
  作者            45/45   100.0%
  摘要            45/45   100.0%
  章节            45/45   100.0%
  一级标题         45/45   100.0%
  二三级标题       44/45    97.8%
  表格            43/45    95.6%
  图表            45/45   100.0%
  公式            42/45    93.3%
  参考文献         45/45   100.0%
  附录            30/45    66.7%
  作者-单位映射     19/45    42.2%

  单位列表里混进邮箱的条数：0   （整改前是 61）
  章节 2289 / 表格 543（有单元格 417）/ 图表 564（有图片 322）
  公式 3438 / 参考文献 5968 / 附录 473
```

与修复前对比：标题 44→45、作者 44→45、摘要 44→45、表格 42→43、图表 44→45、公式 41→42、参考文献 44→45。

---

## 4. 全功能回归

### 4.1 单元测试

```
.venv\Scripts\python.exe -m pytest tests -q
848 passed, 1 skipped, 13 warnings in 90.12s (0:01:30)
```

那个 skip 是 `tests/test_reader.py:137`：

```
SKIPPED [1] tests\test_reader.py:137: 当前数据里没有被借树的论文（structures.json 可能刚被整体重算过）
```

**这是本轮修复的直接产物**：以前只有扫描件需要借章节树，现在它能自己解析了，"借树"功能没有测试对象，所以跳过。不是回归。

### 4.2 静态检查

```
.venv\Scripts\python.exe -m ruff check .
All checks passed!
```

### 4.3 前端语法

抽出全部 HTML 里的内联 `<script>` 与独立 `.js`，逐个 `node --check`：

```
admin-login.html   1 块   analysis.html   1 块   discover.html   2 块
graph.html         1 块   guide.html      无     index.html      2 块
library.html       2 块   project-tour.html 1 块 reader.html     1 块
settings.html      1 块   study-plan.html 1 块   writing.html    1 块

共检查 17 个脚本块/文件，失败 0
```

### 4.4 接口冒烟（56 项）

覆盖 OpenAPI 里全部 57 个路径 / 60 个操作，分六组：公开只读、单篇论文链路、写作模块、管理接口、参数非法路径、页面可访问性。

```
通过 56 项，失败 0 项
```

摘录关键几条：

```
[PASS] GET /health                    200  {"status":"ok","version":"2.1.0"}
[PASS] GET /graph                     200  nodes 38 / edges 210
[PASS] GET /paper/{f}/structure       200  title='Attention Is All You Need' authors=8 sections=22
[PASS] GET /paper/{f}/figure/0        200  image/png 58194 bytes  crop=prebuilt
[PASS] POST /writing/zh2en            200  {"source":"你好","translation":"Hello."}
[PASS] POST /chat 2MB                 413  {"detail":"请求体超过 1MB"}
[PASS] GET /settings                  503  {"detail":"管理功能未配置，请联系管理员"}
[PASS] GET /nonexistent               404  {"detail":"未找到该接口"}
```

### 4.5 浏览器端到端（18 项）

用系统 Chrome（`channel="chrome"`，`--no-proxy-server`），视口 1600×1000，逐页打开并做真实交互，全程监听控制台：

```
=== 1. 首页 ===       首页可打开 PASS / 无控制台错误 PASS
=== 2. 论文库 ===     有列表 PASS / 无控制台错误 PASS
=== 3. 阅读器 ===     读到标题 PASS ['1 Introduction','2 Background','3 Model Architecture']
                      章节树已渲染 PASS secCount=22
                      不再显示「没解析到章节」 PASS
                      不再标注「章节树借自文本版」 PASS
                      无控制台错误 PASS
=== 4. 分析页 ===     无控制台错误 PASS
=== 5. 图谱页 ===     画布已渲染 PASS / 无控制台错误 PASS
=== 6. 写作页 ===     无控制台错误 PASS
=== 7. 发现页 ===     下拉框已填充 PASS options=43
                      显示论文标题而非文件名 PASS 'Layer Normalization'
                      无控制台错误 PASS
=== 8. 设置页 ===     无意外控制台错误 PASS / 按设计提示未配置 PASS（503 两条）

通过 18 项，失败 0 项
```

---

## 5. 如实交代的问题

### 5.1 测试过程中我自己的三次判断错误（不是代码缺陷）

| # | 我写错的期望 | 实际 | 结论 |
|---|---|---|---|
| 1 | `check_112.py \| Select-Object -First 60` 判 exit=1 | 脚本实际 exit=0 | PowerShell 管道截断导致，不是脚本失败 |
| 2 | `/ui/quick-translate.html` 应为 200 | 该文件不存在（`reader.html` 内含翻译面板） | 我凭印象写的页面清单 |
| 3 | 阅读器用 `?file=` 打开 | 实际参数是 `?paper=`（`web/reader.html:524`） | 我的脚本错，不是阅读器错 |

另外接口冒烟的 4 项首轮"失败"也全部是我的期望值写错（管理接口在本机未配 `ADMIN_TOKEN` 时按设计返回 503 而非 401；`/graph/local` 空体返回 400 而非 422）。修正断言后 56/56 全过。**没有一项是接口真的有问题。**

### 5.2 尚未解决的真实问题

| 问题 | 影响 | 为什么没在本轮做 |
|---|---|---|
| **摘要里仍有 OCR 粘连词**（`Thedominantsequencetransductionmodels`、`arebased oncomplexrecurrent`） | 只影响显示可读性，**不影响章节/作者/参考文献等结构化字段**（那些走的是分行判定，不走摘要文本） | 试过按本文词频做动态规划切分，10 个样本只切开 6 个。用户已决定单独记待办 |
| **作者-单位配对是推断的** | 4 列 × 2 位作者的版面里会错位（原文按列对齐，轮转分配会串） | 无坐标可依据；要精确需做 OCR 框的行列归属，成本远大于收益 |
| 作者-单位映射全库仅 19/45 篇 | 双栏不规则版面（PPO、GPT-3、70 人的大团队）映射不上 | 同上一项 |
| 附录 30/45 = 66.7% | 分母有水分：15 篇"空"里 12 篇原文真没附录、2 篇 PDF 不在磁盘 | 按"有附录的论文"重算是 30/31 = 96.8% |

### 5.3 搜到的、但没动的

`data/meta/figures/` 下仍有两套命名并存：
- 顶层 `<stem>_fig<N>.png` 316 张（`app/structure.py` 写，接口现在**优先读它**）
- 子目录 `<stem>/fig<N>.png` 32 张（`app/figures.py` 按需裁，缓存用）

本轮没合并，只让接口优先读顶层。两套并存不会出错，但会浪费磁盘。

---

## 6. 复现命令

```powershell
cd D:\alonely\deepseek\campus-agent
$env:PYTHONIOENCODING = "utf-8"

# 1. 单元测试
.venv\Scripts\python.exe -m pytest tests -q

# 2. 静态检查
.venv\Scripts\python.exe -m ruff check .

# 3. 全库覆盖率 + 数据同步 + 抽样看内容
.venv\Scripts\python.exe scripts\check_112.py

# 4. 只看扫描件解析得对不对
$env:PYTHONPATH = "D:\alonely\deepseek\campus-agent"
.venv\Scripts\python.exe -c "from app.structure import parse_structure as p; s=p('data/papers/1706.03762v1_img.pdf'); print(s['title'], len(s['authors']), len(s['sections']), s['references']['count'])"

# 5. 重建全库结构（慎用，会跑十几分钟）
.venv\Scripts\python.exe scripts\rebuild_structures.py --dry-run
```

浏览器验收：起本地服务后打开
`http://127.0.0.1:8012/ui/reader.html?paper=1706.03762v1_img.pdf`
看左侧「章节」是否展开 22 条、有没有"借自文本版"的提示。

---

## 7. 仓库状态

未提交。`git status --short` 里与本轮相关的：
```
 M app/structure.py
 M tests/test_structure_fields.py
 M data/meta/structures.json
?? data/meta/structures.json.bak-before-ocr
?? scripts/rebuild_structures.py
?? scripts/check_112.py
?? docs/PARSE-112-OFFICIAL-CHECK.md
?? docs/PARSE-112-AUDIT.md
```
其余 `M` 项（`app/main.py`、`app/online.py`、`app/security.py`、`web/*.html`、`data/meta/online/*` 等）来自更早的几轮改动，本轮未触碰。

临时脚本已全部删除（`scripts/_*.py` 剩余 0）。