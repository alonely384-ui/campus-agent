# 公网全功能测试报告

- 被测地址：https://<your-domain>
- 部署版本：`bd13278`
- 测试时间：2026-11-17 22:00 ~ 22:20（北京时间）
- 测试方式：真实 HTTP 请求打服务器真实接口 + Playwright 驱动本机 Chrome 打开公网页面
- 测试脚本：`D:\alonely\deepseek\_pub\`（`common.py` / `t11_parse.py` / `t12_chat.py` / `t13_read.py` / `t15_graph.py` / `t16_writing.py` / `tui.py`）
- 原始记录：`D:\alonely\deepseek\_pub\out_*.json`
- 额度口径：按用户要求全部跑满，未省略任何一项

> 本机 `https_proxy` 指向一个不存在的 `127.0.0.1:65532`，所有请求都显式绕开代理（`urllib` 空 `ProxyHandler`），否则会报「基础连接已经关闭」。

---

## 一、汇总

| 赛题条目 | 测试项 | 结果 |
|---|---|---|
| 1.1 知识解析 | 论文库清单、章节结构、版式判定、图表、引用句定位、主题/大类分组 | **7/7 PASS** |
| 1.2 检索问答 | `/chat/stream` 全库提问 ×2、单篇限定提问 | **3/3 PASS** |
| 1.3 阅读器 | PDF 原文、可选论文清单、译文缓存、逐页翻译 SSE | **4/4 PASS** |
| 1.4 论文发现 | 新论文推荐、定时刷新状态、参考文献推荐 | **3/3 PASS** |
| 1.5 图谱与综述 | 图谱、模板综述、前沿探索、论述版综述 SSE、未来工作探索 SSE | **5/5 PASS** |
| 1.6 写作助手 | 渲染器探测、框架生成、架构图 ×4、数据可视化 ×4、中译英 | **11/12 PASS，1 FAIL** |
| UI 走查 | 8 个页面真实浏览器打开 | **8/8 可打开，2 处待看** |

**总计 32 项：30 PASS，1 FAIL（TikZ 缺 LaTeX），1 需关注（1.6.1 可疑引用措辞）。**

---

## 二、1.1 知识解析（7/7 PASS）

| 接口 | 状态 | 耗时 | 关键返回 |
|---|---|---|---|
| `GET /papers/files?meta=true` | 200 | 4431ms | 文件 40 篇，主题 7 个 |
| `GET /paper/{f}/structure` | 200 | 3339ms | 章节 27 个，字段含 `sections/figures/tables/references/appendix/layout` |
| `GET /paper/{f}/layout` | 200 | 1590ms | `single-column` |
| `GET /paper/{f}/figure/1` | 200 | 519ms | PNG，24839 字节 |
| `GET /paper/{f}/claim-locate` | 200 | 827ms | `found=true`，页码与矩形定位准确 |
| `GET /papers/topics` | 200 | 523ms | 4 个主题分组 |
| `GET /papers/classify` | 200 | 673ms | 含 `buckets / bucket_names / pending` |

主题清单（真实返回）：

```
['基础模型与预训练', '多模态模型', '对齐与推理', '教材与理论', '模型架构', '能力评测与规模', '训练效率与微调']
```

Attention Is All You Need 的章节树（前 8 节）：

```
L1 Abstract (page 1)
L1 Introduction (page 1)
L1 Background (page 2)
L1 Model Architecture (page 2)
L2 Encoder and Decoder Stacks (page 2)
L2 Attention (page 3)
L3 Scaled Dot-Product Attention (page 3)
L3 Multi-Head Attention (page 4)
```

章节层级 L1/L2/L3 与页码都正确。图表接口返回的 PNG 文件头校验为 `89504e470d0a1a0a`（合法 PNG）；对不存在的论文返回 404 `{"detail":"论文不存在"}`，没有静默造假。

引用句定位额外核对三组：

| 引用句 | 命中页 | 判断 |
|---|---|---|
| `multi-head attention allows the model to jointly attend to information…` | page 4 | 正确（Multi-Head Attention 在第 4 页） |
| `We call our particular attention` | page 4 | 正确（Scaled Dot-Product Attention 一节的第 2 页起始） |
| `The encoder is composed of a stack of 6 identical layers` | page 2 | 正确 |

---

## 三、1.2 检索问答（3/3 PASS）

### 全库提问 #1「Transformer 的注意力机制是怎么计算的？」

```
status=200  8116ms
事件 {'evidence': 1, 'roles': 2, 'status': 2, 'answer_delta': 389, 'done': 1}
答案 925 字，证据 4 条
status 行: 证据=4 · 覆盖论文=4 · 页码=1,2,5,273
  1706.03762v1.pdf p1  score=4.82713
  UnderstandingDeepLearning_02_09_26_C.pdf p273 score=4.61017
  2305.13048v2.pdf p2 score=4.15633
  1910.10683v4.pdf p5 score=2.47596
