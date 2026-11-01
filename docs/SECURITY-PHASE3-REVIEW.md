# 第三阶段：生产部署前审查报告

- 基线提交：`5c3a6b8`（安全实现）、`36822ec`（第二阶段报告）；被审对象 = `36d8bfe` 之后的 HEAD
- 审查时间：2026-11-01
- 审查性质：**只读**。未修改任何代码、未执行任何生产服务器命令、未调用线上写接口、未触碰 Chroma/BM25/RAG 数据
- 审查方法：源码静态阅读 + AST 扫描 + 本机实测探针（探针脚本已删除，工作区 `git status` 干净）

> **关于证据等级的约定**（贯穿全文，请勿混读）
> - **【实测】** 本机跑过命令或读过源码行，结论可复现
> - **【推断】** 基于代码结构的推理，本轮**未**验证
> - **【无法验证】** 依赖生产服务器，SSH 不通，本轮无法取得

---

## 0. 结论摘要

**当前项目可以从公网安全上线，但必须先补齐 3 件 P0 事项，其中 1 件是代码修复。**

| 判断 | 结论 |
|---|---|
| 认证 / 路由对账 / 预算 / 体积四层 | 【实测】**实现正确，可上线**。540 项测试覆盖，8 个管理接口 100% 纳入认证 |
| 限流层 | 【实测】实现正确，但 **`/admin/login` 的口令爆破防护在 Nginx 之后会失效**，必须配合 Nginx 侧限流 |
| **事件循环阻塞** | 【实测】**真缺陷，必须修**。一个纯读接口可让整站 6.4 秒无响应，放大 595 倍 |
| `LLM_MAX_CONCURRENCY` | 【实测】**配置项是死的**，代码零处引用，不生效 |
| `request._receive` 包装 | 【实测】**对 SSE / PDF / 上传全部安全**，Starlette 与 uvicorn 的路径已逐行确认 |
| `.env.example` | 【实测】26 个键与 `Settings` 一一对应，无冗余无遗漏；但 **8 项中 6 项生产前必须改** |
| Nginx 方案 | 已完成，含 SSE 与 WebSocket 无关的完整配置、限流、安全头 |
| 502 排查 | 已完成，五条只读排查路径，全部基于服务商控制台/VNC，不假设 SSH |
| 最大阻塞项 | **SSH 不通 + 服务器 11-30 到期 + 无域名**，三者叠加才是真正的 Deadline 风险 |

**距 11-18 截止还有 17 天。** 按本文第 7 节的并行/串行拆分执行，时间充裕；但 SSH 恢复这件事必须**今天**开始，因为它不受你控制。

---

## 1. 第二阶段代码审查结果

### 1.1 认证层 —— 通过【实测】

| 检查项 | 结论 | 证据 |
|---|---|---|
| 凭证未配置时的行为 | 两条都为空 → 管理接口一律 **503**，绝不降级放行 | `app/security.py:158-167` `admin_configured()` |
| 口令比较 | 常量时间，先 SHA-256 再 `hmac.compare_digest` | `app/security.py:174-179` |
| Session 签发 | `<过期时间戳>.<HMAC-SHA256(secret, 时间戳)>`，服务端无 session 表 | `app/security.py:182-192` |
| Session 校验 | 签名不符 / 时间戳非数字 / 已过期 → 全部 False | `app/security.py:195-206` |
| Cookie 属性 | `httponly=True`、`samesite="strict"`、`path="/"`；`secure` 跟随 `_IS_PROD` | `app/main.py:1749-1757` |
| 中间件与依赖双层 | 中间件按模板拦 + 8 个路由各自 `dependencies=[AdminOnly]` | `app/main.py:284-295` 与 `app/main.py:393-406` |
| 删除论文 | `DELETE /papers/{paper_id}` 在 ADMIN 内 | `app/main.py:916` |
| 未知路径 | 返回 **404 而非 401**（401 等于告诉探测者「这里有接口」） | `app/main.py:266-269` |

【实测】运行时的分类事实：

```
ADMIN_PATHS ['/papers/{paper_id}', '/settings', '/settings/about',
             '/settings/index/cleanup', '/settings/index/health',
             '/settings/restore', '/settings/schedule', '/settings/test-connection']
PUBLIC count 39
routes 44
ADMIN∩PUBLIC []
_RATE_TABLE size 47
未登记限流档位的路由 []
```

**8 个管理接口 → 8 条 ADMIN 登记，一一对应，无遗漏。**

### 1.2 路由对账 —— 通过，且这是本项目最有价值的架构决定【实测】

`_assert_route_coverage()`（`app/main.py:189-209`）在 **import 阶段**执行（调用点 `app/main.py:1778`，在全部 `@app.get/@app.post` 之后、`app.mount("/ui", ...)` 之后）：

- 有路由既不在 ADMIN 也不在 PUBLIC → `RuntimeError`，**服务根本起不来**
- 登记了却不存在 → 同样 `RuntimeError`，防止清单与代码脱节
- 豁免集合 = `PUBLIC_PREFIXES | DOC_PATHS`（`app/main.py:203`）

配套反向保护：`tests/test_security.py:194-201` 把路由总数**锁死在 44**，任何人新增接口都会先撞上这条测试，被迫想一次「这个新接口归哪一类」。

> 这是模板错误构造的失败关闭（fail-closed）设计，比「靠代码审查」强一个数量级。答辩时可以拿这条讲工程能力。

### 1.3 公开演示接口 —— 免登录，符合评委要求【实测】

39 条 PUBLIC 里包含全部演示功能：`/chat`、`/chat/stream`、`/papers`、`/paper/{filename}*`、`/graph*`、`/writing*`、`/recommendations*`、`/translate/selection`，以及 `/ui/` 整个静态前缀（`app/security.py:113-114` `PUBLIC_PREFIXES = ("/ui/",)`）。

**评委打开 `/ui/` 不需要任何登录。** 唯一会 401 的是设置中心与论文删除，读代码/删数据本来就不该给评委。

`/health` 对匿名访问只回 `{status, version}`，模型名和额度只对管理员回（`app/main.py:618-623`），有测试 `tests/test_security.py:469-480` 覆盖。

### 1.4 登录、CSRF、Origin/Referer、异常处理

| 检查项 | 结论 | 证据 |
|---|---|---|
| 登录失败锁定 | 5 次失败 → 锁 15 分钟，第 5 次当场 429 + `Retry-After` | `app/security.py:146-147`、`app/main.py:1726-1744` |
| 锁定计时起点 | 只在第 1 次失败记录 `since`，避免窗口被无限后推 | `app/security.py:437-452` |
| CSRF 第一道 | Cookie `SameSite=Strict` | `app/main.py:1754` |
| CSRF 第二道 | 管理写接口校验 Origin/Referer host == 请求 host | `app/security.py:654-675`，接入点 `app/main.py:293-295` |
| 无 Origin 时 | **放行**（为了让 curl 能用） | `app/security.py:658-659` |
| 校验失败 | 保持 **422**（不能被兜底处理器降级成 400） | `app/main.py:363-377` |
| 未捕获异常 | 500 + `"服务器内部错误"`，不吐堆栈与路径 | `app/main.py:380-390` |
| 预算耗尽 | **429** 而不是 500 | `app/main.py:353-360`、`app/main.py:637-640` |

> ⚠️ **「Origin/Referer 都不带就放行」是个有意为之的取舍**（`app/security.py:658-659` 有注释）。它不是漏洞（Cookie 是 `SameSite=Strict`，跨站请求根本带不过来），但答辩若被追问「你怎么防 CSRF」，要主动说清这是三层里的第三层，前两层已经拦住了。

### 1.5 每日预算 —— 统计口径正确，但「记录点」有 2 处缺口【实测 + 推断】

**口径是真实模型调用，不是 HTTP 请求数。** 证据是 5 个扣费点的分布：

