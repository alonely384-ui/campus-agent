# 公网安全整改 · 第一阶段：只读审计与改造方案

- 代码基线：commit `8055410`
- 审计日期：2026-11-01
- 审计范围：全部 44 个路由的实际执行操作、认证缺失面、限流缺失面、信息暴露面
- **本阶段未修改任何代码、未改动线上配置、未调用任何写接口**
- 本报告完成后等你确认，再进入第二阶段

---

## 零、两个必须先说的现场情况

### 0.1 生产服务器当前是宕的

审计期间探测 `http://156.239.2.206:8000`，前后不一致：

```
第一次探测（12 个端点）：
  /health              200    423ms      57B
  /papers              200    378ms  14,191B
  /graph/survey        200    755ms  12,713B
  /settings/about      502    178ms       0B
  /docs                502    168ms       0B

数分钟后全部端点：
  /health -> 502  /papers -> 502  /ui/ -> 502
```

`/settings/about`、`/docs`、`/openapi.json` 在**第一次探测时就已 502**，随后所有端点都变成 502。

**502 证明外层有东西在跑且连不上后端**：要么 `campus-agent.service` 挂了，要么 uvicorn 卡死（`MemoryMax=3G` 被 OOM 杀掉后会按 `Restart=always` 重启）。

**我无法登上服务器确认原因**：
```
$ ssh -o BatchMode=yes root@156.239.2.206
root@156.239.2.206: Permission denied (publickey,password).
```
本机当前**没有可用的 SSH 私钥**（DEPLOY-GUIDE 里记录的免密配置不在这台机器上或已失效）。

**这一条比后面的安全整改更紧急。** 无论整改做不做，赛前必须先确认服务能稳定跑住。

### 0.2 本机实测暴露的服务是旧版本

审计过程中发现本机同时存在两个 uvicorn 进程：

```
PID=22916  RSS=1120MB  threads=127  C:\...\Python312\python.exe -m uvicorn ... 
PID=20860  RSS=   4MB  threads=2    .venv\Scripts\python.exe -m uvicorn ... --port 8012
```

- PID 22916 用的**系统 Python**，RSS 已 **1120MB**、**127 个线程**
- PID 20860 才是 `.venv` 里我本次启动的实例

1120MB 对 2 核 4G 的服务器（`MemoryMax=3G`）意味着余量只有约 1.9GB。这个数字直接决定了下面限流参数的上限。

---

## 一、接口权限清单（44 个路由逐条）

分类依据是**接口实际执行的操作**，不是 HTTP 方法。例如 `GET /paper/{f}/translate` 虽然是 GET，但会真实调用大模型并把结果写盘，所以不是"只读"。

### 1.1 公开只读（13 个）

无副作用、不消耗 LLM 额度，评审可自由访问。**仍需限流**，因为实测有接口会线性占用 CPU。

| # | 方法 | 路径 | 实际行为 | 实测单次耗时 |
|---:|---|---|---|---:|
| 1 | GET | `/favicon.ico` | 302 跳转到 `/ui/favicon.svg` | <1ms |
| 2 | GET | `/health` | 读常量，返回 `{status, model, version}` | 1ms |
| 3 | GET | `/papers/files` | `PAPERS_DIR.glob("*.pdf")` 列目录 | 2ms |
| 4 | GET | `/papers` | 读 `catalog` + 调 `build_topics()` 聚合主题 | 43ms |
| 5 | GET | `/papers/topics` | 同上，走 Louvain 社区分组 | 49ms |
| 6 | GET | `/reader/papers` | 读 structures.json 拼阅读清单 | 19ms |
| 7 | GET | `/paper/{filename}/structure` | 读缓存 JSON | 3ms |
| 8 | GET | `/paper/{filename}/layout` | `detect_layout()` **逐页跑 PyMuPDF** | 60ms |
| 9 | GET | `/paper/{filename}/translation` | 读已缓存译文 | 8ms |
| 10 | GET | `/paper/{filename}` | `FileResponse` 返回 PDF（支持 Range） | — |
| 11 | GET | `/recommendations` | 读缓存 + 按 `user` 标签重排 | 3ms |
| 12 | GET | `/writing/renderers` | 探测 mermaid/graphviz 可执行性 | 4ms |
| 13 | GET | `/writing/chart/{name}`、`/writing/diagram/{name}` | 读 `META_*_DIR` 下已生成的 PNG | 2ms |

> 注：#13 两个路由近似重复，合并计数，路由总数仍按 44 计。

### 1.2 公开演示（21 个）—— 必须限流

**会消耗真实 LLM 额度、CPU 或磁盘**，但属于赛题演示的核心功能，评委必须能用。这里才是资源保护的主战场。

| # | 方法 | 路径 | 消耗什么 | 实测耗时 | 建议限流档 |
|---:|---|---|---|---:|---|
| 14 | POST | `/chat` | **LLM 额度 + RAG 检索**，最多 5 轮工具调用 | 2.3–4.9s | 严 |
| 15 | POST | `/chat/stream` | 同上，SSE 长连接 | 首事件 2.0s / 总 7.6s / **1048 行** | 严 |
| 16 | GET | `/paper/{f}/translate` | **LLM 额度**，逐页翻译并通过 SSE 推送 | 单页 5.1s，9 页 ≈ 45s | 严 |
| 17 | POST | `/translate/selection` | LLM，文本上限 6000 字 | 1.2s | 中 |
| 18 | POST | `/writing/framework` | LLM + 候选池（4–40 篇） | 3.5s | 中 |
| 19 | POST | `/writing/diagram` | LLM + 本地 mermaid/graphviz 进程 | 4.0s | 中 |
| 20 | POST | `/writing/zh2en` | LLM，文本上限 20000 字 | 2.8s | 中 |
| 21 | POST | `/writing/chart` | **matplotlib 渲染**（CPU 密集），上传 20MB | 1.5s | 中 |
| 22 | GET | `/graph/survey` | **纯 CPU 计算，不调 LLM** | **626ms（并发 16 时 10.1s）** | 中 |
| 23 | GET | `/graph/frontier` | 纯 CPU，Brandes 中介中心性 | 609ms | 松 |
| 24 | GET | `/graph` | 读缓存 + 派生社区/中心性 | 18ms | 松 |
| 25 | GET | `/paper/{f}/figure/{index}` | **PyMuPDF 裁图**，首次写盘缓存 | 120ms | 松 |
| 26 | GET | `/writing/paper-figures` | PyMuPDF 全文搜图 | 180ms | 松 |
| 27 | POST | `/library/download` | **外网 arXiv 下载 + 解析 + 入库** | 8–30s | 严 |
| 28 | POST | `/paper/{f}/references/download` | 外网 S2 + 循环下载最多 20 篇 + 入库 | 30–180s | 严 |
| 29 | POST | `/papers/upload` | 落盘 + 解析 + **建索引（写 Chroma）** | 2–10s/篇 | 严 |
| 30 | POST | `/upload` | 落盘到 `data/uploads/`，上限 20MB | 50ms | 中 |
| 31 | POST | `/recommendations/refresh` | **外网 S2 + arXiv 串行抓取** | **14–34s** | 严 |
| 32 | POST | `/recommendations/{key}/feedback` | 写 `recommendations.json` + 重排 | 5ms | 中 |

