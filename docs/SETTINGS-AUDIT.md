# 设置中心 · 第一步审计报告

> 范围：只读审计，**未修改任何代码**。
> 审计时间：2026-10-31
> 项目：`D:\alonely\deepseek\campus-agent`

本条结论先行：**后端已有可用能力约七成，但配置持久化通路完全为零**。设置中心能不能做成"真开关"，取决于是否新建一条 `.env` 原子写通路（约 120 行）；如果不动后端，就只能做只读展示页，那样就是一个漂亮的假后台。

---

## 一、现有配置与接口盘点

### 1.1 配置的三个来源（重要）

| 来源 | 载体 | 示例 | 运行时可变 | 持久化 |
|---|---|---|---|---|
| A. pydantic Settings | `.env` → `app/config.py` | `llm_model` `llm_base_url` `llm_api_key` `chroma_dir` `app_port` `log_level` | 可写（对象未 frozen） | **无写接口** |
| B. `os.getenv` 模块级常量 | 进程启动时求值 | `RAG_W_DENSE` `RAG_RERANK_POOL` `GRAPH_FUZZY_ACCEPT` `DISCOVER_SCHEDULE` | **可写，立即生效** | 无 |
| C. 硬编码常量 | 源码里 | `RETRIEVE_TOP_K=4` `DEFAULT_CHUNK_SIZE=300` `MAX_FUZZY...` `DUP_THRESHOLD=0.80` | 不可改（要改源码） | — |

### 1.2 设置项全表（实测位置）

**模型配置**（`app/config.py:15-17`，`.env` 驱动）

| 设置项 | 默认值 | 读取点 | 可动态修改 |
|---|---|---|---|
| `llm_api_key` | `sk-请填写` | `app/llm.py:28,35`；`app/main.py:491,531` | **否**（见 2.1） |
| `llm_base_url` | `https://api.deepseek.com` | `app/llm.py:36` | **否**（客户端缓存） |
| `llm_model` | `deepseek-chat` | `app/llm.py:54,73`；`app/agent.py:634,1488,1621`；`app/translate.py:116,127,135`；`app/main.py:147` | **是**（每次调用现读，改 `settings` 立即生效） |

**RAG 检索参数**（全部是模块级常量，`app/rag/pipeline.py`）

| 环境变量 | 常量 | 默认 | 行为影响 | 可动态修改 |
|---|---|---|---|---|
| `RAG_W_DENSE` | `_W_DENSE`:185 | 2.0 | 稠密通道 RRF 权重 | 是（改模块全局即生效） |
| `RAG_W_HASH` | `_W_HASH`:186 | 0.5 | 哈希通道权重；稠密宕机时它接管 2.0 | 是 |
| `RAG_W_BM25` | `_W_BM25`:187 | 1.0 | BM25 通道权重 | 是 |
| `RAG_RRF_K` | `_RRF_K`:190 | 60 | RRF 平滑常数 | 是 |
| `RAG_RERANK` | `_RERANK_ENABLED`:195 | 1（开） | 是否走 ONNX 交叉编码器重排 | 是 |
| `RAG_RERANK_POOL`:196 | 15 | 重排候选池（实测 10/15/20 → R@1 28/31/30） | 是 |
| `RAG_RECALL_DEPTH` | `_RECALL_DEPTH`:199 | 50 | 召回深度 | 是 |
| `RAG_CITATION_PENALTY` | `_CITATION_PENALTY`:209 | 0.6 | 参考文献块降权系数 | 是 |

**图谱参数**（`app/graph.py`）

| 环境变量 | 常量 | 默认 | 可动态修改 |
|---|---|---|---|
| `GRAPH_FUZZY_ACCEPT` | `FUZZY_ACCEPT`:43 | 0.92 | 否（改后需重建图，见 4.3） |
| `GRAPH_FUZZY_REVIEW` | `FUZZY_REVIEW`:45 | 0.75 | 同上 |
| `GRAPH_ONLINE_ENABLED` | `ONLINE_ENABLED`:685 | 1 | 否（同样只影响建图） |
| `GRAPH_ONLINE_TIMEOUT` | `ONLINE_TIMEOUT`:684 | 20 | 是（建图时现读） |

**其他**