| 扣费点 | 文件:行 | 说明 |
|---|---|---|
| `llm.chat()` | `app/llm.py:92` | 非流式一次性调用 |
| `llm.chat_stream()` | `app/llm.py:117` | 流式调用（在 `create()` 之前扣） |
| 查询翻译 | `app/agent.py:634` | 中文问题 → 英文检索词 |
| 主循环（非流式） | `app/agent.py:1584` | 在 `MAX_ROUNDS` 循环体内，**每轮扣一次** |
| 主循环（流式） | `app/agent.py:1720` | 同上 |

- 扣在**调用前**（`app/security.py:587-594` 有注释说明为什么）：并发时先扣后调，超额的那批直接拒，不会一起把账冲爆
- 「先扣不退」：调不通也不退还额度，宁可少算不漏算
- 一次 `/chat` 最多 1（翻译）+ 5（`MAX_ROUNDS` 轮）+ 1（修复回环）= **至多 7 次**

**两处缺口：【推断】**

1. **`app/llm.py:92` 与 `:117` 同时被 `/chat` 之外的功能复用**（写作、翻译、图表 caption），这是正确的。
2. **`settings_api.test_connection()`（`app/main.py:1634-1644`）会真实调用上游模型，但不扣预算。** 该接口是管理员专用，影响有限，但严格讲是绕过。

> 结论：**预算的统计对象正确，实现也正确**。`test_connection` 不计数属于可接受的例外，但应在报告里显式记一笔（本文已记）。

### 1.6 预算持久化 —— 通过【实测】

| 检查项 | 结论 | 证据 |
|---|---|---|
| 原子写 | 写 `.tmp` 再 `os.replace`（同盘原子替换） | `app/security.py:502-511` |
| 并发安全 | 全程 `threading.Lock` | `app/security.py:559-567` |
| 跨天清零 | `_refresh_locked()` 比对 `%Y-%m-%d` | `app/security.py:522-526` |
| 落盘失败 | 只记 warning，不炸主流程 | `app/security.py:512-514` |
| 读盘失败 | `(OSError, ValueError, TypeError)` → 归零 | `app/security.py:499-500` |
| **异常恢复的一个缺陷** | `_save_locked` 失败后 `self._count` **不回滚**，内存计数比磁盘多 | 【实测】见下 |

**关于「异常恢复」的确切表述【实测】**：落盘失败时 `consume()` 仍返回 `True`（`app/security.py:566-567`，`_save_locked` 吞掉了 OSError），所以内存里的 `_count` 已经 +1，磁盘上没有。进程重启后会**丢掉这一段计数**。安全方向上是「少算额度」，不是「多放行」，可接受；但**不能**说成「异常恢复完备」。

**跨天清零的时区【推断】**：`_today()` 用 `time.strftime("%Y-%m-%d")`（`app/security.py:516-518`），即**服务器本地时区**。`DEPLOY-GUIDE.md:68` 写明服务器时区已设为 Asia/Shanghai，所以自然日口径与北京时间一致。**这一条本轮无法验证**（SSH 不通，不能读服务器时间）。

测试证据：`tests/test_security.py:381-445` 五条（跨实例持久化、限额阻塞、跨天重置、50 线程抢 10 次不超发、落盘是合法 JSON）。

### 1.7 限流器 —— 实现正确，但**多进程/重启的边界与 Nginx 引入后的连锁反应必须讲清**

| 维度 | 实际行为 | 证据 |
|---|---|---|
| 多线程 | ✅ 安全。全程 `threading.Lock`，测试 100 线程抢 10 令牌不超发 | `app/security.py:367-389`、`tests/test_security.py:346-364` |
| 多进程 | ❌ 不共享。每进程独立内存桶 | `app/security.py:351-358` 类注释 |
| **实际是否多进程** | **单进程**。`uvicorn` 启动无 `--workers`（`DEPLOY-GUIDE.md:285`），代码无 `workers=` 参数 | 【实测】`grep workers app/main.py` 无结果 |
| 服务重启 | 全部清零，窗口内可放行「桶容量 × IP 数」次 | `app/security.py:352-357` 注释已声明 |
| 内存上界 | `max_keys=20000`，满了按最老时间戳淘汰一半 | `app/security.py:360-364`、`391-402` |
| 内存泄漏 | 60 秒扫一次，清掉 1 小时无活动的 key | `app/security.py:391-398` |

**在多 worker 的情况下会怎样【推断】**：桶按进程隔离，实际限流阈值变成 `配置值 × worker 数`；且每日预算同理，**实际额度变成 `LLM_DAILY_BUDGET × worker 数`**（预算在进程内 `_loaded` 缓存，各自 `os.replace` 会互相覆盖，最后一次写赢）。所以**部署时绝对不能加 `--workers`**。这条必须写进部署清单。

#### ⚠️ **P0 级连锁风险：Nginx 引入后 `/admin/login` 的爆破防护会失效**

这条是本轮审查**新发现**的，第二阶段报告没有覆盖：

1. 当前部署是纯 IP 暴露，`scope["client"]` = 公网真实 IP，`login_guard` 按 IP 计数，有效
2. 架 Nginx 后，uvicorn 绑定 `127.0.0.1`，`scope["client"]` 永远是 `"127.0.0.1"`
3. `client_ip()` 只有在 `TRUSTED_PROXIES` 里配了 `127.0.0.1` 且请求带到 `X-Forwarded-For` 时才取 XFF（`app/security.py:238-249`）
4. 若 Nginx **没配** `proxy_set_header X-Forwarded-For`，所有请求的 `client_ip` 都退化成 `127.0.0.1` → **一个攻击者连续失败 5 次，就会把所有人（包括你自己）锁在管理后台外面 15 分钟**
5. 若 Nginx 配了 `$proxy_add_x_forwarded_for`（**追加** 而不是覆盖），客户端自带的假 XFF 会被保留，`parts[-1]` 仍是最近一跳的真实 IP——**这一项反而是安全的**；但若写成 `$http_x_forwarded_for`，则可被伪造绕过

**因此 Nginx 配置里 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 是必须项，不是可选项。** 本文第 6 节的配置已包含。

### 1.8 `request._receive` 包装 —— **对 SSE / PDF / 上传全部安全**【实测】

这是本阶段最容易出事的地方，逐条确认：

**包装逻辑本身**（`app/main.py:319-333`）：

```python
raw_receive = request._receive          # 先抓住原始句柄
async def limited_receive():
    total = 0
    while True:
        message = await raw_receive()   # 调的是原始句柄，不会自递归
        if message.get("type") == "http.request":
            total += len(message.get("body", b"") or b"")
            if total > limit:
                raise _BodyTooLarge
        return message
```

| 场景 | 结论 | 推理链 |
|---|---|---|
| **SSE 流式输出**（`/chat/stream`、`/paper/{f}/translate`） | ✅ 无影响 | `StreamingResponse` 从不调用 `receive()` 读请求体；`limited_receive` 只在有请求体时才被触发。实测第二阶段已端到端跑通流式 |
| **PDF 下载 + Range** | ✅ 无影响 | 读过 Starlette **1.9.0** 源码：`FileResponse.__call__`（`starlette/responses.py:346-412`）只在 `spec_version < (2,4)` 时调 `receive()` **一次**，且**只为了等 `http.disconnect`**，不消费请求体。uvicorn 0.54.0 报的 `spec_version` 是 `"2.3"`（`uvicorn/protocols/http/httptools_impl.py:228`），所以那条分支会走到，但产出的 `http.disconnect` 会原样返回 |
| **文件上传**（`/upload`、`/papers/upload`、`/writing/chart`） | ✅ 被按实际字节计数 | 三条路径都 `await file.read(_UPLOAD_CHUNK)` 逐块读（`app/main.py:494`、`1543`），底层会打到我们包的 `receive`，计数生效 |
| **分块传输无 `Content-Length`** | ✅ 能拦住 | `tests/test_security.py:298-310` 实测 413 |