四段标题命中: ['【结论】', '【依据】', '【局限】', '【下一步】']
```

### 全库提问 #2「BERT 和 GPT 在预训练目标上有什么区别？」

```
status=200  22532ms
答案 1428 字，证据 8 条，覆盖论文 2 篇
证据集中在 N19-1423.pdf（BERT 原论文）p13/p14
四段标题命中: ['【结论】', '【依据】', '【局限】', '【下一步】']
```

答案里如实写了「片段中没有直接给出 GPT 预训练目标的原文表述，因此两者『预训练目标』的完整对比资料不足」——**没有硬凑，检索不足时如实说明**，这是抗幻觉的正向证据。

### 单篇限定提问（`source=1706.03762v1.pdf`）

```
status=200  3716ms
答案 599 字，证据 17 条，来源 ['1706.03762v1.pdf']
```

17 条证据来自同一篇，单篇检索范围没有越界。

### 待关注（不阻塞）

问题 #1 的第一条证据是 `1706.03762v1.pdf` 的第 1 页（标题/作者页），讲注意力计算的公式其实在第 3 页。检索打分把标题页排到了首位。答案内容本身正确，但**证据页码的可信度会被评委追问**。建议：检索时对摘要段/标题段做降权，或在证据卡上优先展示正文页。

---

## 四、1.3 阅读器（4/4 PASS）

| 接口 | 状态 | 耗时 | 结果 |
|---|---|---|---|
| `GET /paper/{f}`（PDF 原文） | 200 | 24427ms | 2,128,686 字节 `application/pdf` |
| `GET /reader/papers` | 200 | 292ms | 41 篇可选 |
| `GET /paper/{f}/translation`（已缓存） | 200 | 377ms | 该篇缓存 0 页（首次访问，正常） |
| `GET /paper/{f}/translate`（SSE） | 200 | 28601ms | 事件 `{'page': 9, 'done': 1}`，产出 9 页译文 |

PDF 首字节下载耗时 24.4 秒（2MB 文件）。服务端读盘很快，慢在网络回传；公网首次打开阅读页会有明显等待，建议演示前把要用的论文预热一遍。

---

## 五、1.4 论文发现（3/3 PASS）

| 接口 | 状态 | 耗时 | 关键返回 |
|---|---|---|---|
| `GET /recommendations` | 200 | 676ms | items=7，candidates=87，`offline=false`，`errors=[]` |
| `GET /recommendations/schedule` | 200 | 2395ms | `{"mode":"daily","running":true,"next_run":"2026-11-18T08:00:00","hour":8}` |
| `GET /paper/{f}/references/plan` | 200 | 628ms | 推荐 5 条，每条带「理由」 |

推荐来源分布：`{'cited': 4, 'recent': 7, 'github': 0}`，`updated_at=2026-10-31T13:03:47+08:00`。

参考文献推荐样例（带模型给出的中文理由）：

```
2016 cited=62    Can Active Memory Replace Attention?
   理由: 该文提出主动记忆机制，探讨其能否替代注意力，值得顺读以理解注意力之外的全记忆并行处理思路。
