# 赛题 1.1.2「PDF 文件解析」对照报告与整改方案

> 生成日期：2026-11-16
> 对照对象：赛题原文 1.1.2 的 11 项解析粒度
> 核查范围：`app/structure.py`、`app/ocr.py`、`app/papers.py`、`data/meta/structures.json`
> 结论口径：每一条判定都必须由实跑命令或数据文件字段支撑，没有证据的不写

---

## 0. 一句话结论

**11 项里 5 项确实做到了，4 项「做到了但有明显硬伤」，2 项基本没做到（附录、作者-单位关联）。**

最要命的不是 11 项有没有，而是**同一批代码被 `max_pages=25` 截断**：
长期论文的表格丢了 **215/417（52%）**、图表丢了 **236/454（52%）**、公式丢了 **661/1738（38%）**、
章节丢了 **728/1426（51%）**。而赛题原文点名的两篇（扫描件 15 页、书籍 541 页）恰好一个是"扫描"、一个是"很长"。

---

## 1. 十一项逐条对照

统计口径：`data/meta/structures.json` 覆盖 41 篇（磁盘上 43 篇，缺 2 篇未生成结构）；全库合计
章节 1003（一级 355 / 二级 584 / 三级 64）、表格 298、图表 298、公式 1538、参考文献条目 3285。

| # | 赛题要求 | 判定 | 覆盖篇数 | 硬证据 |
|---|---|---|---|---|
| 1 | 标题 | ✅ 做到 | 40/41 | `1706.03762v1.pdf` → `Attention Is All You Need` |
| 2 | 作者（含顺序） | ⚠️ 部分 | 40/41，≥2 人 34/41 | `['Ashish Vaswani','Noam Shazeer','Niki Parmar',…]` 顺序正确 |
| 3 | 联系方式 / 工作单位 | ⚠️ 部分 | 单位 38/41，邮箱 23/41 | 有，但**单位和作者没有映射**，且 11/41 篇单位列表里混进邮箱 |
| 4 | 摘要 | ✅ 做到 | 40/41 | `abstract` 800 字（Attention） |
| 5 | 一级标题 | ✅ 做到 | 41/41 | `Introduction` / `Background` / `Model Architecture` … |
| 6 | 二级 / 三级标题 | ✅ 做到 | 41/41 | `3.2.2 Multi-Head Attention`（level=3）确实抓到了 |
| 7 | 表格 | ⚠️ 部分 | 38/41；**有单元格的仅 231/298** | 表格 1/2 抽出了完整行列；但 67 张只有题注没内容 |
| 8 | 图表（chart） | ⚠️ 部分 | 40/41 | Attention 5 个图全在，含题注与页码 |
| 9 | 公式 | ⚠️ 部分 | 37/41 | 1538 条里**仅 46% 有强数学符号**，53% 只是"含等号的行" |
| 10 | 参考文献 | ✅ 做到 | 39/41 | Attention 41 条；Llama 3 239 条 |
| 11 | 附录 | ❌ 基本没做到 | **3/41（7%）** | 实测 10 篇论文有附录，只提到 3 篇 |

### 逐条证据原文

**1 标题** — 实跑 `data/meta/structures.json`：
```
1706.03762v1.pdf: title = 'Attention Is All You Need'
UnderstandingDeepLearning_02_09_26_C.pdf: title = 'Understanding Deep Learning'
1706.03762v1_img.pdf: title = ''          ← 扫描件抽不到
```

**2 作者** — 顺序可信（`_authors` 按 y 坐标自上而下 + 同行按 x 排序）。证据：
```
Ashish Vaswani / Noam Shazeer / Niki Parmar / Jakob Uszkoreit
```
Attention 原文顺序就是 Vaswani, Shazeer, Parmar, Uszkoreit —— 一致。

**3 单位** — 这是 11 项里第二弱的一项，三个独立缺陷：
- **没有作者↔单位映射**：字段只有 `authors: []` 和 `affiliations: []` 两个平行列表，
  没有任何字段说明"谁是 Google Brain、谁是 Google Research"。