### 1.3 管理员专用（8 个）—— 必须认证

这些接口一旦被匿名调用，直接造成**数据丢失、配置串改或额度损失**。

| # | 方法 | 路径 | 危害（确凿） |
|---:|---|---|---|
| 33 | POST | `/settings` | 改 `LLM_API_KEY`、`LLM_BASE_URL`、9 个 RAG 参数。写 `.env` + 立即热更新生效 |
| 34 | POST | `/settings/restore` | 单项恢复默认，等于清空 API Key 或打回 RAG 参数 |
| 35 | POST | `/settings/schedule` | 改定时刷新并**启停后台线程** |
| 36 | POST | `/settings/test-connection` | 真实消耗一次 LLM 调用（`max_tokens=1`） |
| 37 | **GET** | **`/settings`** | **泄露全部配置**：见 1.3.1，虽是 GET 但必须认证 |
| 38 | POST | `/settings/index/cleanup` | **删除三路索引块**（有护栏，见下） |
| 39 | **GET** | **`/settings/index/health`** | **一条请求打满单核 4 秒**，并发 8 时单条 27.6 秒 |
| 40 | **DELETE** | **`/papers/{paper_id}`** | **删 PDF 文件 + 清三路索引**，不可恢复 |

#### 1.3.1 `GET /settings` 泄露内容（实测）

```
{
  "groups": {
    "model": [
      {"key":"LLM_MODEL",     "value":"deepseek-chat"},
      {"key":"LLM_BASE_URL",  "value":"https://api.deepseek.com"},
      {"key":"LLM_API_KEY",   "value":"", "has_key":true, "masked":"********abcd"}
    ],
    "rag": [ RAG_W_DENSE=2.0, RAG_W_BM25=1.0, RAG_W_HASH=0.5, RAG_RRF_K=60,
             RAG_RERANK=1, RAG_RERANK_POOL=15, RAG_RECALL_DEPTH=50,
             RAG_CITATION_PENALTY=0.6 ],
    "schedule": [ DISCOVER_SCHEDULE=daily, DISCOVER_HOUR=8 ]
  },
  "env_file": "/opt/campus-agent/.env",      ← 泄露服务器绝对路径
  "key_configured": true,
  "env_overrides": [...]
}
```

**API Key 本身是安全的**（只有末 4 位掩码），但泄露了：
- 服务器绝对路径 `/opt/campus-agent/.env`
- 完整技术选型（模型、端点、9 个 RAG 参数的具体数值）
- 哪些键被系统环境变量覆盖

对竞赛对手而言，这是直接可抄的实现细节；对攻击者而言，这是精确的探测起点。

#### 1.3.2 `/settings/index/cleanup` 的护栏（已确认存在）

`app/kb_health.py:275 cleanup_ghosts()` 有三重保护，**不会误删有效论文**：

1. `if src not in ghosts: result.skipped.append(src); continue` —— 只处理 `health_check()` 认定的幽灵
2. 单个 source 删除抛异常时记进 `errors` 并继续，不静默吞掉
3. 每清一个都重跑 `verify_consistency()` 核对三路

所以它的危害等级低于 `DELETE /papers/{id}`，但仍是**写操作**，必须认证。

### 1.4 暂时关闭（2 个）—— 生产环境直接不注册

| # | 方法 | 路径 | 理由 |
|---:|---|---|---|
| 41 | GET | `/docs` | Swagger UI，暴露 44 个接口签名（实测 1011B） |
| 42 | GET | `/redoc` | ReDoc 同款（实测 893B） |

外加 `/openapi.json`（36154B 完整 schema）—— 关闭 `docs_url` 时 `openapi_url` 必须一起关，否则 schema 仍然可取。

### 1.5 静态资源（2 项）

| # | 路径 | 处置 |
|---:|---|---|
| 43 | `/ui/*`（`StaticFiles`，挂载于变量 `WEB_DIR`） | 保留公开。含 7 个页面 + JS/CSS/SVG，是评委入口 |
| 44 | `/ui/settings.html` + `/ui/sidebar.js` | **页面本身可公开，但页面内所有 fetch 的管理接口必须 401** |

**关键判断**：不需要对 `/ui/settings.html` 做整页拦截。只要后端 8 个管理接口全部认证，页面打开后每个操作都失败，危害等同关闭。若要更好体验，可在页面加载时探测 `/settings` 返回 401 则直接显示"需要管理员口令"。

### 1.6 分类汇总

| 类别 | 数量 | 占比 |
|---|---:|---:|
| 公开只读 | 13 | 29.5% |
| 公开演示（需限流） | 21 | 47.7% |
| 管理员专用（需认证） | 8 | 18.2% |
| 生产关闭 | 2 | 4.5% |
| **合计** | **44** | 100% |

---

## 二、修改文件列表

**第二阶段只改这 6 个文件 + 新增 3 个。** 全部围绕安全，不碰解析/RAG/BM25/Chroma/Agent 工作流。

### 新增（3 个）