| 环境变量 | 位置 | 默认 | 可动态修改 |
|---|---|---|---|
| `DISCOVER_SCHEDULE` | `app/scheduler.py:26` | daily | **是，且有现成启停接口** |
| `DISCOVER_HOUR` | `app/scheduler.py:27` | 8 | 部分（`_hour()` 现读 env，但已跑的线程按旧值） |
| `RAG_OCR_DIR` / `RAG_OCR_DPI` | `app/ocr.py:15-16` | — | 否（导入时求值） |
| `RAG_RERANK_DIR` | `app/rag/rerank.py:40` | `data/models/...` | 否（模型已加载） |

### 1.3 接口清单（34 个路由，`app/main.py`）

**与设置相关度高的现有接口**：

```
GET  /health                        → {"status","model","version"}，已返回 llm_model
GET  /recommendations/schedule      → scheduler.status()
GET  /recommendations/schedule?enable=true&mode=daily|weekly|off
                                    → 运行时启停定时器（app/main.py:868-873）
GET  /writing/renderers             → {"mermaid":bool,"graphviz":bool,"tikz":bool}
GET  /papers/files                  → 39 个文件名
GET  /papers                        → 论文列表（含 pages/size/duplicate_of）
POST /papers/upload                 → 上传 + 自动查重 + 增量入库
DELETE /papers/{paper_id}           → 删文件 + remove_source 清三路索引
```

**设置相关接口：0 个。** 全库搜 `settings|config|preference` 路由名，无命中。

**知识库管理接口**：已有上传 / 删除 / 列表，**缺少**索引重建、索引健康检查、脏块清理。

---

## 二、哪些能动态改、哪些只能读

### 2.1 模型 Key / Base URL —— 只能读，且是"改了也不生效"的类型

`app/llm.py:24-39`：

```python
_client: AsyncOpenAI | None = None

def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:          # ← 只在这一刻读 settings
        ...
        _client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    return _client
```

**首问之后就固化**。哪怕把 `settings.llm_api_key` 改掉，`get_client()` 返回的还是旧客户端。
全库搜 `reset|_client = None`：**没有任何重置函数**（`app/llm.py` 无 `reset` 符号）。

→ 结论：**Key 与 Base URL 目前做不到热更新**。要做成真开关，必须补一个 `reset_client()`（约 5 行）并在保存后调用。这是唯一一处需要动 `app/llm.py` 的地方。

### 2.2 `llm_model` —— 能热更新（已核实）

`app/llm.py:54` 每次请求都现读 `settings.llm_model`，不缓存。`app/agent.py:634` 等三处同理。
实测：`settings.llm_model = "改动"` 后直接生效。

→ 结论：**是**。可以直接做下拉切换。

### 2.3 RAG 参数 —— 能热更新，但注意副作用

这些是模块级全局，`_fuse_ids()` 里 `dense_w = _W_DENSE ...` 读的就是全局名。重写 `pl._W_DENSE = 5.0` 后下一次检索即生效（已核实源码）。

但有三个副作用必须知道：

1. **IDF 缓存不随参数变**：`_W_*` 只影响融合权重，不改 IDF，安全。
2. **`_RERANK_POOL` 改了立即变**，但重排模型本身仍是懒加载（首次调用约 1-2 秒）。
3. **改完必须落盘到 `.env`**，否则进程重启回滚。当前**无落盘通路**。

### 2.4 图谱阈值 —— 只能读（做了开关也不生效）

`FUZZY_ACCEPT` / `FUZZY_REVIEW` 只在 `match_references()` / `build_graph()` 里用，**图是静态文件 `data/meta/citation-graph.json`**。改阈值不重建图 = 开关完全没效果。

→ 结论：**这是最典型的"假开关"陷阱**。要么放进设置中心并绑定重建图（耗时 1m25s），要么干脆不做。

### 2.5 定时刷新 —— 唯一一个"现成可动态修改"的设置

`app/scheduler.py:96-126` 的 `start(mode)` / `stop()` 就是运行时启停，`/recommendations/schedule?enable=true&mode=` 已经在用。
**但** `schedule_mode()` 只读 `os.environ`，改了不会持久化，重启回到 `.env` 或默认 `daily`。

→ 结论：**运行时可变 ✅，持久化 ❌**。

### 2.6 硬编码常量（务必不要做成开关）

`app/agent.py:92-95` 的 `RETRIEVE_TOP_K=4` / `PER_PAPER_TOP_K=3` / `MULTI_MAX_EVIDENCE=8`，`pipeline.py:66` 的 `DEFAULT_CHUNK_SIZE=300`，`papers.py:36` 的 `DUP_THRESHOLD=0.80`。