- **单位列表混进邮箱**：11/41 篇，例：
  ```
  1706.03762v1.pdf: ['Google Brain', 'avaswani@google.com', 'noam@google.com', 'Google Research']
  1910.10683v4.pdf: ['noam@google.com', 'adarob@google.com', ...]   ← 全是邮箱，一个单位都没抽到
  ```
  根因：`_affiliations` 只按 y 坐标切片，邮箱行和单位行在同一块里。
- **作者≥3 人但单位只有 1 个**：5 篇。

**4 摘要** — `abstract` 字段非空且 >100 字的 40/41。

**5 / 6 标题层级** — 该项是做得最扎实的一块。Attention 论文的 sections 分布：
```
level 1: 9 条   level 2: 15 条   level 3: 3 条
{'number': '3.2.2', 'title': 'Multi-Head Attention', 'level': 3, 'page': 3}
```
全库 1003 个章节，三级都有。

**7 表格** — 单元格抽取用的是 pdfplumber。好的样本（Attention 表 2）：
```
['Model', '', '', '', '']
['', 'EN-DE', 'EN-FR', 'EN-DE', 'EN-FR']
['ByteNet[16]', '23.75', '', '', '']
['GNMT+RL[34]', '24.6', '39.92', '2.3·1019', '1.4·1020']
```
问题样本：`2304.08485v2.pdf`（LLaVA）17 张表里 12 张只有题注、`rows` 为空。

**8 图表** — Attention 论文 5 个图全部命中（含题注+页码）：
```
{'number': '1', 'caption': 'The Transformer - model architecture.', 'page': 2}
{'number': '2', 'caption': '(left) Scaled Dot-Product Attention. …', 'page': 3}
```
只有题注与页码，**不导出图片本身**。赛题问"图表(chart)"，抽题注算不算"解析到"要看答辩口径——
如果评委要"能点开看到图"，这一项就得降级。

**9 公式** — 1538 条，质量分档实测：
```
强数学      711   46.2%
仅等号      819   53.3%     ← 例如 'i = wl'、'r = 1'、'r = 4'
其它          8    0.5%
```
"仅等号"这类是正文行或表格数字被 `_MATH_RE` 误抓。另外 `_formulas` 末尾 `return out[:120]` 写死上限，
4 篇（FlashAttention / TinyLlama / LoRA / 一篇 34 页的）都正好卡在 120。

> **口径注**：上表的 46.2% 用的是**整改前的旧判据**。整改后用同一份旧数据、
> 新版 `_MATH_STRONG_RE` 重测是 **575/1538 = 37.4%**（见 9.2.1）。
> 两个数都对，只是判据不同——现在的正则要求更严，所以旧数据的达标率反而更低。

**10 参考文献** — 最稳的一项。`_find_references` 会**全篇扫**（不受 25 页限制），所以 541 页的书也能拿到：
```
UnderstandingDeepLearning…: {'page': 525, 'count': 232}
2303.08774v6.pdf (GPT-4):    stored 135  /  重解析 193
```

**11 附录** — 最弱的一项。实测 10 篇论文里能定位到附录标题的，只提取到 3 篇：
```
论文                          附录页数  首页  25页内  已提取
1910.10683v4.pdf                  2     44    否       0
2201.11903v6.pdf                  3     31    否       0
2205.14135v2.pdf                  1      7    是       0    ← 窗口内也没抓到
2210.02414v2.pdf                  1     19    是       1
2211.05100v4.pdf                  1     64    否       0
2303.08774v6.pdf                  2     23    是       1
2307.09288v2.pdf                  1     45    否       0
2412.19437v1.pdf                  1     44    否       0
2501.12948v2.pdf                  1     12    是       1
UnderstandingDeepLearning…       3    450    否       0
```
两个独立缺陷（已单独验证）：
- **缺陷 A：窗口外全漏** —— 附录标题在第 25 页之后时 `_sections` 根本没读到。
- **缺陷 B：编号式附录认不出** —— 顶层标题只写 `E`、`G`、`B`，标题行是 `Additional Details` /
  `Input/Output Examples` / `Comparison with Rabe and Staats [66]`，`_appendix` 的正则
  （`_APPENDIX_RE` 或 `^[A-Z](\.\d+)*$`）匹配不到，因为 `_SECTION_RE` 只认数字编号。
  实测三个反例：
  ```
  _SECTION_RE.match('3.2 Model')            -> True
  _SECTION_RE.match('A Appendix')           -> False    ← 连带 Appendix 字样的都一样漏
  _SECTION_RE.match('B.5 Comparison')       -> False
  _SECTION_RE.match('E Additional Details') -> False
  ```