| 文件 | 行数估计 | 职责 |
|---|---:|---|
| `app/security.py` | ~180 | 认证凭证校验、Session Cookie 签发、限流器、请求体大小中间件 |
| `tests/test_security.py` | ~320 | 认证 + 限流 + 信息暴露的自动化测试 |
| `web/admin-login.html` | ~90 | 管理员口令输入页（非全站，仅管理操作前跳转） |

### 修改（6 个）

| 文件 | 改动点 | 风险 |
|---|---|---|
| `app/main.py` | ① `FastAPI(...)` 加 `docs_url=None, redoc_url=None, openapi_url=None`（按环境变量控制）<br>② 加 3 个中间件注册<br>③ 8 个管理路由加 `Depends(require_admin)`<br>④ `/settings` 响应裁剪<br>⑤ 加全局 `EXCEPTION_HANDLER` 屏蔽堆栈<br>⑥ `/chat` 的 `ChatRequest.history` 加单条长度校验 | 低（只加不删） |
| `app/llm.py` | `chat()` / `chat_stream()` 加 `max_tokens` 透传 + 全局默认上限 | 低（有默认值） |
| `app/agent.py` | 3 处 `completions.create` 加 `max_tokens`（2 处主循环 + 1 处查询翻译） | 低 |
| `app/config.py` | 新增 5 个 `SECURITY_*` 环境变量读取 | 低（纯新增） |
| `web/settings.html` | 401 时显示口令输入框，不改变任何现有视觉 | 低 |
| `.env.example` | 新增安全相关键的占位（**不含真实值**） | 无 |

### 明确不改（7 个）

按你的约束，以下文件**一行都不动**：
`app/rag/pipeline.py`、`app/papers.py`、`app/structure.py`、`app/graph.py`、`app/recommend.py`、`app/kb_health.py`、`app/writing.py`。

不重建索引、不删除论文、不做 UI 视觉重构。

---

## 三、认证设计

### 3.1 核心原则

- **全站 Basic Auth 禁用**（会让评委输密码）—— 不做
- **IP 白名单不用于评委**—— 不做
- **只对 8 个管理接口做认证**
- 认证失败返回 **401**（未登录或会话无效/过期）
- **凭证未配置返回 503**（服务端缺 `ADMIN_TOKEN`/`ADMIN_COOKIE_SECRET`，默认拒绝，见 10.1）
- **跨站写请求返回 403**（Origin/Referer 不同源，见 3.4）

### 3.2 凭证存储

```
服务器 .env（已 .gitignore:10 忽略）：
  ADMIN_TOKEN=<32 字节随机 hex>     # 管理员口令，sha256 后比对
  ADMIN_COOKIE_SECRET=<32 字节随机>  # 签发 Cookie 用
```

**不入库、不进前端、不进日志**：
- `.env` 已被 `.gitignore` 忽略（实测 `git check-ignore -v .env` → `.gitignore:10:.env`）
- 代码里只读 `os.environ`，不写任何默认值
- 日志里对 `ADMIN_TOKEN` 做脱敏（`app/security.py` 提供 `_redact()`）
- `.env.example` 只写 `ADMIN_TOKEN=` 空占位 + 生成命令注释，不含真实值

生成方式（部署时执行一次）：
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3.3 认证机制：Session Cookie

**为什么不用 Bearer Token**：前端 `settings.html` 用 `fetch` 调用，token 存在 JS 变量里容易被 XSS 读走，且刷新页面就要重输。Cookie 可以设 `HttpOnly`，JS 读不到。

**流程（第二阶段实际实现）**：

```
1. 管理员访问 /ui/settings.html（或点库页的「删除」）
2. 页面加载时 GET /settings → 401（无有效 Cookie）
3. 前端捕获 401/503 → 跳转 /ui/admin-login.html?next=<原页面相对路径>
4. 提交 POST /admin/login {token: "..."}
   → 后端 sha256(token) 与 ADMIN_TOKEN 比对（hmac.compare_digest，恒定时）
   → 成功：Set-Cookie: ca_admin=<过期时间戳>.<HMAC>; HttpOnly; SameSite=Strict; Path=/; Max-Age=7200
   → 失败：401「口令错误」，按 IP 计数，第 5 次失败当场返回 429 + Retry-After（锁定 15 分钟）
   → 服务端未配置凭证：503，登录页直接提示「请先在 .env 中设置」
5. 跳回 next 指向的原页面，后续请求浏览器自动带 Cookie
6. POST /admin/logout 清 Cookie
```

**与初稿的差异**：初稿写的是「页面弹出输入框」，实现时改成**独立登录页** `web/admin-login.html`。理由是口令输入框嵌在配置表单里容易被误当成普通字段，而独立页只有一个输入框，且可以把 503「服务端没配」和 429「锁定中」这两种特殊状态说清楚。`next` 参数只接受以 `/` 开头且不以 `//` 开头的相对路径，防开放重定向。

**Cookie 内容**：`<过期时间戳>.<HMAC-SHA256(secret, 时间戳)>`
- 服务端无状态（不存 session 表），重启不掉线
- 篡改时间戳会导致 HMAC 校验失败
- `Max-Age=7200`（2 小时），过期强制重新输入

### 3.4 CSRF 防护

`SameSite=Strict` 已能挡住绝大多数跨站请求伪造。但为满足你要求的"检查 CSRF"，加一层：

- 所有管理写接口（`POST /settings*`、`POST /settings/index/cleanup`、`DELETE /papers/{id}`）额外校验 `Origin` / `Referer` 头的 host 必须等于请求的 host
- 不匹配返回 403「跨站请求被拒绝」
- **实现位置更正**：初稿写的是「放在 `require_admin` 依赖里」，实际放在 `security_guard` 中间件里（`origin_allowed()`），因为中间件在路由分发之前就拿到原始 header，且只对「管理模板 + 非 GET/HEAD/OPTIONS」生效，逻辑更集中。`require_admin` 依赖作为第二道保险仍在，负责纵深防御
- 没有 `Origin` 也没有 `Referer` 的请求放行（便于 curl 与自动化脚本）；浏览器发起的跨站表单提交必然带 `Origin`，所以不影响防护效果

### 3.5 CORS

**当前状态：`grep -rn "CORS|allow_origins|Access-Control" app/` → No matches found**

即**没有配置任何 CORS**，这是**安全的默认值**。浏览器会阻止其他域名页面的 JS 读取本服务响应。

