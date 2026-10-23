# 代码审核指南（给"看不懂但想自己审一遍"的人）

> 结论先说：**别从第一行读。按"一个问题从进来到出去"的数据流读，只读 3 个文件里的 9 个函数，其余全跳。**
> 四个核心文件合计约 2100 行，真正决定行为的大约只有 200 行。

---

## 0. 先建立一个心智模型（30 秒）

一次问答只走四步：

```
用户提问
  → main.py 收下（接口）
  → agent.py 准备"参考资料 + 规则"（RAG 上下文 + System Prompt）
  → 大模型按规则回答
  → 回答里带 [来源: 文件 第N页]
```

而"参考资料"是怎么来的：

```
论文 PDF → pipeline.py 抽出文字 → 切成 300 字小块 → 存进索引（chroma + bm25.json）
提问时   → 三路召回 → RRF 融合 → 取回 4 块原文
```

**审核就是验证这两条线没断。** 不是验证每行代码都对。

---

## 1. 阅读顺序（按数据流，不按文件名）

### 第 1 站：`app/main.py` —— 入口长什么样

只看：
- `:67` `POST /chat` → 18 行，看它怎么把 `req.message` 交给 `run_agent`
- `:164` `GET /papers/files` → 看"单篇选择"的来源（扫目录，不是查索引）
- `:218` `DELETE /papers/{paper_id}` → 看删除时**同时**清了什么

三个问题（看完能答就行）：
1. 请求进来后，第一个被调用的函数是谁？
2. `/chat` 的返回值里，除了 answer 还有什么？（提示：evidence）
3. 删除接口里，除了删 PDF，还调用了哪两个函数？

可跳过：装饰器参数、`response_model`、异常转 500 的 try/except。

验证命令：
```powershell
Select-String -Path app\main.py -Pattern '@app\.(get|post|delete)'
```

---

### 第 2 站：`app/agent.py:197` —— RAG 上下文怎么拼

只看 `_rag_context_with_evidence`（约 70 行）：
- 先调 `_search_query`（中文→英文检索词）
- 再调 `retrieve_scored`（拿 4 块 + 分数 + 通道）
- 用 `paper_meta` 插"标题/作者"
- 循环把每块拼成 `【文件 第N页】原文`
- 数值类问题额外补"整页原文"（`_needs_page_text`，`:106`）
- 最后一句是拒答指令

三个问题：
1. 如果检索返回空，这段代码返回什么？（空字符串 + 空列表 → 模型只能答"没找到"）
2. 模型凭什么知道"28.4 是第 8 页"？（因为每块前面有 `【source 第N页】`）
3. 为什么"第六作者"这种问题需要 `paper_meta`？（原文里没有 author/sixth 字样，检索搜不到）

可跳过：`_search_query` 里的缓存与翻译细节、`_merge_terms`。

验证命令：
```powershell
# 直接看拼出来的上下文长什么样（不调模型）
.\.venv\Scripts\python.exe -c "import asyncio; from app.agent import _rag_context; print(asyncio.run(_rag_context('训练用了多少块GPU？','1706.03762v7.pdf'))[:800])"
```

---

### 第 3 站：`app/rag/pipeline.py` —— 切块与检索

只看四个函数：

| 行号 | 函数 | 一句话 |
|---|---|---|
| `:296` | `load_pdf` | 用 pypdf 逐页抽文字，一页一个 Chunk |
| `:515` | `split_text` | 300 字一块、10% 重叠；表格行连表头一起保留 |
| `:268` | `embed` | 词→特征→哈希到 1024 维→归一化（离线兜底向量） |
| `:838` | `retrieve_scored` | 三路召回 → RRF 融合 → 取回原文（每路独立 try/except） |

三个问题：
1. `split_text` 为什么不能"每 300 字切一刀"？（会把句子劈开，检索回来是半句）
2. `retrieve_scored` 里哪一行决定"稠密挂了用哈希顶"？（`_fuse_ids` 里的权重接管）
3. RRF 为什么只比名次不比分数？（三路分数量纲不同，没法直接相加）