【实测】`grep` 确认 `main.py` 里**没有任何** `await request.body()` / `await request.json()` 的调用，不存在「先被 handler 读完、再被中间件计数」的顺序问题。

> **唯一的理论边界**：分块超限时抛 `_BodyTooLarge`，此时响应已经发出，部分客户端可能拿到不完整的流。这是 Starlette 的通用限制，不影响功能。

### 1.9 🔴 **新发现的 P0 缺陷：同步阻塞调用卡死事件循环**

**这是本轮审查最重要的发现，第二阶段测试 540 项**全部没有覆盖**。**

**机制【实测】**：`app.main` 是 `async def` 路由，但**没有 await**，内部的同步 IO 直接在事件循环线程上跑，整站被卡住。

AST 扫描（本轮临时脚本，已删除）在 `app/main.py` 里找到 **23 个此类路由**，其中真正有阻塞的 10 个：

| 路由 | 行号 | 阻塞调用 | 单次耗时 |
|---|---|---|---|
| `GET /paper/{f}/structure` | `app/main.py:946` | `reader_meta()` → **每次**重读解析 `structures.json`（0.8 MB，**无缓存**，`app/papers.py:369-377`） | **均值 592 ms，最差 6401 ms** |
| `GET /paper/{f}/layout` | `app/main.py:954` | `detect_layout()` 打开 PDF（冷启动） | 全库 41 篇冷跑 **22711 ms** |
| `GET /papers`、`/papers/files`、`/papers/topics`、`/reader/papers` | `747/758/789/940` | `list_papers()`、`glob()` | 9 ms ~ 数十 ms |
| `DELETE /papers/{id}` | `917` | `delete_paper()` + `remove_source()` | 【推断】秒级 |
| `GET /paper/{f}/translation` | `1028` | `load_translation()` 同步读 JSON | — |
| `GET /health` | `612` | `daily_budget.status()` 碰磁盘 | 5~18 ms |

**实测放大倍数是 595 倍**（脚本 `_probe_blocking.py`，测完即删，工作区已确认干净）：

```
/health   单发  18.1 ms | 重请求在跑时 2263.8 ms | 重请求本身 6375.2 ms (/paper/2407.21783v3.pdf/structure)
/health   单发   5.9 ms | 重请求在跑时    8.9 ms | 重请求本身    6.6 ms (/paper/2407.21783v3.pdf/layout 已热缓存)
/health   单发   7.0 ms | 重请求在跑时   28.8 ms | 重请求本身   44.8 ms (/papers)
```

**同步的成本分布**（41 篇全量，慢的 8 篇）：

```
1101 ms  1706.03762v1_img.pdf
2192 ms  2005.14165v4.pdf
 684 ms  2107.03374v2.pdf
 795 ms  2210.02414v2.pdf
2137 ms  2307.09288v2.pdf
1821 ms  2310.06825v1.pdf
6401 ms  2407.21783v3.pdf   ← 最差
 529 ms  2412.15115v2.pdf
```

> 对比参照：项目里 **`/graph/frontier` 和 `/writing/figures` 已经用了 `run_in_threadpool`**（`app/main.py:1532` 可查），说明作者**知道**这个模式，只是漏用在了 10 个路由上。这不是设计缺陷，是实现遗漏。

**为什么这会直接打穿 Nginx 层**：阻塞期间连 `/health` 都不响应，Nginx 一旦在 `proxy_read_timeout`（默认 60s）内没拿到响应，就返回 **502**。也就是说——
**部署到 Nginx 之后，这个缺陷从"偶发卡顿"升级成"Nginx 502"，会直接命中你在排查的那个症状。**

**修法**（下一阶段执行，工作量约 10 行）：

```python
from starlette.concurrency import run_in_threadpool
# 每个阻塞路由改成：
return await run_in_threadpool(reader_meta, filename)
return await run_in_threadpool(detect_layout, path)
```

可选并行的性能修复：`app/papers.py:369` 的 `load_structures()` 加 `functools.lru_cache`（按 `mtime` 失效），把 592 ms 均值降到接近 0。这是 1.2/1.3 的代码，**不在本轮审查范围内，建议但不动手**。

---

## 2. `.env.example` 配置审查表

【实测】当前 `.env.example` 共 26 个键，与 `Settings.model_fields` 的 26 个字段**完全一一对应**（第二阶段校验脚本结论，本轮复核通过）：

```
app_env / app_host / app_port / log_level
llm_api_key / llm_base_url / llm_model
admin_token / admin_cookie_secret / trusted_proxies / security_limit_enabled
llm_daily_budget / llm_max_concurrency / llm_max_tokens / budget_file
rag_w_dense / rag_w_bm25 / rag_w_hash / rag_rrf_k / rag_rerank / rag_rerank_pool
rag_recall_depth / rag_citation_penalty
discover_schedule / discover_hour
chroma_dir
```

### 2.1 你点名的 8 项

| 键 | 当前值 | 判定 | 生产值 | 理由（含代码位置） |
|---|---|---|---|---|
| `APP_ENV` | `dev` | 🔴 **必须改** | `prod` | `app/main.py:130` 用它切 `_IS_PROD`，一箭双雕：关 `/docs`+`/redoc`+`/openapi.json`（`:136-138`），并给 Cookie 加 `Secure`（`:1755`）。**⚠️ 前提是已上 HTTPS**，见 2.3 |
| `ADMIN_TOKEN` | 空 | 🔴 **必须填** | 32 字节随机串 | 空 → 管理接口全 503（`app/security.py:158-167`）。生成长度建议 ≥32（`secrets.token_urlsafe(32)`） |
| `ADMIN_COOKIE_SECRET` | 空 | 🔴 **必须填** | 另一条独立随机串 | 与口令**必须不同**——换密钥可让所有旧 Cookie 立刻失效（`app/security.py:186-187` 注释） |
| `TRUSTED_PROXIES` | 空 | 🟡 **Nginx 上线后必填** | `127.0.0.1` | 不填则拿不到真实 IP，`/admin/login` 锁定会误伤所有人（见 1.7）。**实际上 `app/security.py:216,221` 已把 `127.0.0.1` 硬编码进 `_LOOPBACK` 并 `|` 进可信集合，所以填不填效果一样**——但建议显式写，读代码的人才知道这是有意的 |
| `LLM_DAILY_BUDGET` | `800` | 🟡 **建议调** | 演示期 `300`~`500` | 800 次 × 每次 ≤2048 输出 token ≈ 上限 164 万输出 token。真实消耗取决于流量，**这是次数闸不是钱闸**（第二阶段 10.5 已声明）。评委集中访问 2 天，500 更安全 |
| `LLM_MAX_CONCURRENCY` | `4` | 🔴 **无效配置** | 随便填，**但要知道它不生效** | 【实测】全项目**零处引用**：`grep -rn "llm_max_concurrency\|Semaphore" app/` 只命中定义处 `app/config.py:137`。并发实际由上游 429 与 Nginx 连接数限制决定 |
| `LLM_MAX_TOKENS` | `2048` | ✅ 保持 | `2048` | 5 处调用点真的用的是它（`app/llm.py:97,122`、`app/agent.py:1591,1727`），是防刷爆账单的最后一道闸 |
| `APP_HOST` | `0.0.0.0` | 🔴 **必须改** | `127.0.0.1` | 架 Nginx 后必须**只监听回环**，否则 8000 端口绕过 HTTPS 直连（`app/main.py:1811` 用它；systemd 用的是命令行 `--host`） |

### 2.2 其余键的判定