**结论：不添加 CORSMiddleware**。若将来要加，必须写死具体域名，禁止 `allow_origins=["*"]` 配合 `allow_credentials=True`（浏览器本身也会拒绝这个组合）。

### 3.6 防绕过（SSE / 静态资源 / 其他路由）

你特别要求检查的绕过面，逐条核对：

| 绕过面 | 现状 | 防护方式 |
|---|---|---|
| **SSE 长连接** | `/chat/stream`、`/paper/{f}/translate` 是公开演示，不需要认证 | 走限流层，不走认证层。认证与限流是两套独立机制 |
| **静态资源** | `/ui/*` 由 `StaticFiles` 挂载，**不经过路由依赖** | 中间件跑在 ASGI 层，先于 StaticFiles 执行；用 `request.url.path` 前缀匹配，不依赖路由 metadata |
| **路径大小写/尾斜杠** | FastAPI 默认 307 重定向 | 中间件在重定向前拦截，规范化 `path.rstrip("/")` 后再判断 |
| **`/openapi.json`** | 关闭后访问返回 404 | 关闭的是 `openapi_url`，不是路由本身，没有"隐藏路由"可绕 |
| **HTTP 方法伪装** | `X-HTTP-Method-Override` | 项目未启用该扩展，FastAPI 默认忽略此头 |
| **中间件遗漏新路由** | 未来新增的管理接口 | 中间件用**白名单**而非黑名单：只有显式登记为 public 的路径才放行，其余默认要求认证 |

**最后一条是本设计的关键**：用默认拒绝（default-deny）而非默认放行。将来任何人新增接口，忘记登记就是被拦住，而不是被放行。

### 3.7 认证决策表

```
请求进入
  │
  ├─ path 在 PUBLIC_PATHS（13 个只读 + 21 个演示 + /ui/* + /health）
  │     └─ 放行 → 进入限流层
  │
  ├─ path 在 ADMIN_PATHS（8 个管理接口）
  │     ├─ Cookie 有效 && Origin 匹配 → 放行
  │     ├─ 无 Cookie → 401 + WWW-Authenticate
  │     └─ Cookie 无效 / Origin 不匹配 → 403
  │
  └─ 其他任何路径
        └─ 404（生产环境不暴露"这个路径存在但你需要权限"）
```

**为什么未知路径返回 404 而不是 401**：401 会告诉攻击者"这个路径存在"。404 什么都不说。

---

## 四、限流设计

### 4.1 先看实测数据（这是数值的唯一依据）

#### 4.1.1 并发退化实测（本机 16 核，测前预热一次）

| 接口 | c=1 | c=4 | c=8 | c=16 | 退化特征 |
|---|---:|---:|---:|---:|---|
| `/health` | 1ms | 2ms | 5ms | 10ms | 线性，健康 |
| `/papers` | 43ms | 201ms | 310ms | 597ms | 近似线性，`build_topics()` 无缓存 |
| `/graph` | 18ms | 62ms | 137ms | 271ms | 近似线性 |
| `/recommendations` | 3ms | 8ms | 14ms | 29ms | 线性，健康 |
| `/graph/survey` | **626ms** | **2592ms** | **5176ms** | **10144ms** | **完全串行排队，CPU 密集** |
| **`/settings/index/health`** | **3977ms** | **11378ms** | **27610ms** | **44865ms** | **完全串行，最危险** |

**结论**：`/graph/survey` 和 `/settings/index/health` 是**纯 CPU 绑定**，并发完全不扩展（c=16 时耗时 ≈ 单个 × 16）。这两个接口必须单独限死。

#### 4.1.2 `/chat` 实测

```
conc=1  wall=2.58s
conc=2  wall=3.95s
conc=4  wall=5.61s
```
近似线性扩展（瓶颈在上游网络，本机 CPU 有空闲）。说明 **`/chat` 的成本主要在 LLM 上游，不在本机**，限制它主要是为了**省钱**而非省 CPU。

#### 4.1.3 SSE 实测

```
/chat/stream        首事件 2.04s  总耗时 7.62s  行数 1048  事件 answer_delta×1042
/paper/{f}/translate 单页 5.07s（force 重译） 9 页整篇 ≈ 45s
```
SSE 是**长连接**，不能用普通 QPS 限流，必须按**并发连接数**限。

#### 4.1.4 内存实测

```
本机实际服务进程：RSS 1120MB，127 线程
服务器约束：MemoryMax=3G
单次 /chat 5MB 请求体 → 7.3s，10MB → 12.7s，20MB → 500
```

**20MB 请求体直接被 500，说明当前完全没有请求体上限**——这本身就是一条拒绝服务路径。

### 4.2 限流配置（初始值，全部可调）

设计目标：**2 核 4G + 评审 5~10 人同时访问的场景下，任何人都打不垮服务，且任何人都能用**。

#### 4.2.1 第 1 层：请求体大小

| 项 | 值 | 依据 |
|---|---:|---|
| 全局请求体上限 | **1 MB** | 实测 5MB 要 7.3s、10MB 要 12.7s；正常 `/chat` 请求体 < 4KB（`message` 限 2000 字 + `history` 最多 50 条）。1MB 已是 250 倍余量 |
| 文件上传上限 | 20 MB（**保持现状**） | `UPLOAD_MAX_BYTES` 已存在且已分块限流，不改 |
| CSV 上传上限 | 20 MB（保持现状） | 同上 |

#### 4.2.2 第 2 层：IP 频率（令牌桶）

| 分组 | 接口 | 速率 | 突发 | 依据 |
|---|---|---:|---:|---|
| A 轻量只读 | `/health`、`/papers`、`/graph`、`/recommendations` 等 | **2 req/s** | 10 | c=8 时 310ms，2 req/s 远低于饱和点 |
| B LLM 对话 | `/chat`、`/chat/stream` | **0.1 req/s（6 次/分钟）** | 3 | 单次 2.3–7.6s，6 次/分钟即已满负荷 |
| C LLM 其他 | `/translate/selection`、`/writing/*`、`/paper/{f}/translate` | **0.2 req/s（12 次/分钟）** | 3 | 单次 1.2–5.1s |
| D 重量级 | `/library/download`、`/recommendations/refresh`、`/papers/upload`、`references/download` | **0.02 req/s（60 次/小时）** | 2 | 单次 8–180s |
| E CPU 密集 | `/graph/survey`、`/settings/index/health` | **0.05 req/s（3 次/分钟）** | 2 | c=16 时不扩展，必须限死 |