---

## 2. 赛题点名的两篇特例

### 2.1 扫描件 `1706.03762v1_img.pdf`

| 项目 | 数值 |
|---|---|
| 页数 | 15 |
| PDF 文字层 | **0 字**（pymupdf `get_text()` 每页都返回空串，第 0 页只有 1 张内嵌图）|
| OCR 缓存 | `data/papers/ocr/1706.03762v1_img.json`，15 页、**34505 字**，第 0 页 `'Attention Is All You Need\nAshishVaswani*…'` |
| RAG 侧 | ✅ 已接通：`app/rag/pipeline.py:393` 与 `:1134` 无文字层时读 OCR 缓存，实测第 1/2/10 页分别 2436 / 3746 / 3111 字 |
| 结构化侧 | ❌ **完全没接**：`structure.py` 里 grep `ocr` **0 处命中** |

即：**扫描件能被检索、能被问答，但 11 项结构粒度全废**。实跑 fresh 重解析：
```
1706.03762v1_img.pdf 重解析 0.7s
  title='' authors=0 sections=0 tables=0 figures=0 formulas=0 references=count 0 appendix=0
```

另外发现一个**缓存陈旧**问题：`structures.json` 里这篇还存着 27 个 sections，
但按当前代码重解析是 0 —— 说明这份是旧版代码留下的，从未重建过。

OCR 成本实测：单页 **8.6 秒**（200 DPI，2 核）。15 页 ≈ 2 分钟，541 页的书 ≈ 77 分钟，
所以 **OCR 必须离线批处理、不能在上传路径里同步跑**——现有设计（`scripts/ocr_scanned.py` 预生成缓存）是对的。

### 2.2 长篇书籍 `UnderstandingDeepLearning_02_09_26_C.pdf`

| 项目 | 25 页窗口（当前） | 全书 | 丢失 |
|---|---|---|---|
| 页数 | 541 | 541 | — |
| sections | 20 | **439** | 419（95%）|
| figures | 12 | **80** | 68（85%）|
| tables | 0 | 0 | 0 |
| formulas | 0 | **120** | 120（100%）|
| references | 232 | 232 | 0（全篇扫，不受影响）|

耗时：25 页窗口 12.0s，全书重解析（`max_pages` 放大）也是 **12.0s** —— 反而没变慢，
因为 `_find_references` 本来就把 541 页全扫了。**这一项的修复成本几乎为零。**

另外：书的附录其实在 **450 页以后**（第 29 页和第 450 页那两处是正文引用"见附录 A"），
现有窗口无论怎么调都读不到 450 页，除非把 sections 也改成全篇扫。

---

## 3. 全局性硬伤：`max_pages=25` 截断

`app/structure.py:1007` `def parse_structure(path: str, max_pages: int = 25, ...)`，
`sections / tables / figures / formulas` 全部只用前 25 页的行（`:1026 for pno in range(1, min(max_pages, pages))`）。
只有 `references` 走了单独的全篇扫描（`:861 _find_references`）。

**影响面实测**（43 篇里 26 篇页数 >25）：

```
论文                                              页 | 25页窗口 表/图/式/章  | 全书 表/图/式/章
UnderstandingDeepLearning…                      541 | 0/12/0/20          | 0/80/120/439
2303.08774v6.pdf (GPT-4)                        100 | 7/9/5/17           | 19/20/64/69
2407.21783v3.pdf (Llama 3)                       92 | 7/7/7/15           | 30/25/120/72
2501.12948v2.pdf (DeepSeek-R1)                   86 | 4/5/64/20          | 32/19/120/49
2307.09288v2.pdf (Llama 2)                       77 | 12/14/23/41        | 52/34/31/65
2005.14165v4.pdf (GPT-3)                         75 | 12/17/10/19        | 16/25/18/39
2203.02155v1.pdf (InstructGPT)                   68 | 3/9/10/25          | 12/50/25/26
2103.00020v1.pdf (CLIP)                          48 | 8/18/14/18         | 20/22/27/20
2201.11903v6.pdf (Chain-of-Thought)              43 | 8/4/95/21          | 28/5/106/21
```
合计（这 26 篇）：
```
全书合计: 417 表 / 454 图 / 1738 式 / 1426 章
截断丢失: 215 表 / 236 图 /  661 式 /  728 章
```