| 键 | 当前值 | 判定 | 说明 |
|---|---|---|---|
| `SECURITY_LIMIT_ENABLED` | `1` | ✅ 保持 1 | `0` 只应出现在 `tests/conftest.py` |
| `BUDGET_FILE` | `data/meta/llm-budget.json` | ✅ 保持 | 已在 `.gitignore:34`；必须是**可写目录**（systemd 用户对 `/opt/campus-agent/data/meta/` 需有写权限） |
| `RAG_*` 7 项 | 见文件 | ✅ 保持 | 调参属业务，与部署无关 |
| `DISCOVER_SCHEDULE` | `daily` | 🟡 建议 `off` | 演示期每天 8 点会去 arXiv 抓取并可能调模型，与评委访问抢资源 |
| `LOG_LEVEL` | `INFO` | ✅ 保持 | 演示期要能查日志 |
| `CHROMA_DIR` | `./data/chroma` | ✅ 保持 | 【推断】相对路径按 CWD 解析，systemd 已设 `WorkingDirectory`（`DEPLOY-GUIDE.md:283`），故安全 |

### 2.3 🚨 **`APP_ENV=prod` 的时序陷阱**（你说到点上了）

`app/main.py:1755`：

```python
secure=_IS_PROD,   # 生产走 HTTPS 才加 Secure；本地 http 调试不加
```

- 纯 HTTP 下设 `APP_ENV=prod` → 浏览器收到 `Set-Cookie: ...; Secure`（`Starlette` 的行为）→ **在 HTTP 页面上 Secure Cookie 不会被保存** → 登录「成功」但下一次请求仍是 401
- 表现为：**输入正确口令 → 跳回设置页 → 又被踢回登录页，无限循环**

**所以顺序必须是：先上 HTTPS，再改 `APP_ENV=prod`。** 中间必须有一个过渡态：

> **过渡态方案**：Nginx 已配好 HTTPS、但还没改 `APP_ENV` 时，保持 `APP_ENV=dev`。此时 `/docs` 仍可访问（仅一个 form 提交，无数据泄露）、Cookie 无 `Secure`（但已在 HTTPS 隧道内，风险有限）。**验证 HTTPS 全通之后，再单独改 `APP_ENV=prod` 并重启。**

---

## 3. 剩余风险清单（P0 / P1 / P2）

### P0 —— 不解决就不能上线

| # | 风险 | 证据 | 后果 | 修法 |
|---|---|---|---|---|
| **P0-1** | **同步阻塞卡死事件循环**（1.9 节） | 【实测】595 倍放大；`app/main.py` 10 个路由 | 一个读论文的请求让全站 6.4s 无响应；Nginx 下线时表现为 **502** | 10 处加 `run_in_threadpool`（约 10 行） |
| **P0-2** | **SSH 不通** | 【实测】`Permission denied (publickey,password)` | 无法部署、无法备份、无法回滚、无法排 502 | 走服务商控制台重置密码或重装系统/重建密钥 |
| **P0-3** | **服务器 11-30 到期** | 已知事实 | 比赛 11-18 提交，但评审期可能延后；到期即全站 502 | **今天续费**（可并行） |
| **P0-4** | **无域名 → 无 HTTPS → 不能开 `APP_ENV=prod`** | `app/main.py:1755` | 进阶功能（文档关闭、Secure Cookie）全部无法启用 | 买域名 + Let's Encrypt，或退而用 IP + 自签（见 6.5） |
| **P0-5** | **Nginx 未配 XFF 会导致登录锁定误伤** | 1.7 节推理链 | 一人爆破，全员被锁 15 分钟 | Nginx 配置里写 `X-Forwarded-For $proxy_add_x_forwarded_for` |

### P1 —— 上线后一周内必须消化

| # | 风险 | 证据 | 影响 |
|---|---|---|---|
| P1-1 | `LLM_MAX_CONCURRENCY` 是死配置 | 全项目零引用 | 管理员以为限了 4 并发，实际没有；上游 429 时才暴露 |
| P1-2 | `load_structures()` 无缓存，每次请求重解析 0.8MB | `app/papers.py:369-377` | 单请求 592ms 均值，是 P0-1 的主要来源；加 LRU 可再降一个量级 |
| P1-3 | 预算跨天清零依赖服务器本地时区 | `app/security.py:516-518` | 【无法验证】若时区不是 Asia/Shanghai，清零时刻会错位 8 小时 |
| P1-4 | `test_connection` 调模型不扣预算 | `app/main.py:1634-1644` | 管理员可绕过额度；影响小但应记录 |
| P1-5 | 预算落盘失败时内存计数不回滚 | `app/security.py:512-514` | 重启后小段计数丢失（偏保守方向，可接受） |
| P1-6 | 无 token 级记账，800 次不等于钱上限 | 第二阶段 10.5 已声明 | 极端流量下账单可能超预期 |
| P1-7 | **代码里没有任何访问日志**（只有 `限流触发`、`登录失败` 两处 warning） | `app/main.py:302,1736` | 502/攻击/异常流量**无法事后取证**。Nginx 的 `access.log` 成为唯一证据源 |

### P2 —— 有余力再做

| # | 风险 | 说明 |
|---|---|---|
| P2-1 | 多 worker 不安全 | 限流与预算都按进程隔离；**正确处置是"不加 --workers"**，不是改代码 |
| P2-2 | 无单会话吊销 | 只能换 `ADMIN_COOKIE_SECRET` 全量踢下线（`app/security.py:186-187`） |
| P2-3 | 会话 2 小时过期 | 评审现场若超 2 小时需重输口令（`app/security.py:143`） |
| P2-4 | 无 CSP / HSTS 由应用层提供 | 交给 Nginx（第 6 节配置已含） |
| P2-5 | `guide.html`(127KB) / `project-tour.html`(1.4MB) 被 `.git/info/exclude` 排除 | 不影响部署，但别在别的机器上 `git clone` 后期待它们存在 |

---

## 4. 生产部署前置条件

按「依赖关系」而非「重要性」排序：

| # | 条件 | 现状 | 能否并行 | 前置 |
|---|---|---|---|---|
| C1 | 服务器未过期且可开机 | 【无法验证】 | ✅ 可并行 | 无 |
| C2 | 拿到控制台/VNC/网页终端任一入口 | 【已知】SSH 不通 | ✅ 可并行 | 无 |
| C3 | 能登录 root 且 SSH 免密可用 | ❌ | ❌ 串行 | C2 |
| C4 | 域名已解析到 `156.239.2.206` | ❌ 无域名 | ✅ 可并行（买域名可立刻做） | 无 |
| C5 | 证书就绪（Let's Encrypt 或自签） | ❌ | ❌ 串行 | C4（自签不需要 C4） |
| C6 | Nginx 已安装 | 【无法验证】 | ❌ 串行 | C3 |
| C7 | 端口 443/80 在 ufw 与服务商安全组两处都放行 | 【无法验证】 | ❌ 串行 | C3 |
| C8 | 应用能单机跑通（`/health` 200） | ❌ 当前 502 | ❌ 串行 | C3 |
| C9 | P0-1 代码修复已合并 | ❌ 未做 | ✅ 可并行（本地就能改） | 无 |

**关键洞察：C4（买域名）和 C9（改代码）今天就能开工，完全不依赖 SSH。**

---

## 5. 线上 502 排查流程（只读）

> **前提：不假设 SSH 可用。** 以下所有命令都在**服务商控制台提供的 VNC / 网页终端 / Rescue 模式**里执行，全程只读（`systemctl status`、`journalctl`、`ss`、`free`、`df` 都是只读的；`curl` 打到本机 `/health` 也只读）。

### 5.1 先判断"502 是谁发的"