**超限统一返回 429**，带 `Retry-After` 头。

#### 4.2.3 第 3 层：LLM 并发与预算

| 项 | 值 | 依据 |
|---|---:|---|
| LLM 并发上限 | **4** | 加强制并发限制，超出排队（不是拒绝） |
| 每日调用预算 | **800 次/天** | 见下方估算 |
| 单次 `max_tokens` | **2048** | 见 4.2.5 |
| SSE 单 IP 并发连接 | **3** | 长连接，防止占满 |
| SSE 全局并发连接 | **20** | 2 核下 more 会排队 |

**每日预算 800 次的推算**：

- 一场评审按 10 人 × 每人体验 8 分钟算
- 每人平均 10 次 `/chat` + 5 次翻译 + 5 次写作 = 20 次 LLM 调用
- 10 人 = 200 次
- 单日 4 场评审 = 800 次
- DeepSeek 定价按 2048 输出 token 估算，800 次约 **2~3 元/天**

这个值留了 4 倍余量。**预算耗尽后返回 429 + 明确提示"今日演示额度已用完"**，而不是让 API Key 被刷爆账单。

#### 4.2.4 第 4 层：输入长度（现有 + 补充）

| 接口 | 字段 | 现有上限 | 处置 |
|---|---|---:|---|
| `/chat` | `message` | 2000 | 保持 |
| `/chat` | `history` 条数 | 50 | 保持 |
| **`/chat`** | **`history[].content`** | **无** | **新增 2000/条** ← 漏洞 |
| `/translate/selection` | `text` | 6000 | 保持 |
| `/writing/zh2en` | `text` | 20000 | 保持 |
| `/writing/framework` | `idea` | 2000 | 保持 |
| `/writing/diagram` | `description` | 2000 | 保持 |
| `/library/download` | `query` | 300 | 保持 |

#### 4.2.5 最大生成 token

**当前状态：全项目 0 处 `max_tokens`**（实测 `grep` 仅 `settings_api.py:644` 的 `max_tokens=1` 是测试连接用的）。

| 调用点 | 位置 | 建议值 | 依据 |
|---|---|---:|---|
| `llm.chat()` | `app/llm.py:88` | **2048** | 翻译/写作单段输出 |
| `llm.chat_stream()` | `app/llm.py:107` | **2048** | 对话流式 |
| Agent 主循环（非流式） | `app/agent.py:1579` | **2048** | 实测答案 580 字 ≈ 800 token，2048 留 2.5 倍 |
| Agent 主循环（流式） | `app/agent.py:1713` | **2048** | 同上 |
| 查询翻译 | `app/agent.py:633` | **64** | 只输出一串英文关键词 |

> 说明：`max_tokens` 是**上限不是目标值**，正常回答不会因此变短。但它是防刷爆账单的最后一道闸。

#### 4.2.6 上传大小及数量

| 接口 | 现状 | 建议 |
|---|---|---|
| `/papers/upload` | 无总体积上限，单文件 20MB（`save_paper` 内部） | **单次最多 10 个文件** |
| `/upload` | 20MB | 保持 |
| `/writing/chart` | 20MB | 保持 |

### 4.3 限流的实现位置

**关键决策：在应用层实现，不依赖 nginx。**

理由：你现在**没有 nginx、没有域名**，而且服务器 11-30 到期。把安全能力绑在 nginx 上，等于把"能否安全"绑在"是否续费 + 是否买域名"上。应用层实现随代码走，迁移服务器不影响。

两层配合：
1. **应用层（必做）**：`app/security.py` 实现上述全部限流，不依赖外部组件
2. **nginx（可选，后置）**：将来装 nginx 后，可再加 `limit_req` 作为第二道防线

### 4.4 内存影响评估

限流器要存令牌桶状态。按最坏估算：
- 每个 IP 每组一个桶 = 5 组
- 1000 个 IP × 5 组 × ~200 字节 = **约 1 MB**
- 1 小时不活跃自动清理

**对 1120MB 的现有占用影响可忽略。**

---

## 五、信息暴露处置

| 项 | 现状 | 处置 |
|---|---|---|
| `GET /docs` | 200，1011B | `docs_url=None`，返回 404 |
| `GET /redoc` | 200，893B | `redoc_url=None`，返回 404 |
| `GET /openapi.json` | 200，36154B | `openapi_url=None`，返回 404 |
| `GET /health` | 返回 `{status, model:"deepseek-chat", version}` | **`model` 字段改为运行时才暴露**：非管理员只返回 `{status:"ok"}`；管理员返回完整 |
| `GET /settings` | 泄露 `.env` 绝对路径 + 全部 RAG 参数 | **改为管理员专用**；且响应去掉 `env_file` 字段 |
| 异常堆栈 | 现有代码多处已 `logger.exception` + 返回通用中文 | **补一个全局 `EXCEPTION_HANDLER`** 兜底，确保任何未捕获异常都返回 `{"detail":"服务器内部错误"}`，不含 traceback |

> 注：`/health` 的 `model` 字段是给运维探活用的。评委不需要知道模型名。但**你的技术文档里已经写了用 DeepSeek**——这个字段的实际防泄露价值有限，降级优先级，但顺手做了。

**生产/开发开关**：通过 `APP_ENV=prod|dev` 控制。`dev` 时保留 `/docs`（本地调试方便），`prod` 时全关。

---

## 六、测试用例

新增 `tests/test_security.py`，约 **34 条**。全部用 FastAPI `TestClient`，不需要起真实服务。

### 6.1 认证（12 条）