**这不是理论推演，是下一行代码决定的**：`for pno in range(1, min(max_pages, pages))`。
赛题原文明确要求"解析的粒度越细越好"，而 26/43 篇（60%）的解析被砍了一半。

---

## 4. 直接后果：参考文献推荐功能的「候选 0 篇」

这条链路会串到一起。实测 43 篇的引用解析覆盖：

```
L1 数字体可解        : 9
L2 作者-年份可解     : 20
L3 靠反向可解(待实现) : 4
非 arXiv 来源        : 9
全断                 : 1
```
其中扫描件 `1706.03762v1_img.pdf` 报到「候选 0 篇」的直接原因是：
RAG 侧有 OCR 文字（34505 字，参考文献在第 10 页），但 `citation_counts()` 读的是
**`structure` 抽取链路**（走 pypdf，0 字），所以计数恒为 0。

**修好 1.1.2 的扫描件绑定，这条推荐链路会顺手修好 1 篇 + 另外 3 篇（GPT-4 / Llama 3 / Qwen2.5）只要接上反向引用即可。**
这也是把 1.1.2 和 1.4.1 一起讲的更好叙事。

---

## 5. 整改方案（按性价比排序）

### P0-1 结构解析也读 OCR（解决"扫描件"）

**问题**：`structure.py` 完全不碰 OCR。

**改法**：`parse_structure` 打开 PDF 后先判断"有没有文字层"：
- 若整篇 `get_text()` 总字数 / 页数 < 阈值（建议 50 字/页），改用 OCR 缓存作为行来源。
- OCR 缓存已存在（`app/ocr.py:cached_page`）就直接用；不存在就返回空结构并在日志里写明
  「扫描件未 OCR，请先跑 `scripts/ocr_scanned.py`」，**不要在上传路径里同步跑 OCR**（单页 8.6s）。

**代价**：低。只加一个分支 + 一行日志。
**风险**：OCR 文本没有字号/坐标信息，`_title/_authors/_affiliations` 这些靠版式的方法全部失效，
只能退化到"正则 + 首段启发式"，精度会明显低于原生 PDF。所以**这一项只能拿到"有标题/有章节/有参考文献"，
拿不到"作者-单位映射"**，要如实写进文档。
**验证**：重跑 `1706.03762v1_img.pdf`，期望 title 非空、sections ≥ 20、references.count ≥ 30。

### P0-2 去掉 25 页截断（解决"长篇数据"）

**问题**：`range(1, min(max_pages, pages))` 硬砍。

**改法**：建议**默认值改为 `0 = 不限制`**，内部按 `min(max_pages or pages, pages)` 处理。
理由（有实测支撑）：
- 全书重解析 541 页的书 **12.0s**，跟现在 25 页的 12.0s **一样快**（因为 references 本来就全扫）。
- GPT-4 报告：25 页 6.4s → 全书 10.2s，多 3.8s。
- 结构解析在上传路径上只跑一次（`app/papers.py:496`），且在 `try/except` 里，失败不影响上传。

**风险**：
- 大文件（>500 页）会多几秒；可用 `parse_structure(max_pages=0)` + 上传时给个上限（如 400 页）折中。
- 内存：541 页全量 lines 实测无异常，但建议加一句峰值日志。

**验证**：重跑 26 篇长论文，对照本文第 3 节的"全书"列，确认 tables/figures/formulas/sections 都到位。
**必须同时**：`scripts/` 下补一个"重建全部 structures.json"的脚本（现在没有），否则历史数据永远是旧的
—— GPT-4 的 references 现存 135、重解析 193 就是这种漂移。

### P0-3 附录识别（补上唯一"没做到"的项）

两个缺陷，改法分两步：
1. **`_SECTION_RE` 增加字母编号**：`A`/`B.5`/`E` 这类章节（要求同样满足"字号更大或加粗 + 贴左栏"，
   避免把正文词首大写字母当成编号）。