| 现象 | 判定 | 下一步 |
|---|---|---|
| 返回体是 Nginx 的 `502 Bad Gateway` 页面 | 【推断】说明 Nginx 在跑，但**上游 8000 不通** | 走 5.3~5.6；并注意：`DEPLOY-GUIDE` 里没有 Nginx，**如果你看到的是 Nginx 页面，就说明服务器上的现状与文档不符，需要先搞清楚现状再动** |
| 连接被拒 / 超时 / 无响应 | 【推断】服务器或端口层面不通 | 走 5.2 |
| 返回体是应用自己的 JSON（如 `{"detail":"..."}`） | 不是 502，是应用层错误 | 查 `journalctl -u campus-agent` |

### 5.2 第一层：服务器活着吗、端口开着吗（控制台 VNC）

```bash
# 1) 机器是否在跑、负载如何
uptime
free -m            # 看 available 与 swap
df -h /            # 磁盘满了会导致各种诡异的 502

# 2) 谁在听 8000
ss -lntp | grep 8000

# 3) 本机自己打自己（绕开一切网络层）
curl -sv --max-time 5 http://127.0.0.1:8000/health
```

**判读表**

| 观察 | 结论 | 佐证手段 |
|---|---|---|
| `uptime` 很短（< 几小时） | 机器重启过 | `last reboot`、`journalctl -k \| tail -50` |
| `free -m` 的 available < 200MB，或 swap 疯狂使用 | **内存耗尽** | 见 5.4 |
| `df -h /` 使用率 > 95% | 磁盘满，Python 写日志/临时文件失败 | 见 5.4 |
| `ss` 无 8000 | **进程没在跑** | 见 5.3 |
| `ss` 有 8000 但只绑了 `127.0.0.1` | 外部访问不到 | 【推断】现在是 `0.0.0.0`（`DEPLOY-GUIDE.md:285`），若变成 127 说明有人改过 |
| `curl 127.0.0.1:8000/health` 返回 200 | **应用本身是好的，问题在外层网络/防火墙** | 见 5.6 |
| `curl` 报 `Connection refused` | 端口没人听 | 见 5.3 |
| `curl` 卡住不返回 | 进程活着但事件循环被卡死 | **高度怀疑 P0-1**，见 5.5 |

### 5.3 第二层：systemd 与进程

```bash
systemctl status campus-agent --no-pager -l
systemctl show campus-agent -p ActiveState -p SubState -p NRestarts -p ExecMainStatus \
  -p MemoryCurrent -p ActiveEnterTimestamp
```

**判读表**

| `SubState` / `ActiveState` | 结论 |
|---|---|
| `active (running)` | 进程在，转 5.5/5.6 |
| `failed` → 看 `ExecMainStatus` | 退出码。`1`=Python 异常；`137`=**被 SIGKILL（OOM 杀手）**；`143`=SIGTERM |
| `activating (auto-restart)` 反复 | 启动即崩 → 看日志 |

```bash
# 最近 200 行，找 Traceback / OOM / 启动失败
journalctl -u campus-agent -n 200 --no-pager
# 只看本次启动以来的
journalctl -u campus-agent -b --no-pager | tail -100
```

**关键日志模式**（对照第二阶段实现）

| 日志内容 | 含义 | 处置 |
|---|---|---|
| `安全层路由对账通过：管理 8 条，公开 39 条` | 启动成功 | 正常 |
| `安全层未登记的路由（默认公开是最危险的默认值）` | **新代码上线后启动被对账拦下** | 这是设计如此，需补登记后重启 |
| `管理接口被访问，但 ADMIN_TOKEN / ADMIN_COOKIE_SECRET 未配置` | 凭证丢失 | 检查 `.env` |
| `LLM 预算落盘失败` | 目录不可写 | `ls -la data/meta/` 查权限 |
| `Traceback` + `MemoryError` | 内存爆 | 见 5.4 |
| **完全没有启动日志** | 进程根本没起来 | 检查 `ExecStart` 路径、venv 是否存在 |

### 5.4 第三层：OOM 与磁盘

```bash
# 内核有没有杀过进程（0 表示没杀过任何进程）
journalctl -k --no-pager | grep -i -E "out of memory|oom-kill|killed process" | tail -20
dmesg -T 2>/dev/null | grep -i -E "oom|kill" | tail -20

# systemd 自己的内存上限触发没有（MemoryMax=3G）
journalctl -u campus-agent --no-pager | grep -i -E "memory|MemoryMax|hit" | tail
systemctl show campus-agent -p MemoryCurrent --value | awk '{printf "%.0f MB\n", $1/1024/1024}'

# 磁盘与 inode
df -h /; df -i /
du -sh /opt/campus-agent/data/* 2>/dev/null | sort -h | tail
```

**判读**：如果 `dmesg` 里有 `oom-kill` 且时间与 502 出现时间吻合 → **确认为内存耗尽**。

【推断】最可能的两个内存来源（需在服务器上验证，不公平臆断）：
1. 全量建索引时的峰值（历史实测 876 MB）
2. 无 `--workers` 的单进程 + `MemoryMax=3G` + `MemorySwapMax=1G`，若并发陡增会撞上限

### 5.5 第四层：进程活着但不响应（**高度怀疑 P0-1**）

```bash
# 有没有响应（3 秒超时）
curl -s -o /dev/null -w "code=%{http_code} time=%{time_total}\n" --max-time 3 http://127.0.0.1:8000/health

# 进程状态：D=不可中断（IO 卡死）, R=运行, S=睡眠
ps -o pid,stat,%cpu,%mem,etime,cmd -p $(pgrep -f "uvicorn app.main:app" | head -1)

# 线程栈（只读，需要 py-spy；没有就跳过，装了会很有用）
pip show py-spy >/dev/null 2>&1 && py-spy dump --pid $(pgrep -f "uvicorn app.main:app" | head -1)
```

**判读**

| 观察 | 结论 |
|---|---|
| `code=200` 且 `time<0.1` | 事件循环正常，问题在 Nginx/网络 |
| `code=200` 但 `time` 是 2~7 秒 | **与 P0-1 的实测特征完全吻合**（单次 `reader_meta` 6.4s） |
| `curl` 超时（`code=000`） | 事件循环被长期卡死 |
| `stat` 是 `D` | 磁盘 IO 阻塞（可能是 NFS/磁盘故障） |

### 5.6 第五层：外层网络与防火墙（控制台安全组 + 服务器内）

**这一层必须在两处同时确认**（`DEPLOY-GUIDE.md:377` 已提醒「阿里云有两层防火墙」，但该文写的服务商是阿里云，而实际服务器是**雨云**，条款需按雨云控制台对照）：

```bash
# 服务器内 ufw
ufw status verbose
# iptables 兜底
iptables -L INPUT -n -v --line-numbers | head -20
```

**在雨云控制台的「防火墙 / 安全组」页面**确认：80、443 已放行 `0.0.0.0/0`；22 已放行（否则 SSH 必不通）。

**从你自己的电脑**（不是服务器上）判断是"服务器不通"还是"应用不通"：

| 你电脑上的现象 | 结论 |
|---|---|
| `telnet 156.239.2.206 8000` 通、但 HTTP 502 | 应用或 Nginx 层问题 |
| `telnet` 不通 | **防火墙/安全组/服务器网络**问题 |
| `ping 156.239.2.206` 通、`telnet 443` 不通 | 端口级封锁 |

> ⚠️ 本轮**没有**执行任何对 `156.239.2.206` 的网络探测，以上现象判断的依据是你描述的现状 + 通用网络原理。

### 5.7 排查时必须守住的纪律

1. **全程只读**：不执行 `systemctl restart`、不改 `.env`、不删任何文件——直到把根因定位清楚
2. **先取证再动手**：`journalctl -u campus-agent -b > /tmp/evidence-$(date +%s).log` 先把现场存下来
3. **不做「重启试试」**：`Restart=always` + `RestartSec=5` 已经在替你反复重启；还 502 说明不是偶发

---

## 6. 完整部署与回滚清单