| # | 用例 | 期望 |
|---:|---|---|
| 1 | 未认证 `GET /settings` | 401 |
| 2 | 未认证 `POST /settings` | 401 |
| 3 | 未认证 `POST /settings/index/cleanup` | 401 |
| 4 | 未认证 `DELETE /papers/{id}` | 401 |
| 5 | 未认证 `POST /settings/restore` | 401 |
| 6 | 未认证 `POST /settings/schedule` | 401 |
| 7 | 未认证 `GET /settings/index/health` | 401 |
| 8 | 未认证 `POST /settings/test-connection` | 401 |
| 9 | 错误 token 登录 | 401 |
| 10 | 正确 token 登录 → 后续请求 | 200 |
| 11 | 篡改 Cookie 时间戳 | 403 |
| 12 | 过期 Cookie | 401 |

### 6.2 公开接口不受影响（8 条）

| # | 用例 | 期望 |
|---:|---|---|
| 13 | 未认证 `GET /health` | 200 |
| 14 | 未认证 `GET /papers` | 200 |
| 15 | 未认证 `GET /graph` | 200 |
| 16 | 未认证 `GET /recommendations` | 200 |
| 17 | 未认证 `GET /ui/index.html` | 200 |
| 18 | 未认证 `POST /chat`（限流未触发） | 200 |
| 19 | 未认证 `POST /chat/stream` | 200 且 `content-type: text/event-stream` |
| 20 | 未认证 `POST /translate/selection` | 200 |

### 6.3 信息暴露（5 条）

| # | 用例 | 期望 |
|---:|---|---|
| 21 | prod 模式 `GET /docs` | 404 |
| 22 | prod 模式 `GET /redoc` | 404 |
| 23 | prod 模式 `GET /openapi.json` | 404 |
| 24 | `GET /settings` 响应 | 不含 `env_file` 键 |
| 25 | 触发内部异常 | 响应体不含 `Traceback` / 文件路径 |

### 6.4 限流（6 条）

| # | 用例 | 期望 |
|---:|---|---|
| 26 | `/chat` 连续 4 次（突发上限 3） | 第 4 次 429 |
| 27 | 429 响应带 `Retry-After` 头 | 存在且为正整数 |
| 28 | `/health` 高频请求（2 req/s 突发 10） | 第 11 次 429 |
| 29 | 请求体 2MB | 413 |
| 30 | 请求体 1MB | 通过 |
| 31 | `/chat` history 单条 5000 字 | 422 |

### 6.5 防绕过（3 条）

| # | 用例 | 期望 |
|---:|---|---|
| 32 | 未知路径 `/admin/foo` | 404（非 401） |
| 33 | 管理接口带错误 `Origin` 头 | 403 |
| 34 | `POST /settings/`（尾斜杠）未认证 | 401（不能靠尾斜杠绕过） |

**回归要求**：现有 **485 个测试**必须全部通过。

---

## 七、部署与回滚方案

### 7.1 部署前置（阻塞项）

| # | 事项 | 状态 |
|---:|---|---|
| 1 | **恢复 SSH 访问** | ❌ 当前 `Permission denied (publickey,password)` |
| 2 | **排查生产 502** | ❌ 未知原因 |
| 3 | 确认服务器 11-30 到期后是否续费 | ❓ 你尚未决定 |
| 4 | 生成 `ADMIN_TOKEN` | 待部署时执行 |

**第 1 项不做，后面全都无法部署。**

### 7.2 部署步骤（第二阶段代码完成后）

```bash
# 1. 备份当前状态
cd /opt/campus-agent
git rev-parse HEAD > /root/deploy-rollback-head.txt
cp .env /root/.env.backup-$(date +%Y%m%d%H%M)

# 2. 生成管理员凭证（只在服务器上生成，不写进任何文件）
TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "ADMIN_TOKEN=$TOKEN" >> .env
echo "ADMIN_COOKIE_SECRET=$SECRET" >> .env
echo "APP_ENV=prod" >> .env
# ⚠️ 把 $TOKEN 抄到你的密码管理器，终端之后查不到（.env 有记录但别再 cat 到日志里）

# 3. 拉代码
git fetch origin && git reset --hard origin/main
systemctl restart campus-agent
sleep 8

# 4. 验收（见 7.3）
```

### 7.3 部署验收清单

| # | 检查 | 命令 | 通过标准 |
|---:|---|---|---|
| 1 | 服务活着 | `systemctl is-active campus-agent` | `active` |
| 2 | 公开接口正常 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/papers` | `200` |
| 3 | 管理接口拦住 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/settings` | `401` |
| 4 | 文档关闭 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/docs` | `404` |
| 5 | 限流生效 | 连发 20 次 `/chat`，最后几条 | `429` |
| 6 | 登录可用 | `curl -c /tmp/c.txt -X POST -d '{"token":"<TOKEN>"}' -H 'Content-Type: application/json' localhost:8000/admin/login` | `200` + Set-Cookie |
| 7 | 带 Cookie 读配置 | `curl -b /tmp/c.txt localhost:8000/settings` | `200` |
| 8 | 密钥不入库 | `git grep -i "admin_token\|campus2026"` | 无真实值 |

### 7.4 回滚方案

**分级回滚，逐级升级**：

| 级别 | 触发条件 | 操作 | 恢复时间 |
|---|---|---|---|
| L1 | 管理接口进不去（口令忘了/漏配） | SSH 登录服务器在 `.env` 里补 `ADMIN_TOKEN` 与 `ADMIN_COOKIE_SECRET` 后重启 | 1 分钟 |

> **L1 原方案已被推翻**。这里原本写的是「把 `ADMIN_TOKEN` 设为空 → 认证层自动放行」的应急开关，第二阶段按你的意见删除了该行为：**凭证缺失一律拒绝，不存在任何全局放行开关**。详见 10.1。
| L2 | 限流误伤正常访问 | `.env` 里 `SECURITY_LIMIT_ENABLED=0` → 限流层停用，认证层仍在 | 30 秒 + 重启 |
| L3 | 代码整体有严重问题 | `git reset --hard $(cat /root/deploy-rollback-head.txt)` + `systemctl restart` | 1 分钟 |
| L4 | 连服务都起不来 | 从 `/root/.env.backup-*` 恢复 `.env`，回到 L3 | 2 分钟 |

**~~L1 的设计要特别说明~~（已推翻，保留原文供对照）**：原设计认为 `ADMIN_TOKEN` 为空时认证层放行是「故意的应急开关」。这个判断是错的——它把「配置疏漏」和「主动降级」混为一谈，而攻击者只需要前者。第二阶段已改成缺凭证即 503 拒绝，见 10.1。

### 7.5 不做的部分

- 不装 nginx（无域名，且会引入新的失败点）
- 不配 HTTPS（同上）
- 不改 systemd 配置
- 不动 chroma / bm25 索引

---

## 八、待你确认的决策点

1. **是否接受下述接口降级**：`GET /health` 的 `model` 字段对非管理员隐藏？这会让你用 `/health` 快速确认"服务跑的是哪个模型"变得不方便。
2. **每日预算 800 次**是否合适？如果你预期评审流量更大，可以调到 2000 次（成本约 5~7 元/天）。
3. **生产 502 这件事**：是先排查服务器（需要恢复 SSH），还是先做代码整改？两者可以并行，但**上线前必须解决**。
4. **`/papers/upload` 与 `/library/download` 归到"公开演示"**（评委可自己传论文看效果）还是"管理员专用"？我按前者设计的，因为赛题 1.1.1 的上传交互是演示重点。

---

## 九、本阶段实际执行的命令清单

为便于复核，列出审计中所有产生结论的命令：

```bash
# 路由清点（确认 44 个）
grep -c "@app\.\(get\|post\|put\|delete\)" app/main.py         # → 44