2. **`_appendix` 识别编号式附录**：当一级标题是**纯字母**（`A`…`Z`）且它在 References 之后出现时，
   把它连同其下的 `A.1` / `B.5` 一起判为附录；标题里带 `Appendix|附录` 的仍然照旧。

**代价**：中。要动 `_sections` 这个全模块最核心的函数，**回归风险最高**。
**验证**：现有 `tests/test_structure.py` 全过 + 新建 3 条用例
（Chain-of-Thought 的 `E Additional Details`、FlashAttention 的 `B.5`、正常的 `Appendix` 标题），
再重跑全库确认 sections 总数变化可解释。

### P1-1 作者-单位映射（补"联系/工作单位"）

**改法**：新增字段 `author_affiliations: [{name, affiliation, email}]`。
靠首尾页的**上标数字/符号**关联（`¹` `²` `*`），抽不到就留空，**不要瞎猜**。
**代价**：中高。各刊排版差异极大，命中率不会高（估 40~60%）。
**建议**：如果时间紧，**优先级放到 P2**，答辩时用"平行列表 + 邮箱"讲，承认未做逐人映射。

### P1-2 公式质量（把 53% 的噪声压下去）

**改法**：`_formulas` 收紧——去掉"只要含 `=` 就算"的条件，改为**要求至少有一个强数学信号**
（`∑ ∫ √ ∈ Θ ∂ ∇`、上下标、希腊字母、`softmax/exp/log` 等），同时把 `[:120]` 上限改成可配置。
**代价**：低。只改一个函数。
**风险**：会少抓一些"纯字母等式"（`x = y` 这种在论文里也可能是真公式），宁缺毋滥。
**验证**：重跑 `scripts/_formula.py` 口径的分类脚本，期望"强数学"占比从 46% 升到 75%+。

### P1-3 表格单元格（231/298 → 目标 260+）

**改法**：`_table_rows` 目前只用 pdfplumber 默认策略。可加一次 `table_settings={"vertical_strategy":"lines","horizontal_strategy":"text"}` 的回退尝试。
**代价**：中。要逐页重试，耗时会涨。
**建议**：先只对"题注有、rows 空"的 67 张表做补抽，别全量改。

---

## 6. 优先级建议

| 优先级 | 事项 | 为什么排这儿 | 预计改动 |
|---|---|---|---|
| **P0-2** | 去掉 / 放宽 25 页截断 | 一行代码，恢复 60% 论文一半的解析结果，性价比最高 | `structure.py:1007,1026` |
| **P0-1** | 结构解析读 OCR 缓存 | 赛题原文点名的扫描件，现在 11 项全废；且顺手修好推荐链路 | `structure.py` 新增分支 |
| **P0-3** | 附录识别 | 唯一判定"没做到"的项，7% 覆盖太扎眼 | `_sections` + `_appendix` |
| P1-2 | 公式去噪 | 53% 是噪声，评委抽样一看就露 | `_formulas` |
| P1-3 | 表格单元格回退 | 78% 已有，补到 87% | `_table_rows` |
| P1-1 | 作者-单位映射 | 工作量大、命中率低，可坦诚不做到位 | 新字段 + 新逻辑 |
| — | 重建 `structures.json` 脚本 | **P0 改完必须配套**，否则数据不更新 | `scripts/` 新增 |

---

## 7. 一条必须说的前提

**所有 P0 改完之后，`data/meta/structures.json` 必须整体重建**，否则首页仍显示旧数字。
现在还**没有**这样一个重建脚本（`scripts/` 下只有 `ocr_scanned.py`），
这是 P0 的配套项，不是可选项。

另外，`structures.json` 里 `1706.03762v1_img.pdf` 现存 27 个 sections 而重解析为 0、
GPT-4 参考文献现存 135 而重解析 193 —— **这两处证明这份数据已经和当前代码对不上了**，
不管改不改代码，都该重建一次。

---

## 8. 复现命令

```powershell
$env:PYTHONPATH="D:\alonely\deepseek\campus-agent"
$env:PYTHONIOENCODING="utf-8"

# 11 项覆盖统计 + 两篇特例
.venv\Scripts\python.exe scripts\_audit_112.py

# 25 页截断损失（逐篇对照）
.venv\Scripts\python.exe scripts\_trunc.py

# 附录覆盖与两种失败模式
.venv\Scripts\python.exe scripts\_appx.py
.venv\Scripts\python.exe scripts\_appx2.py

# 公式质量分档
.venv\Scripts\python.exe scripts\_formula.py
```