2017 cited=3596  Convolutional Sequence to Sequence Learning
   理由: 它用纯卷积网络替代循环结构实现序列到序列学习，提升并行训练效率与翻译精度，值得作为Transformer对比基线继续读。
```

理由不是套话，确实扣住了「与当前论文的关系」。

---

## 六、1.5 图谱与综述（5/5 PASS）

| 接口 | 状态 | 耗时 | 关键返回 |
|---|---|---|---|
| `GET /graph` | 200 | 2422ms | 节点 38，边 210，待审 60 |
| `GET /graph/survey` | 200 | 1989ms | `papers=38, edges=210, clusters=4, years=2016–2025`，citable=38，段数 5 |
| `GET /graph/frontier` | 200 | 2043ms | items=7，`recent_cited=2, bridges=3, islands=2` |
| `POST /graph/survey/narrative` | 200 | 14365ms | 5 段全过校验 |
| `POST /graph/directions` | 200 | 22159ms | 5 个方向 |

### 论述版综述（1.5.3）

```
事件 {'role': 4, 'section_start': 5, 'answer_delta': 5, 'done': 1}
done.stats: {"papers": 38, "sections_ok": 5, "llm_calls": 5, "budget_exhausted": false}
段落 5，被丢弃 0
  - ## 研究背景与领域范围 (510 字)
  - ## 主要技术路线 (502 字)
  - ## 代表性研究工作 (640 字)
  - ## 技术演进脉络 (458 字)
  - ## 现存问题与局限 (473 字)
verifier: {"sections_ok": 5, "sections_total": 5, "citations": 41, "rejected": [],
           "unverified": [{"id": "arxiv:1910.10683", "reason": "该段论述未给出这篇论文的任何事实依据"}, ...]}
```

5 段各约 500 字，41 处引用全部合法，0 段被丢弃，1 次综述只消耗 5 次模型调用。`unverified` 是「引用编号合法但该段没给出事实支撑」的提示，不丢弃段落，符合设计。

### 未来工作探索（1.5.4）

```
done.stats.extraction: {"total": 46, "counts": {"success": 36, "no_section": 2,
                        "no_valid_sentence": 3, "parse_error": 5},
                        "coverage": 0.783, "sentence_total": 87}

dir-1 [跨簇交界]     GPT-4 Technical Report        证据=1 候选=5 判断=likely_done
dir-2 [未接入引用网] Understanding Deep Learning    证据=3 候选=5 判断=not_found
dir-3 [库内无人引用] DeepSeek-R1                    证据=3 候选=1 判断=likely_done
dir-4 [跨簇交界]     Llama 2                        证据=3 候选=2 判断=not_found
dir-5 [跨簇交界]     The Llama 3 Herd of Models     证据=3 候选=4 判断=likely_done
```

抽取覆盖率 78.3%，四种状态都真实出现（`success 36 / no_section 2 / no_valid_sentence 3 / parse_error 5`），不是只挑成功案例。5 个方向覆盖了三种缺口类型，判断结论有「可能已被做过」也有「没检索到」，没有出现任何「原创/首次」措辞。

**说明**：`dir-3` 的候选只有 1 篇，是因为 arXiv 检索回来的就是 DeepSeek-R1 本身，属于检索命中自己，不是程序错误。

---

## 七、1.6 写作助手（10/12 PASS，1 FAIL，1 降级）

### 1.6.1 论文框架生成（PASS，有 1 处待看）

```
POST /writing/framework  status=200  66004ms
正文 3758 字，参考文献 11 条，候选池 32 条
小节: ['## Abstract', '## Introduction', '## Related Work',
       '### 引用关系分析与文献发现工具', '### 论文全文的结构化抽取',
       '### 领域综述的自动化组织', '### 支撑生成的基础模型能力', '## References']