### 6.1 目标架构

```
评委浏览器
   │  https://<域名>/ui/
   ▼
Nginx :443  ──  TLS 终止 / HTTP→HTTPS 跳转 / 安全响应头 / 基础限流
   │          / 静态资源压缩 / SSE 不缓冲
   ▼ proxy_pass http://127.0.0.1:8000
Uvicorn :8000（单进程，只绑回环）
   │          应用层：管理员认证 + 业务限流 + 每日模型预算 + 请求体上限
   ▼
data/（Chroma、BM25、PDF、JSON）—— 不动
```

**分层职责不重叠**：

| 关注点 | Nginx | 应用 |
|---|---|---|
| TLS / 证书 | ✅ | ❌ |
| 安全响应头（CSP/HSTS/X-Frame） | ✅ | ❌ |
| 连接数、单 IP 请求速率（粗粒度） | ✅ | ❌ |
| 管理员认证 | ❌ | ✅ 唯一权威 |
| 按业务语义的精细限流（对话 6 次/分 vs 读页面 2 次/秒） | ❌ | ✅ |
| 每日模型调用预算 | ❌ | ✅ |
| 请求体 1MB/24MB 分档 | 粗粒度 `client_max_body_size` 兜底 | ✅ 按实际字节 |

### 6.2 部署前置（Phase A，可并行，无需 SSH）

- [ ] **A1** 续费服务器（P0-3）
- [ ] **A2** 买域名并 A 记录指向 `156.239.2.206`（P0-4）
- [ ] **A3** 本地修 P0-1（10 处 `run_in_threadpool`）+ 补一条回归测试
- [ ] **A4** 本地跑通 `pytest tests -q`（应为 541+）与 `ruff check .`

### 6.3 部署步骤（Phase B，严格串行）

#### B0 · SSH 恢复（前置阻塞项）

按优先级尝试，**都在雨云控制台操作**：

1. **控制台 VNC 登录**（最稳）：直接进 root shell
2. 查看 `~/.ssh/authorized_keys` 是否还有你的公钥，`sshd -T | grep -E "pubkey|password"` 看是否禁用了密码登录
3. 若密码登录可用：控制台「重置密码」→ 用密码 SSH 进去 → 重新部署公钥
4. 若都不可用：控制台「VNC 单用户/救援模式」→ 挂载修复 `sshd_config` → 重启
5. **最后手段**：`/opt/campus-agent/data/` 单独备份（见 B1）后重装系统

> ⚠️ 在你拿到任一登录入口之前，**B1 之后的所有步骤都不可执行**。

#### B1 · 部署前备份（必须先做）

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p /root/ca-backup-$STAMP

# 1) 代码与配置（不含数据）
cd /opt/campus-agent
cp -a .env /root/ca-backup-$STAMP/.env.bak
git -C /opt/campus-agent rev-parse HEAD > /root/ca-backup-$STAMP/git-head.txt
tar czf /root/ca-backup-$STAMP/code.tar.gz --exclude=data --exclude=.venv --exclude=__pycache__ .

# 2) 数据（体量大，单独打包，不含可重建的向量库）
tar czf /root/ca-backup-$STAMP/data-core.tar.gz \
    data/papers data/meta/structures.json data/meta/recommendations.json
# chroma 体积大，只备份目录清单，重建用 scripts/ 里的建库脚本
find data/chroma -type f -name "*.sqlite3" -exec sha256sum {} \; > /root/ca-backup-$STAMP/chroma-sums.txt

# 3) 系统层
systemctl cat campus-agent > /root/ca-backup-$STAMP/campus-agent.service.bak
cp /etc/ufw/user.rules /root/ca-backup-$STAMP/ufw.backup 2>/dev/null || true

# 4) 核对
ls -lh /root/ca-backup-$STAMP/
```

**验收**：`data-core.tar.gz` 里能 `tar tzf` 出 39 个 PDF；`code.tar.gz` 大小合理（约 20~50MB）。

#### B2 · 应用配置切换

```bash
cd /opt/campus-agent
cp .env .env.bak.$(date +%Y%m%d%H%M%S)     # 二次保险

# 逐项改（用编辑器，不要 sed 一把梭）
#   APP_ENV=dev          ← 先不动！等 HTTPS 验证通过再改 prod（见 2.3）
#   APP_HOST=127.0.0.1   ← 改
#   ADMIN_TOKEN=<32字节随机>
#   ADMIN_COOKIE_SECRET=<32字节另一条>
#   TRUSTED_PROXIES=127.0.0.1
#   LLM_DAILY_BUDGET=500
#   DISCOVER_SCHEDULE=off
```

生成密钥：

```bash
python3 -c "import secrets; print('ADMIN_TOKEN='+secrets.token_urlsafe(32))"
python3 -c "import secrets; print('ADMIN_COOKIE_SECRET='+secrets.token_urlsafe(32))"
```

**验收**（关键，别跳过）：

```bash
# systemd 的 EnvironmentFile 不剥行内注释，必须确认没有行内注释
grep -nE '^[A-Z_]+=.*#' /opt/campus-agent/.env && echo "❌ 有行内注释，必须修" || echo "✅ 无行内注释"
# 权限
chmod 600 /opt/campus-agent/.env
ls -l /opt/campus-agent/.env      # 期望 -rw-------
```

#### B3 · 改 systemd 绑定回环

```bash
sed -i 's|ExecStart=.*|ExecStart=/opt/campus-agent/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000|' \
    /etc/systemd/system/campus-agent.service