> 上述四个脚本目前是临时核查脚本（下划线开头），结论已写进本文。
> 如果希望长期留档，建议整理成 `scripts/audit_112.py` 并去掉下划线前缀。

> **2026-11-16 补记**：这四个核查脚本已在整改完成后删除（结论全部固化进本文第 9 章）。
> 全量重建改用长期脚本 `scripts/rebuild_structures.py`，见第 9 章。

---

# 9. 整改落地结果（2026-11-16）

> 本章回答一个问题：**第 5 章的方案改完之后，11 项各自到了什么水平。**
> 所有数字都来自整改后实际重建的 `data/meta/structures.json`（45 条），
> 不是估算。重建命令与验证命令见 9.7。

## 9.1 整改前后总账

| 项目 | 整改前 | 整改后 | 变化 |
|---|---:|---:|---:|
| 章节 | 1003 | 2267 | +1264 |
| 表格 | 298 | 540 | +242 |
| 表格有单元格 | 231 | 417 | +186 |
| 图表 | 298 | 561 | +263 |
| 图表带图片文件 | 0 | 319 | +319 |
| 公式 | 1538 | 3431 | +1893 |
| 参考文献条目 | 3285 | 5929 | +2644 |
| 附录 | 3 | 473 | +470 |
| 单位列表混进邮箱 | 61 | 0 | −61 |
| 有「作者→单位」映射的篇数 | 0 | 18 | +18 |

## 9.2 十一项覆盖率（分母 45 篇）

| # | 赛题要求 | 整改前 | 整改后 | 说明 |
|---|---|---|---|---|
| 1 | 标题 | 40/41 | **44/45** | 唯一缺的是扫描件，需先跑 OCR |
| 2 | 作者（含顺序） | 40/41 | **44/45**，≥2 人 38/45 | 速度保持 |
| 3 | 联系方式 / 工作单位 | 38/41，含 @ 11 篇 | **32/45，含 @ 0 篇**，映射 18/45 | 单位列表**去掉了 61 条邮箱**；新增 `author_affiliations` |
| 4 | 摘要 | 40/41 | **44/45** | |
| 5 | 一级标题 | 41/41 | **45/45** | |
| 6 | 二级 / 三级标题 | 41/41 | **44/45** | |
| 7 | 表格 | 38/41，有行 231/298 | **42/45，有行 417/540** | 无行表格从 67 张降到 123 张（总表数变多，占比从 22% 降到 23%） |
| 8 | 图表 | 40/41，只有题注 | **44/45，319 张真图片** | 新增图片导出 |
| 9 | 公式 | 37/41，1538 条 | **41/45，3431 条** | 去掉了 `r = 1` 这类噪声；解除了 120 条上限。强数学占比见下方口径说明 |
| 10 | 参考文献 | 39/41，3285 条 | **44/45，5929 条** | 之前的"缺口"主要不在正则，而在只读了前 25 页 |
| 11 | 附录 | **3/41** | **30/45** | 修复了字母编号识别 |

### 9.2.1 公式质量的口径说明（别把两个百分比直接相减）

「强数学符号占比」这个指标在整改前后**口径不同，不能直接比较**：

| 统计口径 | 公式行数 | 强数学 | 占比 |
|---|---:|---:|---:|
| 整改前的数据，旧代码产出的全部行 | 1538 | 575 | **37.4%** |
| 整改后的数据，`_is_formula_line()` 过滤后剩下的行 | 3431 | 3333 | **97.1%** |

- **37.4%** 是用**与整改后完全相同的那条 `_MATH_STRONG_RE`** 去量**旧数据**得到的，
  是可比的基线（原第 1 章写的「46%」用的是更早一版更宽的判据，与现在的正则不可比，以 37.4% 为准）。
- **97.1%** 高不是因为"模型变好了"，而是因为 `_is_formula_line()` 已经把
  `r = 1`、`i = wl` 这类不含数学结构的行**在入库前就扔掉了**。
  分子涨（575 → 3333）、分母也涨（1538 → 3431），但分母涨得更少，占比自然跳上去。