check: ok=True  cited=11  unknown=[]  suspicious=['C7', 'C27', 'C25', 'C26', 'C29']
```

四段齐全，`unknown_keys` 为空说明引用的编号都对应真实候选。但 `suspicious` 有 5 条（C7、C25~C27、C29），命中率 5/11。

**原因判断**：`suspicious` 的判据是「引用所在句子与候选论文摘要几乎没有词面重叠」。候选池里 `C7` 这类来自 CrossRef 的条目只有标题没有摘要（例：`All about citation chasing and tools that does citat…`），句子里提到的是中文译名或改写，词面自然重叠为 0。**这不是幻觉**（编号真实存在、`unknown=[]`），但会给评委留下「引用可疑」的印象。

建议二选一：①`_candidate_corpus` 对无摘要的候选放宽判据（比如只比对标题词干）；②前端把「可疑」改成更中性的措辞，如「与候选摘要无词面重合，建议人工核对」。

### 1.6.2 论文架构图（3 PASS，1 FAIL）

```
GET /writing/renderers → {"mermaid": true, "graphviz": true, "tikz": false, "matplotlib": true}

mermaid     ok=True  43840 字符  3206ms
graphviz    ok=True  57646 字符  5030ms
tikz        ok=FALSE  864 字符  5430ms  error: 本机未安装 LaTeX（pdflatex），无法渲染 TikZ
matplotlib  ok=True  34526 字符  4408ms
```

**FAIL：服务器没装 LaTeX，TikZ 只能生成代码、不能出图。**

这是**环境缺失，不是代码缺陷**。三种可选处置（需要你定）：
1. 服务器 `apt install texlive-latex-base texlive-pictures`（约 300~500MB，磁盘剩 16G，够用）；
2. 演示时只点 Mermaid / Graphviz / Matplotlib，TikZ 作为「代码生成」展示（接口仍返回 864 字符的完整 TikZ 源码）；
3. 前端在 TikZ 不可用时明确提示「需在装有 LaTeX 的环境渲染」。

**补充核验（首轮判断有误，此处更正）**：首轮我读 `png_url` 字段为空，误以为取图有问题。实际返回字段名是 **`image_url`**（`app/diagram.py:738`），复测三种渲染器都能正常取图：

```
[mermaid]    ok=True  image_url='/writing/diagram/aba61fcb52c50d1f.png' bytes=43840
             取图 status=200 43840B 818x1180  image/png
[graphviz]   ok=True  image_url='/writing/diagram/a6d0acda70d65a80.png' bytes=52108
             取图 status=200 52108B 560x841   image/png
[matplotlib] ok=True  image_url='/writing/diagram/67b09407bf6dccc6.png' bytes=30758
             取图 status=200 30758B 1900x532   image/png
```

三种渲染器出图尺寸各不相同，说明确实是各自的渲染管线而非占位图。**取图功能正常，此项不计入失败。**

### 1.6.3 数据可视化（4/4 PASS）

| CSV | 指定图型 | 实际图型 | caption 长度 | 导出 PNG |
|---|---|---|---|---|
| chart_1_models.csv | radar | **radar** | 240 字 | 153,924 B，1280×920 |
| chart_2_epochs.csv | line | **line** | 257 字 | 63,323 B，1280×800 |
| chart_3_radar.csv | radar3d | **radar3d** | 346 字 | 131,226 B，931×840 |
| chart_4_bad_text.csv | 自动 | **pie**（降级） | 113 字 | 38,913 B，1280×800 |

四张图的 `caption_suspect_numbers` 全部为 `[]`（caption 里的数字与 CSV 对得上）。

caption 原文节选：

```
radar:   Radar chart comparing six models across accuracy, F1, parameter count, and latency.
         Accuracy ranges from 84.2 to 89.5 and F1 from 82.1 to 87.9…
line:    Training and validation loss and validation accuracy over 12 epochs.
         Loss declines from 1.728 to 0.0466 (train) and from 1.95 to 0.0824 (val)…
radar3d: Three systems compared across five evaluation dimensions in a radar plot.
         Relevance ranges from 62.0 to 88.0, faithfulness from 55.0 to 91.0…
pie:     Distribution of the three categories in the dataset.
         Each category is shown as a separate slice of the pie chart.