可跳过：`_tokens` / `_stem` / `_bm25_ids` 的公式细节、`add_chunks` / `remove_source`。

验证命令：
```powershell
# 看切块结果（块数、每块长度、表头有没有跟着走）
.\.venv\Scripts\python.exe -c "from app.rag.pipeline import load_pdf,split_text; p=[c for c in load_pdf(r'data\papers\1706.03762v7.pdf') if c.page==8][0].text; cs=split_text(p,300); print('块数',len(cs),'最大',max(map(len,cs))); print([c[:60] for c in cs if '28.4' in c])"
```

---

### 第 4 站：`app/papers.py` —— 上传与删除

只看两个函数：
- `:233` `save_paper` → 校验类型/大小 → 算 `paper_id=sha256[:12]` → 落盘 → 查重 → 写 index.json
- `:322` `delete_paper` → 删 PDF + 文本缓存 + 索引条目（**注意：删块由 main.py 调 `remove_source` 完成**）

三个问题：
1. 为什么 `paper_id` 用内容哈希，而不是文件名？（改个名就是另一篇了）
2. 查重比的是什么？（前 3 页文字的 2-gram Jaccard，阈值 0.80）
3. 删除时"删文件"和"删索引"为什么是两处代码？（职责分离，`papers.py` 管文件，`pipeline.py` 管索引）

可跳过：`structure_path` / `load_structures` / `find_paper_by_title`。

---

## 2. 30 分钟版本（今天就能做完）

| 时间 | 做什么 |
|---|---|
| 0–5 分 | 读本文第 0 节的心智模型，能自己画出两条线 |
| 5–12 分 | 读 `main.py:67-88`，回答第 1 站三个问题 |
| 12–22 分 | 读 `agent.py:197-260`，跑一次验证命令，看上下文长什么样 |
| 22–30 分 | 读 `pipeline.py:515-555`，跑切块验证命令，看"28.4"在块里 |

做完这 30 分钟，你已经能对着评委讲清"一次问答是怎么走完的"。

---

## 3. 审核时只问三件事（不要问"这行为什么这么写"）

1. **输入是什么、输出是什么？** 看函数签名 + 最后一行 `return`。
2. **失败会怎样？** 找 `try/except`、找 `if not ...: return`。
3. **有没有证据？** 每个结论都要能跑一条命令复现，不能靠"看起来对"。

---

## 4. 看不懂时的处置（很重要）

| 情况 | 怎么办 |
|---|---|
| 一行太长、看不懂 | 只看函数名 + 参数 + 返回类型，跳过实现 |
| 装饰器一大串 | 全部跳过，它们是框架配置，不影响逻辑 |
| 类型注解 `list[tuple[Chunk, float, list[str]]]` | 读成"返回列表，每项是（块，分数，通道名）" |
| 数学公式（BM25/IDF/RRF） | 只需知道"它在给结果排序"，公式算错会有测试报错 |
| 不认识的名词 | 记在纸上，攒 3 个一起问我，别现场卡住 |

---

## 5. 把问题问准的模板

> "`pipeline.py:838` 的 `retrieve_scored`，第 X 行我看不懂它在干什么。输入是 A，我预期输出是 B，实际是 C。这行是不是在做 D？"

这个模板能让我 3 分钟给你讲清，比"这块看不懂"快十倍。

---

## 6. 审核完成的标志（自检）

- [ ] 能画出"提问 → 检索 → 拼上下文 → 回答"的流程
- [ ] 能说出 4 个关键函数的输入输出
- [ ] 能跑通至少一条验证命令，并解释输出
- [ ] 能说出"哪一步失败会怎样"（检索空 → 拒答）

四条都打勾，你就能自己去答辩了；有一条卡住，把卡住的那条发我。