一句话：**整改做的是"少收垃圾"，不是"多收公式"。**

## 9.3 五处具体改动

### ① 去掉 `max_pages=25` 截断（改动一行，收益最大）

`parse_structure(path, max_pages: int = 0, ...)`，`limit = pages if max_pages <= 0 else min(max_pages, pages)`。

单篇收益举例（同一份 PDF 整改前后）：

| 论文 | 改动前 | 改动后 |
|---|---|---|
| `2412.19437v1`（DeepSeek-V3，60 页） | 章节 54 / 表 3 / 图 8 / 式 66 | 章节 86 / 表 9 / 图 11 / 式 22 |
| `2307.09288v2`（Llama 2，92 页） | 章节 41 / 表 12 / 图 14 / 式 23 / 参考文献 35 | 章节 93 / 表 52 / 图 34 / 式 7 / **参考文献 234** |
| `2407.21783v3`（Llama 3，92 页） | 章节 15 / 表 7 / 图 7 / 参考文献 36 | 章节 72 / 表 30 / 图 25 / **参考文献 503** |
| `UnderstandingDeepLearning`（541 页） | 章节 20 / 图 12 / 参考文献 0 | 章节 507 / 图 80 / **参考文献 232** |

**耗时没有变差**：重建 43 篇总耗时 512 秒（8.5 分钟），最长单篇 Llama 2 57 秒。
原因是原来 `_find_references` 本来就要扫到末页，截断省下的时间本来就不存在。

### ② 公式去噪

新增 `_is_formula_line()`：长度 2~160、英文词 <8，且满足「强数学符号」或「整行无英文字母的纯符号式」或「`F(x) = ...` 形式的函数定义」。

判据实测（改后）：

| 文本 | 判定 |
|---|---|
| `r = 1` | ❌ 不是公式 |
| `i = wl` | ❌ |
| `softmax(Q K^T)` | ❌ |
| `√dk` | ✅ |
| `PE(pos, 2i) = sin(pos/10000)` | ✅ |
| `MultiHead(Q, K, V) = Concat(head1, ..., headh)` | ✅ |
| `FFN(x) = max(0, xW1 + b1)W2 + b2` | ✅ |
| `We use the Adam optimizer (Kingma & Ba, 2015).` | ❌ |

**走过的弯路**：第一版只认强数学符号，Attention 论文公式从 17 掉到 6，丢掉了
`FFN(x) = max(0, xW1+b1)W2+b2` 这种全是字母和括号的真公式。加了"函数定义式"识别才补回来。

### ③ 表格单元格回退

pdfplumber 抽不出行的表，改用 pymupdf 的 `page.find_tables()` 再试一次。
**跳过有内嵌图片的页**——LLaVA 论文那 12 张无行表全是内嵌位图示例，没有文字可抽，重试也是白试。

实测：全库 67 张无行表里，有内嵌图 14 / 有 pymupdf 表格框 22 / 纯文字页 34，
其中"有框无图"的 21 张回退成功 8 张。

### ④ 图表导出图片

新增 `_figure_shots()`：按题注定位，取「题注上方到上一个文字行」之间的矩形，
`page.get_pixmap(clip=box, dpi=110)` 渲染成 PNG，存到 `data/meta/figures/<stem>_figN.png`，
路径写回 `figure["image"]`。

Attention 论文导出 5 张（53KB / 32KB / 12KB / 12KB / 9KB），
目视确认 fig1（Transformer 整体架构）、fig2（Scaled Dot-Product Attention 与 Multi-Head）
裁切完整、文字清晰可读。

### ⑤ 附录识别

两个独立缺陷：

1. `_SECTION_RE` 只认数字编号，`A Appendix` / `B.5 Comparison` 压根进不来。
   新增 `_APPENDIX_SECTION_RE` 单独认字母编号——**没有放宽 `_SECTION_RE`**，
   因为它还用于判断"这一行是不是章节标题"，放宽后 `As shown in Table 2` 也会命中。
2. 不少论文把编号和标题拆成两个行对象：`F` 一个、`Appendix: Full Prompts` 一个。
   新增 `_appendix_sections()` 补扫这种两行写法，并过滤掉 checklist / 表格里的字母编号。