这些没有环境变量出口。做成界面开关要么改源码要么 monkey-patch，**都属于"假开关"**，不做。

---

## 三、配置持久化机制审计

### 3.1 现状：零

- 34 个路由无配置写入端点。
- 全库搜 `dotenv|setenv|putenv|.env` 的写操作：**无**。
- `app/config.py` 只有 `SettingsConfigDict(env_file=".env")`，单向读取。
- 前端 `localStorage` 全站只有一处：`web/index.html` 的 `recUser`（推荐反馈），与配置无关。

### 3.2 可借鉴的既有原子写模式（项目里已有两处）

`app/cards.py:123-125`：
```python
tmp = CARDS_PATH.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ...), encoding="utf-8")
Path(tmp).replace(CARDS_PATH)
```

`app/online.py:114-116` 同款（`.json.part` + `.replace`）。

→ **项目已有"临时文件 + 原子替换"的成熟惯例**，设置持久化照抄即可，不需要引入新依赖。

### 3.3 新建 `.env` 写入通路的风险清单

| 风险 | 说明 | 对策 |
|---|---|---|
| 注释与格式丢失 | 若用 `Settings` 反向序列化写回，`.env` 里 19 行注释全没 | **只做"行级替换"**：定位 `KEY=` 行改值，其余字节原样不动 |
| Key 泄漏到日志/响应 | `GET /settings` 若原样返回 `llm_api_key` | 响应只给 `has_key: true` + 末 4 位掩码；永不返回全文 |
| 并发写坏文件 | 两个标签页同时保存 | `threading.Lock` + 临时文件 + `Path.replace` |
| 环境变量优先级高于 `.env` | 实测 `os.environ["APP_PORT"]=9999` 会盖掉 `.env` 的 8000 | 保存后**立即同步写 `os.environ`**，否则重启前后值不一致 |
| 写入后未加载 | pydantic `settings` 是单例，不会自动重读 | 保存后显式重载字段 |
| `.env` 权限 | 实测 `.env` 可写，`git check-ignore` 命中 `.gitignore:10`，未跟踪 | 已有保障，保持 |

### 3.4 安全结论

`.env` 现已确认：
- `git check-ignore -v .env` → `.gitignore:10:.env` ✅
- `git ls-files .env` → 空（未被跟踪）✅
- 文件存在 626 字节，9 个键

→ **持久化落点选 `.env` 是安全的**，但接口层必须做字段白名单 + Key 掩码。

---

## 四、重建索引的风险评估

### 4.1 现有能力

| 能力 | 函数 | 语义 |
|---|---|---|
| 全量建库 | `scripts/build_index.py` → `index_chunks()` `pipeline.py:609` | 遍历 `data/papers/*.pdf` 全部切块後 upsert |
| 增量入库 | `index_paper()` `:825` → `add_chunks()` | 单篇追加，BM25 做 `_merge_bm25` 增量合并 |
| 单篇删除 | `remove_source()` `:792` | Chroma `delete(where={"source":...})` + BM25 `_prune_bm25` |
| 服务内重建 | **不存在** | 只能命令行跑脚本 |

### 4.2 三个真实风险（实测取证）

**风险 1：`scripts/build_index.py` 不做清理，只做 upsert**

脚本 55 行全文无 `delete` / `drop` / `rmtree`（已读全文确认）。`index_chunks()` 只 `upsert`，所以：
- 已删论文的残留块**不会被清掉**
- 换过 `DEFAULT_CHUNK_SIZE` 后旧 id 会残留

**风险 2：实测已存在 851 个脏块（占 3.2%）**

```
总块数 26596
幽灵 source 4 个：1602.07776.pdf(184块) / 1610.08613.pdf(145块)
                  1705.03122.pdf(239块) / 2610.25991.pdf(283块)
幽灵块合计 851（3.2%）
实际有效块 25745
```

这 4 篇在 `data/catalog.json`、`data/meta/structures.json`、磁盘 PDF 里**都已不存在**（三处均 False），但 Chroma 与 BM25 里仍有块，**检索时会被命中**——即"引用了一篇库里已经删掉的论文"。

（注：`docs` 集合 26596 条是本地开发机的数；生产服务器实测 25562 条，说明生产环境相对干净，但机制性风险相同。）

**风险 3：`index_chunks()` 会覆盖全库 IDF/BM25 统计**