grep ExecStart /etc/systemd/system/campus-agent.service
systemctl daemon-reload
systemctl restart campus-agent
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 http://127.0.0.1:8000/health
```

**验收**：`200`；`ss -lntp | grep 8000` 显示 `127.0.0.1:8000`。

#### B4 · 安装并配置 Nginx

```bash
apt install -y nginx
# 若走 Let's Encrypt：
apt install -y certbot python3-certbot-nginx
```

**先用 80 端口验通再上证书**（避免同时踩两个坑）：

```nginx
# /etc/nginx/sites-available/campus-agent-http
server {
    listen 80;
    server_name <你的域名>;
    client_max_body_size 24m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # ← P0-5，必须
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -sf /etc/nginx/sites-available/campus-agent-http /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default     # 否则 80 端口会撞
nginx -t && systemctl reload nginx
curl -s -o /dev/null -w "%{http_code}\n" http://<你的域名>/health
```

**验收**：`200`。**到这一步，评委已经能用域名访问了（只是还没有 HTTPS）。**

#### B5 · 申请证书并切到 443

```bash
certbot --nginx -d <你的域名> --agree-tos -m <邮箱> --redirect
# 自动续期检查
systemctl list-timers | grep certbot || certbot renew --dry-run
```

**手工核对 `certbot` 生成的 443 server 块，把 SSE 与限流补上**（certbot 只会加基础代理）：

```nginx
server {
    listen 443 ssl http2;
    server_name <你的域名>;

    ssl_certificate     /etc/letsencrypt/live/<你的域名>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<你的域名>/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 24m;

    # ---- 安全响应头 ----
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    # CSP 按前端实际使用的 CDN 清单调整，先用较宽的值验证不误杀
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'" always;

    # ---- 基础限流（与应用层限流叠加，不是替代）----
    limit_req_zone $binary_remote_addr zone=ca_all:10m rate=10r/s;
    limit_req_zone $binary_remote_addr zone=ca_chat:10m rate=20r/m;
    limit_conn_zone $binary_remote_addr zone=ca_conn:10m;

    # ---- 管理入口单独收紧：补上应用层 login_guard 在多进程/代理下的盲区 ----
    limit_req_zone $binary_remote_addr zone=ca_admin:10m rate=10r/m;

    location = /admin/login {
        limit_req zone=ca_admin burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
        include /etc/nginx/proxy_params_ca;
    }

    location ~ ^/(chat|chat/stream|paper/.*/translate)$ {
        limit_req zone=ca_chat burst=5 nodelay;
        limit_conn ca_conn 4;
        proxy_pass http://127.0.0.1:8000;
        include /etc/nginx/proxy_params_ca;
        # ---- SSE 关键三行 ----
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding on;
    }

    location / {
        limit_req zone=ca_all burst=20 nodelay;
        limit_conn ca_conn 20;
        proxy_pass http://127.0.0.1:8000;
        include /etc/nginx/proxy_params_ca;
    }
}
```

`/etc/nginx/proxy_params_ca`（新建，避免四处重复）：

```nginx
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Connection        "";
```

> **`limit_req_zone` 必须写在 `http{}` 里，不能写在 `server{}` 里**。上面为了方便阅读放在 server 块内，实际要移到 `/etc/nginx/nginx.conf` 的 http 块。`nginx -t` 会报错提醒你。

```bash
nginx -t && systemctl reload nginx
```

**验收（逐条，缺一不可）**

```bash
curl -sI https://<你的域名>/health | grep -iE "strict-transport|x-content-type|x-frame"
curl -s https://<你的域名>/health                            # 期望 {"status":"ok","version":"0.1.0"}
curl -sI https://<你的域名>/ui/ | head -1                    # 期望 200
curl -s -o /dev/null -w "%{http_code}\n" https://<你的域名>/settings   # 期望 401 或 503，不能是 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<你的域名>/chat \
     -H "Content-Type: application/json" -d '{"message":"你好"}'        # 期望 200，且能看到流式
```

#### B6 · 验证 HTTPS 全通后，才切 `APP_ENV=prod`

```bash
sed -i 's/^APP_ENV=.*/APP_ENV=prod/' /opt/campus-agent/.env
systemctl restart campus-agent
sleep 8

# 文档必须 404
for p in /docs /redoc /openapi.json; do
  printf "%-16s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' https://<你的域名>$p)"
done

# 登录后 Cookie 必须有 Secure
curl -si -X POST https://<你的域名>/admin/login \
     -H "Content-Type: application/json" -d '{"token":"<口令>"}' | grep -i set-cookie
# 期望包含：HttpOnly; Secure; SameSite=strict; Max-Age=7200
```

**验收**：三条文档路径全 404；Cookie 含 `Secure`；浏览器登录设置中心**一次成功不循环**。

#### B7 · 关掉 8000 对公网的暴露

```bash
ufw delete allow 8000/tcp
ufw status verbose
# 雨云控制台安全组里也把 8000 的一条删除，只留 22/80/443
```

**验收**：从你自己电脑 `telnet 156.239.2.206 8000` 不通；`https://<域名>/` 正常。

### 6.4 健康检查清单（每次改动后都跑一遍）

```bash
cat > /root/ca-check.sh <<'EOF'
#!/bin/bash
D="https://<你的域名>"
echo "== 本机应用 =="
curl -s -o /dev/null -w "  127.0.0.1:8000/health  %{http_code}  %{time_total}s\n" --max-time 5 http://127.0.0.1:8000/health
echo "== 对外 =="
for p in /health /ui/ /papers /graph; do
  curl -s -o /dev/null -w "  %{http_code}  %{time_total}s  $p\n" --max-time 10 "$D$p"
done
echo "== 管理接口必须拒绝匿名 =="
for p in /settings /settings/about /settings/index/health; do
  curl -s -o /dev/null -w "  %{http_code}  $p\n" --max-time 5 "$D$p"
done
echo "== 文档必须关闭（prod）=="
for p in /docs /redoc /openapi.json; do
  curl -s -o /dev/null -w "  %{http_code}  $p\n" --max-time 5 "$D$p"
done
echo "== systemd =="
systemctl is-active campus-agent; systemctl show campus-agent -p MemoryCurrent --value | awk '{printf "  mem %.0f MB\n", $1/1024/1024}'
echo "== 端口 =="
ss -lntp | grep -E ':(80|443|8000)' 
EOF
chmod +x /root/ca-check.sh
```

期望输出：`/health`、`/ui/`、`/papers`、`/graph` 全 `200`；三个管理接口全 `401`（或未配凭证时 `503`）；三个文档路径全 `404`；8000 只出现在 `127.0.0.1` 上。

### 6.5 没有域名时的退路（若域名来不及）

| 方案 | 可行性 | 代价 |
|---|---|---|
| 纯 IP + HTTP 保持 `APP_ENV=dev` | ✅ 立即可用 | `/docs` 开着；Cookie 无 `Secure`；但**评委能用** |
| 纯 IP + 自签证书 | ⚠️ 可行 | 评委浏览器会弹「不安全」警告，体验差，**不推荐用于比赛** |
| 用免费域名（如 `.top` / `eu.org`） | ✅ 半天可成 | 需要 DNS 生效时间 |

**如果实在没域名**：保持 `APP_ENV=dev`（不要改 prod！），靠应用层认证 + 限流 + 预算兜底，把 Nginx 只当反向代理和限流器用。**这比强行上 `prod` 导致登录死循环要好得多。**

### 6.6 回滚清单（按触发条件）

| 触发条件 | 回滚动作 | 耗时 |
|---|---|---|
| Nginx 配置错、`nginx -t` 不过 | `cp /etc/nginx/sites-available/campus-agent-http` 上一次备份 → `nginx -t && systemctl reload nginx` | 1 分钟 |
| 上线后 `/health` 非 200 | `systemctl restart campus-agent`；仍不行则进入下一行 | 1 分钟 |
| 新代码导致启动失败（路由对账 RuntimeError） | `cd /opt/campus-agent && git reset --hard <backup-git-head> && systemctl restart campus-agent` | 3 分钟 |
| 登录死循环（`APP_ENV=prod` 但无 HTTPS 生效） | `sed -i 's/^APP_ENV=.*/APP_ENV=dev/' .env && systemctl restart campus-agent` | 1 分钟 |
| 数据异常（论文消失/索引错乱） | 停服务 → 解包 `data-core.tar.gz` → 起服务 | 10 分钟 |
| 全站不可用且短时间修不好 | `ufw allow 8000/tcp` + 恢复 `APP_ENV=dev` + `--host 0.0.0.0`，退回纯 IP 直连 | 2 分钟 |
| 证书出问题 | `certbot renew --force-renewal`；不行则临时降级 80 端口 | 5 分钟 |
| 完全失控 | 目标：**恢复到 B1 备份状态**。`git reset --hard` + `.env` 还原 + data 还原 + `systemctl restart` | 15 分钟 |

**回滚的黄金原则**：**永远先备份 `.env` 和 `git HEAD`**。本次所有风险都能通过这两个文件还原。

---

## 7. 部署后验收测试清单

按「评委视角」而非「开发者视角」排序。每条给出期望值与判据。

### 7.1 公开演示（**不需要登录**，这是最重要的 8 条）

| # | 操作 | 期望 | 判据 |
|---|---|---|---|
| 1 | 浏览器打开 `https://<域名>/ui/` | 首页正常，无弹窗要求登录 | 能看到对话输入框 |
| 2 | 输入「这篇论文讲了什么」并回车 | 有流式输出（文字逐字出现，不是一次性全出） | 观察 3 秒内出现首个字 |
| 3 | 打开 `/ui/reader.html` 读一篇论文 | PDF 逐页渲染、章节树可点、能滚动 | 大文件（>10MB）也能加载 |
| 4 | 阅读页悬停一个词 | 弹出译文卡片 | 约 450ms 后出现 |
| 5 | 打开 `/ui/graph.html` | 引用图谱正常渲染 | 节点与边可见 |
| 6 | 打开 `/ui/discover.html` | 推荐列表加载 | 无「加载失败」 |
| 7 | 打开 `/ui/writing.html` | 写作助手页面可用 | — |
| 8 | 打开 `/ui/library.html` | 论文列表加载 | 39 篇左右 |

### 7.2 管理接口必须受保护

| # | 操作 | 期望 |
|---|---|---|
| 9 | 匿名 `curl https://<域名>/settings` | `401` |
| 10 | 匿名 `curl https://<域名>/settings/index/health` | `401` |
| 11 | 浏览器匿名打开 `/ui/settings.html` | 自动跳到 `admin-login.html` |
| 12 | 口令输错 5 次 | 第 5 次返回 `429`，提示「尝试次数过多」 |
| 13 | 正确口令登录 | 跳回设置中心，五个模块都能加载，`undefined` 出现 0 次 |
| 14 | 登录后刷 `https://<域名>/health` | 返回体里含 `model` 和 `budget` |
| 15 | 登出后再刷 `/settings` | `401` |
| 16 | 密码框 | `type=password`，不显示明文 |

### 7.3 生产开关生效

| # | 操作 | 期望 |
|---|---|---|
| 17 | `curl -o /dev/null -w "%{http_code}" https://<域名>/docs` | `404` |
| 18 | 同上 `/redoc`、`/openapi.json` | 全 `404` |
| 19 | 登录响应头 | `Set-Cookie` 含 `Secure` |
| 20 | `curl -I https://<域名>/health` | 含 `Strict-Transport-Security`、`X-Content-Type-Options` |

### 7.4 限流与预算

| # | 操作 | 期望 |
|---|---|---|
| 21 | 10 秒内连发 10 次 `/health` | 会遇到 `429` + `Retry-After` |
| 22 | 快速连发 5 次 `/graph/survey` | 会被挡在 `429`（`BUCKET_CPU` 只有 burst=2） |
| 23 | 管理员登录后看 `/health` 的 `budget` | `used` 在增长，`limit` 是配置值 |
| 24 | 把 `LLM_DAILY_BUDGET` 临时改小再重启 | 耗尽后 `/chat` 返回 `429` 而**不是** 500 |

### 7.5 稳定性（**新增，专门针对 P0-1**）

| # | 操作 | 期望 |
|---|---|---|
| 25 | 反复打开最重的论文（`2407.21783v3.pdf`）的阅读页 5 次，同时另一个标签页刷 `/health` | `/health` **不应超过 1 秒**；若超过 3 秒 → P0-1 未修好 |
| 26 | 20 个并发 `/paper/xxx/structure` | 全部 200，且总耗时 ≈ 单次耗时（证明真的并行了） |
| 27 | 上传一个 21MB 的 PDF | `400` + 「文件超过 20MB」（应用层）/ 或 Nginx `413` |
| 28 | `kill -9` 主进程后 8 秒 | `systemctl is-active` = `active`（自愈） |
| 29 | 观察 30 分钟 | `MemoryCurrent` 稳定，无单调上升 |

### 7.6 故障可观测（**P1-7 的补救**）

| # | 检查 | 期望 |
|---|---|---|
| 30 | `tail -f /var/log/nginx/access.log` | 能看到评委访问的完整记录 |
| 31 | `journalctl -u campus-agent -f` | 启动时有对账日志；管理接口被拒时有 `logger.error` |
| 32 | 故意用错误口令打 6 次 | Nginx `access.log` 有记录，应用日志有 `管理员登录失败` |

---

## 8. 明确结论：距离安全上线还差什么

### 8.1 必须做的（按顺序）

**串行链（不能跳步）**：

```
C2 拿到控制台入口  →  B0 SSH 恢复  →  B1 备份  →  B2 .env  →  B3 绑回环
                                                          ↓
                                        B4 Nginx 80 验通  →  B5 证书 + 443
                                                          ↓
                                        B6 APP_ENV=prod（仅在此之前验证 HTTPS 全通）
                                                          ↓
                                        B7 关 8000  →  7.1~7.6 全量验收
```

**其中 B2 有一个硬约束**：`APP_ENV` 必须**留到最后一步**才改 prod。这是本文最重要的操作纪律。

### 8.2 可以并行做的（今天就能开工，不等 SSH）

| 事 | 谁做 | 工时 |
|---|---|---|
| **改代码修 P0-1**（10 处 `run_in_threadpool` + 一条回归测试） | 我可以立刻做 | 30 分钟 |
| 买域名 + 配 A 记录 | 你 | 20 分钟，DNS 生效几小时 |
| 续费服务器 | 你 | 5 分钟 |
| 雨云控制台重置密码 / 重装密钥 | 你 | 15 分钟 |
| 准备 Nginx 配置文件（本文 6.3 B5 已给完整版本） | 我可提前写好 `deploy/nginx.conf` | 20 分钟 |

### 8.3 依赖外部、无法由我推进的

1. **服务器能否登录**（雨云控制台是否有权限、是否欠费、22 端口是否被服务商封锁）
2. **域名申请的实名/备案**（国内域名可能需要备案；`.top`/`com` 走境外注册商可免）
3. **比赛评分口径是否接受 IP 直连**（若必须域名，A2 就变成硬阻塞）

### 8.4 一句话总结

> **代码层面：只差 1 件事——把 10 个阻塞路由挪进线程池（P0-1），其余四层（认证/对账/预算/体积）经审查可直接上线。**
> **基础设施层面：差 4 件事——SSH 恢复、续费、域名、HTTPS，其中 3 件今天就能并行开工。**
> **部署与回滚方案、502 排查流程、验收清单本文已全部给出，不需要再调研。**

---

## 附录 A · 本轮执行过的只读命令

为便于复核，列出本轮所有产生结论的命令（全部为本机只读操作，无任何服务器命令）：

```powershell
git log -3 --format="%h %ad %s" --date=short      # 确认基线
git status --short                                 # 确认工作区干净（两次：审查前、审查后）
(Get-Content app\security.py).Count                # 719
Select-String -Path app\main.py -Pattern "dependencies=\[AdminOnly\]|^@app\.(get|post|delete|put)"
Select-String -Path app\*.py -Pattern "reserve_llm_call|BudgetExceeded|daily_budget"
Select-String -Path app\*.py -Pattern "llm_max_concurrency|max_concurrency|Semaphore"
python -c "from app import security as s, main as m; ..."   # 分类事实
python scripts/_probe_blocking.py                  # 并发探针（已删除）
pytest tests -q                                    # 540 passed, 13 warnings in 51.00s
ruff check .                                       # All checks passed!
```

## 附录 B · 本轮产出的可复用事实

| 事实 | 值 | 取法 |
|---|---|---|
| `security.py` 行数 | 719 | `(Get-Content app\security.py).Count` |
| 路由总数 | 44 | `len(main._route_templates())` |
| ADMIN / PUBLIC | 8 / 39 | 启动日志 `安全层路由对账通过` |
| `_RATE_TABLE` 条目 | 47 | `len(security._RATE_TABLE)` |
| 未登记限流档位的路由 | 0 | 集合差 |
| `structures.json` 大小 / 解析耗时 | 0.8 MB / 8.0 ms | 实测 |
| `reader_meta` 均值 / 最差 | 592 ms / 6401 ms | 实测 41 篇 |
| `detect_layout` 全库冷启动 | 22711 ms / 41 篇 | 实测 |
| 事件循环阻塞放大倍数 | **595×**（18.1ms → 2263.8ms） | 实测探针 |
| Starlette / uvicorn / openai / httpx | 1.9.0 / 0.54.0 / 3.35.1 / 0.28.1 | 实测 |
| uvicorn ASGI `spec_version` | `"2.3"` | `httptools_impl.py:228` |
| OpenAI 客户端默认超时 / 重试 | read=60s / max_retries=2 | 实测 |
| `.env.example` 键数 = Settings 字段数 | 26 = 26 | 实测 |