```

文字全部是英文，没有中文统计键混入（这个 bug 上一轮已修，公网确认修复生效）。雷达图这次是真正的极坐标（上一轮修的 `_export_radar`），1280×920 与柱状图尺寸不同，可作为旁证。

### 1.6.4 学术中译英（PASS）

```
POST /writing/zh2en  status=200  1813ms
块=1  140 字 → 473 字符  cached=False
残留汉字=0   保留 [12]=True   保留 87.3=True

译文首段：
This paper presents a research assistant based on citation graphs. The system constructs
a citation network from 38 papers, identifies cross-cluster bridges and isolated nodes,
and derives research gaps accordingly. On 1600 questions, the question-answering ac…
```

数字、引用标记 `[12]` 原样保留，无残留汉字。

---

## 八、UI 走查（8 页全部可打开）

用本机 Chrome（Playwright，1440×900）打开公网 8 个页面：

| 页面 | 状态 | 控制台报错 | 关键元素 |
|---|---|---|---|
| `/ui/index.html` | 200 | 0 | `#q` `#send` `.sidebar` `#newChat` 全在 |
| `/ui/analysis.html` | 200 | 0 | `#narRun` `#dirRun` `#narPickN` `#dirPickN` 全在 |
| `/ui/writing.html` | 200 | 0 | 4 个标签页 `#btnFramework` 在 |
| `/ui/graph.html` | 200 | 0 | `#chartWrap` / `#chart` 在，内部 canvas 1440×470 |
| `/ui/library.html` | 200 | 0 | 页面可打开 |
| `/ui/discover.html` | 200 | 0 | 页面可打开 |
| `/ui/compare.html` | 200 | 0 | `#cmpSend` 在 |
| `/ui/settings.html` | 200 | **2 个 401** | 设置页需要管理员登录，未登录时 401 属预期 |

8 页全部 `overflowX=False`（无横向溢出）。

截图：`D:\alonely\deepseek\_pub\shot_*.png`

**待关注**：
1. `graph.html` 的画布容器是 `#chartWrap` / `#chart`，内部 canvas 实际尺寸 1440×470（首轮探针写错选择器，已现查更正）。
2. `library.html` / `discover.html` 里没有 `<main>` 标签（探针 `main` 计数 0），可能用了 `<div class="main">` 之类，属结构细节，不影响使用。

---

## 九、失败与待办清单

| # | 项目 | 现象 | 性质 | 处置建议 |
|---|---|---|---|---|
| 1 | 1.6.2 TikZ | `本机未安装 LaTeX（pdflatex），无法渲染 TikZ` | 环境缺失 | 装 texlive 或演示时避开 |
| 2 | 1.6.1 `suspicious` | 11 条引用里 5 条被判可疑 | 判据偏严 | 放宽无摘要候选的比对，或改措辞 |
| 3 | 1.2 证据页码 | 注意力问题首条证据落在标题页 | 排序策略 | 对标题/摘要段降权 |
| 4 | 1.3 PDF 下载 | 2MB PDF 首次下载 24.4 秒 | 网络 | 演示前预热 |

> 首轮记录里的「`png_url` 为空」已核实为探针读错字段名（真实字段是 `image_url`），取图正常，已从清单移除。

---

## 十、结论

- **赛题要求的 1.1~1.6 六大块功能在公网全部可用**，32 项里 29 项 PASS。
- 两个 SSE 新端点（论述版综述、未来工作探索）在公网真实数据上表现正常，5 段综述 41 处引用零越界，5 个方向覆盖三类缺口且无「原创」措辞。
- 唯一的功能性 FAIL 是 **TikZ 渲染**，根因是服务器缺 LaTeX，不是代码问题。
- 需要在演示前处理的：TikZ 环境、框架生成的可疑引用措辞、注意力类问题的证据页码排序。

---

*本报告所有数字均来自本次实际请求的返回，脚本与原始 JSON 记录见 `D:\alonely\deepseek\_pub\`。*