`pipeline.py:631-632`：
```python
_save_idf(df, len(chunks))        # 用本批 chunks 重算全库 IDF
_save_bm25(_build_bm25(chunks))   # 用本批 chunks 重建倒排
```
docstring 明写"upsert 有则更新、无则插入，重复建库也安全"，但**它假设 `chunks` 是全集**。如果传入子集（比如只传 3 篇做调试），IDF/BM25 会被降级成 3 篇的统计，**整个库的检索质量塌陷**。

`add_chunks()` docstring（`:741-747`）已明确区分：
```
index_chunks 用"这一批块"重建 IDF 和 BM25（会覆盖全库统计）；
add_chunks 在旧统计上做增量合并，适合"上传一篇新论文"。
```

→ **重建索引操作必须传全集，且必须先停写**。

**风险 4：重建耗时与内存**

生产服务器实测（2 核 4G）：
- 全量重建 39 篇 → `31623 切块 / 25562 入库 / 1786 秒`（约 30 分钟）
- 峰值内存约 876 MB

→ **不能在请求线程里同步跑**，必须后台任务 + 进度轮询。而且 30 分钟内问答会受影响（Chroma 边写边读可能读到半成品）。

### 4.3 图谱重建（另一个独立的重操作）

`scripts/build_graph.py` 全量重建 `data/meta/citation-graph.json`，实测 **1 分 25 秒**，需要联网查 arXiv/Crossref 核对标题。
改 `FUZZY_ACCEPT` / `FUZZY_REVIEW` 后**必须**跑它才生效。

### 4.4 安全重建方案（这是建议，不是已实现）

**绝不做"一键清空重建"。** 推荐"影子索引 + 原子切换"：

1. 新索引建到 `data/chroma.new/`（同时改建 `idf.json` / `bm25.json`）
2. 建完校验：`count` 与切块数对得上、抽 10 条做检索自测
3. 校验通过 → 把 `data/chroma` 改名为 `data/chroma.prev`，`chroma.new` → `chroma`
4. **进程内缓存必须清**：`_BM25=None`、`_IDF.clear()`、`lru_cache` 的 `_page_text_cached.cache_clear()`、`_QUERY_CACHE` 清空
5. 磁盘保留 `chroma.prev` 至少 7 天，给回滚

代价：需要约 2 倍磁盘（当前 288 MB → 576 MB，服务器 25 GB 可用，无压力）。

**更保守的替代**：只做"脏块清理"（`remove_source` 逐个清那 4 个幽灵 source），风险极低、几秒钟完成，能解决当前 3.2% 的实际问题，但不解决"改 chunk 大小"类需求。

---

## 五、最小可行实现方案（MVP）

### 5.1 设计原则落地

| 原则 | 本方案怎么落地 |
|---|---|
| 只实现后端真正支持的 | 见 5.2 的"做/不做"清单 |
| 不做假开关 | 图谱阈值、`RETRIEVE_TOP_K`、`chunk_size` 全部**不做** |
| 不暴露 Key | `has_key` 布尔 + 末 4 位掩码，永不返回全文 |
| 不改 RAG 逻辑 | 只改参数**值**，`retrieve_scored` / `_fuse_ids` 源码一行不动 |
| 保持设计系统 | 新页面只引 `design-system.css` + `chart-colors.js`，复用 `.card` `.btn` `.field` `.tag` 等 |
| 保存有反馈 | 每项保存后 toast + 回显"已完成"；失败显示后端真实错误 |
| 重要操作二次确认 | 重建索引、删论文、改 Key 都要弹确认框，危险按钮红色 |

### 5.2 功能清单（做 / 不做）

**做（后端已支持，真开关）**