实测：`2201.11903v6`（CoT）0 → 21 节；`1910.10683v4`（T5）0 → 21 节；
`2211.05100v4` 0 → 4 节；`2412.19437v1` 0 → 2 节。

### ⑥ 作者-单位映射

新增 `author_affiliations` 字段：`{"Ashish Vaswani": ["Google Brain", "avaswani@google.com"], ...}`。

做法是按 x 聚类把首页作者区分成列，每列最上面的人名当列主，往下遇邮箱或机构词就记到列主名下。
版式规整的论文能到满分（Attention 8/8）；整块共享单位的（Llama 2 的 `GenAI, Meta`）用"整行宽 > 260pt"识别后记给所有人。

**试过并放弃的方案**：写过一个 `_split_author_line()` 想把
`Peter Albert Amjad Almahairi Yasmine Babaei` 这种挤成一行的多名作者按词长拆开，
实测把 `Henrique Ponde de Oliveira Pinto` 拆成 `Oliveira Pinto`——
用一个更糟的错误换一个错误，整段删掉了。这类版面**做不到就不写这个键**，
而不是给一个错的映射。

## 9.4 顺带修掉的数据污染

`_affiliations()` 原来只要"含机构关键词"就收，实测混进 61 条邮箱（如 `avaswani@google.com`），
还把 ZeRO 论文的摘要正文 `...researchers have used the system breakthroughs` 当成了单位
（因为含 `research` 这个关键词）。

改后三重过滤：含 `@` 跳过、命中章节标题跳过、长度 > 60 跳过。
**全库"单位列表含 @"的条数：61 → 0。**

## 9.5 还没做到的

1. **扫描件仍然解析不出结构。** `1706.03762v1_img.pdf` 文字层是 0 字，
   `structure.py` 完全不读 OCR 缓存（`app/rag/pipeline.py` 读了，结构侧没读）。
   重建后它的 sections 从 27 掉到 0，章节树靠 `scripts/borrow_sections.py`
   从同 arXiv 编号的文本版借（27 节），前端会标注来源。
   **要真正解决必须让 `parse_structure` 接入 `app/ocr.py`**，单页 OCR 8.6 秒，
   15 页约 2 分钟，只能离线批处理。这是本次没做的最大一块。
2. **作者-单位映射只有 18/45 篇。** 双栏不规则版面（PPO、GPT-3）仍然映射不上，
   宁可不写也不给错的。
3. **公式"强数学占比"不能看百分比的绝对变化。** 同一正则在旧数据上是 37.4%，
   新数据上是 97.1%（见 9.2.1），跳升的原因是分母里的噪声行被过滤掉了，不是识别能力提升。
   真正要知道"还有没有真公式被漏"，得抽样人工看漏掉的行，本轮没有做。
4. **表格无内容率没有实质下降**（22% → 23%）。剩下的 123 张无行表里，
   14 张是内嵌位图、34 张是纯文字页无框，没有可抽的结构。

## 9.6 回归保障

- `pytest tests -q` → **837 passed, 13 warnings in 78.20s**
- `ruff check .` → **All checks passed!**
- 中途出现过的 `1 skipped`：`test_borrow_section_tree_exposes_source` 依赖
  `borrow_sections.py` 写入的 `sections_borrowed_from` 标记，重建会覆盖掉它。
  **这是重建流程的已知副作用**，必须紧接着跑一次 `scripts/borrow_sections.py`（见 9.7）。

## 9.7 复现命令

```powershell
# 全量重建（约 8.5 分钟，43 篇，含图表渲染）
.venv\Scripts\python.exe scripts\rebuild_structures.py

# 重建会覆盖掉扫描件的借树标记，必须补跑
.venv\Scripts\python.exe scripts\borrow_sections.py

# 只重建指定几篇（调试用，不写备份）
.venv\Scripts\python.exe scripts\rebuild_structures.py --only 1706.03762v1.pdf,2307.09288v2.pdf

# 只比数不写盘
.venv\Scripts\python.exe scripts\rebuild_structures.py --dry-run
```

`rebuild_structures.py` 是本轮新建的长期脚本（之前**没有**任何重建入口，
这本身就是"数据与代码对不上"的根因）。写盘前会备份成 `structures.json.bak`。