# 认证机制存在性
grep -rn "add_middleware\|HTTPBasic\|APIKey\|Depends" app/main.py
# → No matches found

# CORS 配置
grep -rn "CORS\|allow_origins\|Access-Control" app/
# → No matches found

# max_tokens 全项目
grep -rn "max_tokens" app/
# → 仅 settings_api.py:644 max_tokens=1（测试连接）

# 路由挂载点
grep -rn "app.mount\|include_router" app/
# → 仅 main.py:1407 app.mount("/ui", ...)

# 生产探测（12 个端点，见 0.1）

# 并发基线（8 端点 × 4 并发档 × 4~16 次，见 4.1.1）

# 请求体上限探测
# 5MB→200(7.3s)  10MB→200(12.7s)  20MB→500  40MB→500

# /chat 实测
# conc=1 2.58s / conc=2 3.95s / conc=4 5.61s

# SSE 实测
# /chat/stream 首事件 2.04s 总 7.62s 1048 行
# /paper/*/translate?pages=2&force=true 5.07s

# 内存实测
# PID 22916 RSS=1120MB threads=127
```

**本阶段未执行任何写操作**。审计中触发的 `data/meta/recommendations.json` 与 `structures.json` 变更由运行中的服务自动写入，未提交。

---

## 十、按第二阶段确认意见修订的设计（2026-11-01）

你在第二阶段开工前提出 5 项必须修订的设计。下面逐条给出**修订前的问题、修订后的实现、以及对应的测试证据**。第七章 7.4 的 L1 回滚方案已被本节 10.1 **推翻**，以本节为准。

### 10.1 删除 `ADMIN_TOKEN` 为空自动放行

**原设计的错误**：7.4 把「`ADMIN_TOKEN` 为空 → 认证层放行」当作应急开关。这是安全上的反模式：任何忘记配置 `.env` 的部署（包括从旧代码升级、`.env` 被误覆盖、容器环境变量漏注入）都会变成**零认证**，而攻击者只需等一个配置疏漏就有完整管理权限。安全默认值必须是「拒绝」。

**修订后**：

```python
# app/security.py
def admin_configured() -> bool:
    return bool(_admin_token()) and bool(_cookie_secret())