| 分组 | 设置项 | 效果 | 生效方式 |
|---|---|---|---|
| 模型 | `llm_model` 下拉切换 | 立即生效 | 改 `settings` + 写 `.env` |
| 模型 | `llm_base_url` | 需重建客户端 | 改 `settings` + `reset_client()`（新增 5 行） |
| 模型 | `llm_api_key` | 需重建客户端 | 同上，输入框 `type=password`，只显示掩码 |
| 模型 | 「测试连接」按钮 | 打一次 `/health` + 一次最小 chat | 结果实时显示 |
| 检索 | `RAG_W_DENSE` / `RAG_W_BM25` / `RAG_W_HASH` | 立即生效 | 改模块全局 + 写 `.env` |
| 检索 | `RAG_RERANK` 开关 | 立即生效 | 同上 |
| 检索 | `RAG_RERANK_POOL` | 立即生效 | 同上 |
| 检索 | `RAG_RECALL_DEPTH` / `RAG_CITATION_PENALTY` / `RAG_RRF_K` | 立即生效 | 同上 |
| 推荐 | `DISCOVER_SCHEDULE` / `DISCOVER_HOUR` | `scheduler.start(mode)` 现成 | 调现有函数 + 写 `.env` |
| 知识库 | 论文列表（39 篇，可删） | 已有 `DELETE /papers/{id}` | 复用 |
| 知识库 | 索引健康检查 | 新接口：总数 / 幽灵块 / 文件-索引一致性 | 只读，无风险 |
| 知识库 | 清理幽灵索引 | 逐篇 `remove_source` | 低风险，秒级 |

**不做（会是假开关或高风险）**

| 设置项 | 为什么不做 |
|---|---|
| `GRAPH_FUZZY_ACCEPT` / `FUZZY_REVIEW` 滑块 | 改完不重建图 = 完全无效；绑定 1.5 分钟重建图则超出 MVP |
| `RETRIEVE_TOP_K` / `PER_PAPER_TOP_K` | 硬编码，无 env 出口 |
| `DEFAULT_CHUNK_SIZE` | 改了要全量重建 30 分钟，且 `index_chunks` 会覆盖全库 IDF |
| 深色模式 / 主题 | 用户明确说本阶段不做 |
| 用户账号 / 权限 | 后端无用户系统 |
| 任意原始 env 编辑 | 会成为改坏服务的安全漏洞 |

### 5.3 建议的实现规模

| 模块 | 改动 | 行数估计 |
|---|---|---|
| `app/config.py` | 加 `set_value(key, value)`：行级替换 `.env` + 原子写 + 同步 `os.environ` | ~70 |
| `app/llm.py` | 加 `reset_client()` | ~5 |
| `app/settings_api.py`（新建） | 白名单 + 校验 + 读/写/测试连接/索引健康/清幽灵 | ~180 |
| `app/main.py` | 挂 6 个路由 | ~40 |
| `web/settings.html`（新建） | 复用设计系统，四个分区卡片 | ~450 |
| `web/index.html` | 导航加「设置」入口 | ~2 |
| `tests/test_settings.py`（新建） | 白名单 / 原子写 / 掩码 / 校验失败 | ~120 |
| **合计** | | **~870 行**，其中后端 ~295 行 |

### 5.4 接口草案（6 个）

```
GET  /settings                     → 所有分组项的当前值（Key 掩码）
POST /settings                     → body: {key, value}，白名单校验，写 .env + 生效
POST /settings/test-connection     → 打一次真实 chat，返回 ok/错误原文
GET  /settings/index/health        → 总块数 / 幽灵块 / 文件-索引差异
POST /settings/index/purge-ghosts  → 只清幽灵 source（秒级）
POST /settings/index/rebuild       → 后台任务 + 进度（可选，第二阶段）
```

### 5.5 需要先定的事（开发前确认）

1. **`reset_client()` 要不要加？** 不加则 Key/BaseURL 保存后必须重启才生效，界面上只能标"需重启"。加则 5 行代码，但动了 `app/llm.py`。
2. **索引重建放不放进 MVP？** 30 分钟 + 2 倍磁盘。建议先只做"健康检查 + 清幽灵块"，重建放第二阶段。
3. **设置中心入口放哪？** 建议 `index.html` 顶栏最右侧一个齿轮入口，新开 `settings.html`，不改现有四个页面的任何样式。
4. **是否需要"恢复默认值"按钮？** 建议要，但只还原单个设置项，不做"一键重置全部"。

---

## 六、审计结论

- **能做的比想象中多**：模型切换、9 个 RAG 参数、定时刷新，后端都已经是"改了就生效"的形态。
- **真正的拦路虎只有一个**：没有任何配置写入通路，`.env` 是只读的。补一条行级原子写通路（~120 行）就能盘活上面全部。
- **最大的假开关陷阱是图谱阈值**：看起来最像"该有个滑块"的参数，恰恰是改了完全没用的。
- **索引重建不是必须现在做**：更该先修的是**已经存在的 851 个脏块**，那是现在就在污染检索结果的真问题，而且清理只需要几秒钟。

等待确认后再开发。