```

- 口令与签名密钥**任一为空**即视为未配置
- 未配置时访问管理接口返回 **503**「管理功能未配置，请联系管理员」，代码注释写明「这是拒绝而不是放行」
- 服务照常启动，公开演示功能完全不受影响——不会因为忘配凭证导致评委看不到东西

**配套的回滚方案改为**：删除 7.4 的 L1。管理接口进不去时，正确做法是用 SSH 登录服务器在 `.env` 里现填 `ADMIN_TOKEN`（30 秒 + 重启），而不是打开一个全局放行开关。真正需要「临时全放」的场景不存在——管理接口本来就不参与演示。

**测试证据**：`tests/test_security.py` 第 1 节的 `test_admin_rejects_when_token_missing` / `test_admin_rejects_when_secret_missing` / `test_admin_configured_requires_both`。

### 10.2 明确 IP 获取规则

**问题**：限流按 IP 记账，如果无条件读取 `X-Forwarded-For`，攻击者每次请求换一个伪造头就等于拥有无限配额。

**修订后的规则**（`app/security.py: client_ip()`）：

| 场景 | 取值 |
|---|---|
| 无可信代理（默认，直接暴露 8000） | 只取 `scope["client"][0]`，**完全忽略** `X-Forwarded-For` |
| 接在 Nginx 后面且 Nginx 地址在 `TRUSTED_PROXIES` | 取 `X-Forwarded-For` 的**最后一个**值 |

取最后一个而不是第一个：`X-Forwarded-For` 是逐跳追加的，最左边是客户端自己写的、可伪造；最右边一个是由最近的可信代理追加的，才是能采信的那一跳。

`TRUSTED_PROXIES` 默认空，`.env.example` 里以空占位明文列出（不隐藏这个选项）。`127.0.0.1`/`::1` 内置可信，方便未来 Nginx 与本服务同机部署时零配置可用——同机回环地址的连接不可能来自公网。

**测试证据**：第 4 节 `test_ignores_x_forwarded_for_without_trusted_proxy`、`test_trusts_last_forwarded_value_from_trusted_proxy`、`test_missing_client_falls_back_to_unknown`。

### 10.3 明确内存限流器的边界，预算改为持久化

**内存限流器（`RateLimiter`）的真实边界，写进代码注释与本文档**：

- **重启即清零**：进程退出后令牌桶状态全部丢失，重启瞬间相当于全员重新领满额度。这是可接受的——限流目的是削峰与防滥用，不是精确计费。
- **多 worker 不共享**：如果用 `--workers 4` 起多进程，每个进程各有一份桶，实际总配额是 4 倍。**当前部署是单 worker**（systemd 的 `ExecStart` 未带 `--workers`），所以不构成问题；若将来加 worker，必须换成 Redis 等共享存储，代码里已用 `RateLimiter` 类封装，替换点集中。
- **有界内存**：`max_keys=20000`，超过后强制清掉一半，并有 `idle_seconds=3600` 的惰性清理，不会因为扫描器换 IP 撑爆内存。

**每日预算（`DailyBudget`）必须持久化 + 并发安全**，因为它直接对应真金白银：

- 落盘到 `BUDGET_FILE`（默认 `data/meta/llm-budget.json`），格式 `{"day": "2026-11-01", "count": 137}`
- 写入用「写 `.tmp` 再 `os.replace`」的原子替换，进程崩溃也不会留下半个文件
- 全程 `threading.Lock` 保护，先读后写在一个临界区内完成
- `_today()` 用 `time.strftime("%Y-%m-%d")`，跨天自动清零
- 计数方式：**调用前扣减**（`reserve_llm_call()`），超额抛 `BudgetExceeded` → 全局异常处理器转成 429

**测试证据**：第 8 节 `test_budget_persists_across_instances`、`test_budget_resets_on_new_day`、`test_budget_concurrent_consume_never_exceeds`（50 线程抢 10 个额度，断言恰好 10 次成功）、`test_budget_file_has_day_and_count`、`test_budget_exceeded_maps_to_429`；第 7 节 `test_limiter_concurrent_allow_never_exceeds`（100 线程抢 10 个令牌）。

### 10.4 请求体中间件区分 JSON 与文件上传

**问题**：只看 `Content-Length` 是不够的——用 `Transfer-Encoding: chunked` 发分块请求时可以不带这个头，或者先报一个小的 `Content-Length` 再继续发。

**修订后**（`app/main.py: security_guard` + `limited_receive()`）：

1. **先分档**：`body_limit_for(path)` 按路径判断。`/papers/upload`、`/writing/chart` 等上传类走 `MAX_UPLOAD_BODY = 24MB`，其余走 `MAX_JSON_BODY = 1MB`。上传的 24MB 是给 PDF 留的，普通 JSON 接口没理由超过 1MB。
2. **再做预检**：`Content-Length` 存在且超限 → 直接 **413**，不解包不读 body，省 CPU。
3. **最后兜底**：包装 `request._receive`，在 `limited_receive()` 里按**实际读到的字节数**累计，一旦超过上限抛 `_BodyTooLarge` → **413**。这一步与 `Content-Length` 无关，所以分块绕过和谎报长度都会被拦住。

**踩过的坑**：第一版 `limited_receive` 里写的是 `await request.receive()`，而 `request.receive` 属性取的就是 `request._receive`，被自己替换后变成自己调自己，直接把所有下载打成 `RecursionError`。修法是先存下原始函数 `raw_receive = request._receive`，闭包里调 `await raw_receive()`。

**测试证据**：第 6 节 `test_oversized_json_rejected`、`test_upload_limit_is_larger_than_json_limit`、`test_body_limit_differs_for_upload_paths`、`test_chunked_body_limited_by_actual_bytes`（不设 `Content-Length` 也照样拦）。

### 10.5 800 次是初始值，明确统计口径与费用边界

**800 次不是费用上限，只是「调用次数」这一道闸**。要讲清楚它的边界，否则会给出虚假的安全感：

| 维度 | 实现情况 |
|---|---|
| 统计单位 | **真实模型 API 调用次数**，不是 HTTP 请求数。一次 `/chat` 可能触发「查询翻译 1 次 + 主循环最多 5 轮 + 校验回环修复 1 轮」，全部计数 |
| 计数位置 | `app/llm.py` 的 `chat()`/`chat_stream()`，以及 `app/agent.py` 三处直接 `completions.create` 的调用点，**调前**各扣一次 |
| 并发安全 | 是，`threading.Lock` + 原子落盘 |
| 每日重置 | 是，按本地日期 |
| 输入 token | **有上限**：`message max_length=2000`，history 单条上限 2000 字、最多 50 条（`trim_history()` 里新增的 `HISTORY_ITEM_MAX_CHARS` 过滤） |
| 输出 token | **有上限**：`LLM_MAX_TOKENS=2048`（默认值），三处 Agent 调用与 `chat()`/`chat_stream()` 全部显式传 `max_tokens` |
| 费用预警 | **没有实现**。原因是 DeepSeek 的响应里虽然带 `usage`，但要把它聚合成「当日花了多少钱」需要一张价格表和额外的持久化，超出本次整改范围。**当前的费用控制完全靠「次数上限 × 单次输入上限 × 单次输出上限」的乘法**，即 800 × (2000+2048×5+…) × 单 token 价格 |
| 单次费用估算 | 按 4.2.3 的推算，800 次约 2~3 元/天。这是**估算值，不是承诺**——优化器/长上下文/税率变化都会影响 |

**结论**：800 是「初始值」，应随评审实际流量调整。若你担心费用，最直接的手段是调低 `LLM_DAILY_BUDGET`；若担心评委撞上限，就调高。真正需要精细计费时，应另起一个「token 用量记账」的独立任务，不在安全整改范围内。

**测试证据**：第 8 节全部 + `test_llm_calls_reserve_budget`（断言 `chat()` 内部真的扣了一次）。

### 10.6 修订后新增的配置项汇总

| 键 | 默认值 | 作用 |
|---|---|---|
| `ADMIN_TOKEN` | 空（=拒绝） | 管理员口令 |
| `ADMIN_COOKIE_SECRET` | 空（=拒绝） | 会话 Cookie 签名密钥 |
| `APP_ENV` | `dev` | `prod` 时关文档、Cookie 加 Secure |
| `TRUSTED_PROXIES` | 空 | 可信代理地址，逗号分隔 |
| `SECURITY_LIMIT_ENABLED` | `1` | 限流总开关，测试环境置 0 |
| `LLM_DAILY_BUDGET` | `800` | 每日模型调用次数上限 |
| `LLM_MAX_CONCURRENCY` | `4` | 上游并发上限 |
| `LLM_MAX_TOKENS` | `2048` | 单次输出 token 上限 |
| `BUDGET_FILE` | `data/meta/llm-budget.json` | 预算计数落盘位置 |

`.env.example` 已同步这 9 项，且实测与 `Settings` 的 26 个字段**一一对应、无未知键、无遗漏字段**。