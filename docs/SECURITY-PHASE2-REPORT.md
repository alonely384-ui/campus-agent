# 公网安全整改 · 第二阶段验收报告

- 代码基线：commit `8055410` → 本阶段提交 `5c3a6b8`
- 完成日期：2026-11-01
- **未执行生产部署、未修改线上配置、未调用任何生产写接口**

---

## 一、结论

| 项 | 结果 |
|---|---|
| 全量测试 | **540 passed**（原 485 + 新增 55），0 失败 |
| 静态检查 | `ruff check .` → **All checks passed!** |
| 路由归属 | 管理 8 条 / 公开 36 条 / 合计 44 条，**无默认公开的新接口** |
| 业务代码改动 | `app/rag/pipeline.py`、`app/papers.py`、`app/structure.py`、`app/graph.py`、`app/recommend.py`、`app/kb_health.py`、`app/writing.py` **全部零改动** |
| 索引 / 论文 | 未重建索引、未删除论文、未改 RAG/BM25/Chroma 核心逻辑 |
| UI 视觉 | 未做视觉重构（新增登录页沿用 `design-system.css` 变量） |

---

## 二、修改文件清单

### 新增（6 个）

| 文件 | 行数 | 作用 |
|---|---|---|
| `app/security.py` | 719 | 安全层全部逻辑：凭证、会话、IP、限流、预算、体积分档 |
| `tests/conftest.py` | 25 | 测试环境关闭限流（认证/对账/体积照常） |
| `tests/test_security.py` | 571 | 55 条安全测试，12 个分节 |
| `web/admin-login.html` | 134 | 独立管理员登录页 |
| `docs/SECURITY-AUDIT-2026-11-01.md` | 256 | 第一阶段只读审计报告 |
| `docs/SECURITY-PHASE1-PLAN.md` | 797 | 第一阶段方案 + 第十章 5 项设计修订 |

### 修改（11 个）

| 文件 | 改动 |
|---|---|
| `app/main.py` | +403 行安全层：中间件、路由对账、异常处理器、`/admin/login`、`/admin/logout`、8 个管理路由加 `dependencies=[AdminOnly]`、`/health` 收窄 |
| `app/config.py` | +23 行：9 个安全相关配置字段 |
| `app/llm.py` | `chat()`/`chat_stream()` 加 `reserve_llm_call()` 与 `max_tokens` |
| `app/agent.py` | 三处 `completions.create` 前加 `reserve_llm_call()` |
| `app/settings_api.py` | `.env` 绝对路径 → 只给文件名 |
| `tests/test_api.py` | 2 条断言随行为变更更新（`model` 字段、开放文档） |
| `tests/test_index_ops.py` | 删除论文测试改为真实登录 |
| `web/settings.html` | `api()` 捕获 401/503 跳登录页 |
| `web/library.html` | `deletePaper()` 捕获 401/503 跳登录页 |
| `.env.example` | +32 行：9 个安全键占位与说明 |
| `.gitignore` | +4 行：忽略 `data/meta/llm-budget.json` |

---

## 三、逐项实现说明

### 3.1 认证（对应修订 10.1）

`app/security.py`：
- `admin_configured()` 要求 `ADMIN_TOKEN` 与 `ADMIN_COOKIE_SECRET` **都非空**。任一为空 → 管理接口返回 **503**，不是放行。
- `verify_password()` 用 `hmac.compare_digest(sha256(输入), sha256(期望))`，恒定时比较。
- `sign_session()` → `<过期时间戳>.<HMAC-SHA256(secret, 时间戳)>`，服务端无状态，重启不掉线。
- Cookie：`HttpOnly; SameSite=Strict; Path=/; Max-Age=7200`，生产环境额外加 `Secure`。
- `LoginGuard`：同一 IP 连续失败 5 次锁定 15 分钟，第 5 次失败当场返回 **429** + `Retry-After`。

### 3.2 路由对账（防「新接口默认公开」）

`app/main.py` 的 `_assert_route_coverage()` 在 **import 阶段**执行：

- 遍历 `app.routes`，任何路由必须落在 `ADMIN_PATHS`、`PUBLIC_PATHS` 或 `PUBLIC_PREFIXES` 之一
- 未登记 → 直接 `RuntimeError`，**服务起不来**
- 反向也查：登记了却不存在同样报错

服务启动日志实测：
```
安全层路由对账通过：管理 8 条，公开 36 条
```

这不是靠开发者记性，是架构层面的强制约束。将来任何人加新接口忘了归类，CI 里的 `pytest` 会立刻失败。

### 3.3 中间件执行顺序

`security_guard`（`app/main.py`）处理顺序：

1. `/ui/` 静态前缀 → 直接放行
2. `/docs`、`/redoc`、`/openapi.json` → 放行（生产环境这些路由根本没注册，自然 404）
3. 未登记路径 → **404**（不是 401：401 等于告诉对方这里有接口）
4. 管理接口 → 凭证未配置 **503** → 会话无效 **401** → 跨站写请求 **403**
5. 限流 → 超限 **429** + `Retry-After` + `X-RateLimit-Template`
6. 请求体体积 → 超限 **413**

### 3.4 限流（对应修订 10.3）

按路由分 5 档令牌桶，全部在 `app/security.py:_RATE_TABLE`：

| 档位 | 速率 | 突发 | 适用 |
|---|---|---|---|
| `BUCKET_LIGHT` | 2.0/s | 10 | `/health`、`/papers`、静态类 |
| `BUCKET_CHAT` | 0.1/s | 3 | `/chat`、`/chat/stream` |
| `BUCKET_LLM` | 0.2/s | 3 | 翻译、写作、推荐等触发模型的路由 |
| `BUCKET_HEAVY` | 0.02/s | 2 | `/graph/survey`、`/settings/index/health` |
| `BUCKET_CPU` | 0.05/s | 2 | 默认档（**未显式配置的路由落到这里，不是无限**） |

`RateLimiter` 的边界（已写进代码注释与文档）：重启清零、多 worker 不共享、`max_keys=20000` 有界、`idle_seconds=3600` 惰性清理。

### 3.5 每日预算（对应修订 10.3、10.5）

- 统计单位是**真实模型调用次数**，不是 HTTP 请求数
- 每次 `chat()`/`chat_stream()`/`agent.py` 三处直连调用，**调前**扣一次
- 落盘 `data/meta/llm-budget.json`，格式 `{"day": "2026-11-01", "count": 137}`
- 原子写（`.tmp` + `os.replace`）+ `threading.Lock`
- 跨天按本地日期自动清零
- 超额抛 `BudgetExceeded` → 429

**800 不是费用上限**，只是次数闸。费用由「次数 × 输入上限 × 输出上限」的乘法兜住：`message` 2000 字、history 单条 2000 字最多 50 条、`max_tokens=2048`。**未实现 token 级记账与费用预警**，理由与边界见 `docs/SECURITY-PHASE1-PLAN.md` 10.5。

### 3.6 请求体（对应修订 10.4）

- `body_limit_for(path)` 分档：上传类 24MB，其余 1MB
- `Content-Length` 预检超限 → 直接 413，不读 body
- 包装 `request._receive`，按**实际读取字节数**累计兜底 → 分块绕过和谎报长度都拦得住

### 3.7 信息暴露

| 项 | 处置 |
|---|---|
| `/docs`、`/redoc`、`/openapi.json` | `APP_ENV=prod` 时 `docs_url=None`，路由不注册 |
| `GET /health` | 匿名只返回 `{status, version}`；有效 Cookie 才额外给 `model` 与 `budget` |
| `GET /settings` | `.env` 绝对路径 → 只给文件名；API Key 始终 `sk-****abcd` |
| `/settings/about` | 移入 `ADMIN_PATHS`（技术栈清单对竞赛对手有价值） |
| 未捕获异常 | 统一 500「服务器内部错误」，不回堆栈、不回路径 |
| 参数校验失败 | 422，只回字段名与原因，不回显原始输入 |

### 3.8 CSRF 与 CORS

- 管理写接口校验 `Origin`/`Referer` host 必须等于请求 host，不同源 **403**
- 无 Origin 也无 Referer 时放行（便于 curl / 自动化），浏览器跨站表单必带 Origin，不影响防护
- **不添加 CORSMiddleware**（当前就是安全默认值）

### 3.9 前端

- 新增 `web/admin-login.html`：`next` 参数只接受以 `/` 开头且不以 `//` 开头（防开放重定向）
- `settings.html` 与 `library.html` 捕获 401/503 → 跳登录页并带上原页面路径
- 登录页对 503（服务端没配凭证）与 429（锁定中）有专门文案

---

## 四、测试结果

### 4.1 全量与专项

```
$ .venv\Scripts\python.exe -m pytest tests -q
540 passed, 13 warnings in 56.61s

$ .venv\Scripts\python.exe -m pytest tests/test_security.py -q
55 passed, 1 warning in 7.92s

$ .venv\Scripts\python.exe -m ruff check .
All checks passed!
```

### 4.2 新增 55 条安全测试的 12 个分节

| 节 | 覆盖 | 条数 |
|---|---|---|
| 1 | 默认拒绝（缺 token / 缺 secret / 缺任一个） | 4 |
| 2 | 认证与 Cookie（篡改、过期、登出、恒定时比对、锁定） | 8 |
| 3 | 路由归属（全覆盖、总数锁死 44、集合无交集、不泄露路径） | 5 |
| 4 | IP 与代理头（伪造 XFF 无效、可信代理取最后一个值） | 4 |
| 5 | 路径规范化（尾斜杠 / 双斜杠不能绕过、未知路径 404） | 5 |
| 6 | 请求体上限（JSON/上传分档、分块按实际字节） | 5 |
| 7 | 限流器单元（100 线程抢 10 令牌不超发、档位存在性） | 7 |
| 8 | 每日预算（跨实例持久化、跨天重置、50 线程不超发、429） | 6 |
| 9 | 信息暴露（匿名无 model、不泄露堆栈、校验保持 422） | 5 |
| 10 | CSRF（跨站 403、同源放行、无 Origin 放行） | 3 |
| 11 | 生产开关（文档 404、换密钥旧会话失效） | 2 |
| 12 | 上传边界（公开但档位更严） | 1 |

### 4.3 测试暴露出的 3 个真实实现 bug（已修，不是改测试迁就）

1. **`LoginGuard.fail()` 锁定时长算错**：原实现 `since` 在计数变化时被重写，锁定窗口会一直往后推；第 5 次失败仍返回 401 而非 429。已改为 `since` 只在首次失败时记录，跨阈值直接返回满额秒数。
2. **`/chat` 把 `BudgetExceeded` 吞成 500**：`except Exception: raise HTTPException(500)` 先捕获了。已在前面加 `except BudgetExceeded: raise`，让额度耗尽正确映射成 429。
3. **`limited_receive` 无限递归**：`await request.receive()` 取的就是被替换后的 `request._receive`，自己调自己，把 PDF 下载打成 `RecursionError`。已改为先存 `raw_receive` 再调用。

---

## 五、端到端验证（真实 HTTP + 真实浏览器）

在 `127.0.0.1:8013` 起一个配置了 `ADMIN_TOKEN`/`ADMIN_COOKIE_SECRET` 的实例，实测 17 项：

```
--- 未登录 ---
GET /health                                  200  {"status":"ok","version":"0.1.0"}
GET /settings                                401  {"detail":"需要管理员认证"}
GET /settings/index/health                   401  {"detail":"需要管理员认证"}
GET /settings/about                          401  {"detail":"需要管理员认证"}
DELETE /papers/xxx                           401  {"detail":"需要管理员认证"}
GET /papers (公开)                             200  {"total":41,...
GET /settings/ (尾斜杠)                         401  {"detail":"需要管理员认证"}
GET /nonexistent                             404  {"detail":"未找到该接口"}
GET /ui/settings.html                        200  <!DOCTYPE html>...
--- 错误口令 ---
POST /admin/login 错                          401  {"detail":"口令错误"}
--- 正确口令 ---
POST /admin/login 对                          200  {"ok":true,"expires_in":7200}
   Set-Cookie: ca_admin=1793522803.ba637a82...; HttpOnly; Max-Age=7200; ...
GET /settings                                200  {"groups":{"model":[...]}
GET /health 已登录                              200  {"status":"ok","version":"0.1.0","model":"deepseek-chat","budget":{...}}
GET /settings/index/health 已登录               200  {"papers_on_disk":41,...,"consistent":true}
--- CSRF ---
POST /settings/schedule 跨站 Origin            403  {"detail":"跨站请求被拒绝"}
--- 登出 ---
POST /admin/logout                           200  {"ok":true}
GET /settings 登出后                            401  {"detail":"需要管理员认证"}
--- 超大 JSON ---
POST /chat 2MB                               413  {"detail":"请求体超过 1MB"}
```

浏览器实测（Playwright + 系统 Chrome）：

| 场景 | 结果 |
|---|---|
| 未登录访问 `settings.html` | 跳转 `admin-login.html?next=%2Fui%2Fsettings.html` |
| 输错口令 | 停留登录页，提示「口令错误」 |
| 输对口令 | 跳回 `settings.html`，标题正常，`undefined` 出现 0 次 |
| 五个模块 | 模型（Key 掩码 `type=password`，占位「已配置，留空表示不修改」）、RAG 7 个参数值正确、定时「后台运行中 · 下一次 2026-11-02T08:00:00」、知识库「检查完成 · 三路一致」、关于项目正常 |
| `library.html` 点删除 | 跳转 `admin-login.html?next=%2Fui%2Flibrary.html` |
| 控制台错误（排除预期 401） | 0 条 |

---

## 六、未解决的风险

按严重程度排序，全部如实列出：

### 6.1 高：生产服务器当前宕机，且 SSH 进不去

- `http://156.239.2.206:8000` 全部端点 502
- `ssh -o BatchMode=yes root@156.239.2.206` → `Permission denied (publickey,password)`，本机无可用私钥
- **本次整改无法部署**，因为部署前置条件（服务器可用 + 能备份 + 能回滚）不满足
- 这是阻塞项，与代码质量无关

### 6.2 高：仍是纯 HTTP，没有域名与证书

- `ADMIN_TOKEN` 和会话 Cookie 在公网上是明文传输
- 生产环境开了 `Secure` 标记后，**必须走 HTTPS 否则 Cookie 根本不会被发送**，所以上 HTTPS 前不要设 `APP_ENV=prod`
- 没有域名就无法申请 Let's Encrypt 证书

### 6.3 中：单 worker 假设

- 限流与预算的内存态在单进程下正确；若将来加 `--workers`，令牌桶会变成 N 倍配额
- 每日预算因为落盘 + 锁，多进程下会竞争同一文件，`threading.Lock` 不跨进程，**会有少量超发**（不是失控，但计数不准）
- 当前 systemd `ExecStart` 未带 `--workers`，所以不触发

### 6.4 中：没有费用预警

- 800 次是次数闸，不是钱闸
- 若上游涨价或单次上下文膨胀，费用会超预期
- 需要 token 级记账才能精确控制，超出本次范围

### 6.5 低：内存限流器重启清零

- 重启瞬间全员重新领满额度，可被利用但不构成实质风险（攻击者无法控制重启时机）

### 6.6 低：`/papers/upload` 与 `/library/download` 仍公开

- 按你的决策保留公开演示
- 已加体积上限（24MB）、限流档位、`UPLOAD_MAX_BYTES`
- **但没有限制「总入库规模」**：恶意者可以反复上传（受限于限流速率，约 0.02/s 档位下一天最多约 1700 次）把磁盘塞满
- 演示数据与正式知识库**未做隔离**，这是设计留白，需要单独任务

---

## 七、部署步骤（**等你确认后才执行**）

### 7.1 前置（全部满足才能开始）

- [ ] 服务器恢复运行，`ssh root@156.239.2.206` 可登录
- [ ] `git -C /opt/campus-agent log -1` 确认当前 HEAD
- [ ] `cp /root/.env /root/.env.backup-$(date +%Y%m%d-%H%M)` 备份环境变量
- [ ] `git rev-parse HEAD > /root/deploy-rollback-head.txt` 记录回滚点

### 7.2 生成凭证（在服务器上执行，不要在本地生成后上传）

```bash
python3 -c "import secrets; print('ADMIN_TOKEN=' + secrets.token_urlsafe(32))" >> /opt/campus-agent/.env
python3 -c "import secrets; print('ADMIN_COOKIE_SECRET=' + secrets.token_urlsafe(32))" >> /opt/campus-agent/.env
grep -c '^ADMIN_' /opt/campus-agent/.env    # 期望输出 2
```

**注意：不要执行 `cp .env.example .env`**，会覆盖已有的 `LLM_API_KEY`。

### 7.3 拉代码并重启

```bash
cd /opt/campus-agent
git fetch origin && git reset --hard origin/main
git log -1 --format='%h %s'    # 期望 5c3a6b8
systemctl restart campus-agent
sleep 5
systemctl is-active campus-agent
journalctl -u campus-agent -n 20 --no-pager | grep '路由对账'
# 期望：安全层路由对账通过：管理 8 条，公开 36 条
```

### 7.4 验收清单（逐条打勾）

| # | 检查 | 命令 | 期望 |
|---|---|---|---|
| 1 | 服务存活 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/health` | `200` |
| 2 | 公开接口正常 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/papers` | `200` |
| 3 | 管理接口拦住 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/settings` | `401` |
| 4 | 文档关闭 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/docs` | `404` |
| 5 | 匿名无 model | `curl -s localhost:8000/health \| grep -c model` | `0` |
| 6 | 登录可用 | `curl -c /tmp/c.txt -X POST -H 'Content-Type: application/json' -d '{"token":"<TOKEN>"}' localhost:8000/admin/login` | `200` + Set-Cookie |
| 7 | 带 Cookie 读配置 | `curl -b /tmp/c.txt -s -o /dev/null -w "%{http_code}" localhost:8000/settings` | `200` |
| 8 | 首页可访问 | `curl -s -o /dev/null -w "%{http_code}" localhost:8000/ui/index.html` | `200` |
| 9 | 浏览器登录页 | 打开 `/ui/admin-login.html` | 页面正常渲染 |

**注意第 4 项**：只有设置 `APP_ENV=prod` 才会 404。如果暂不上 HTTPS，**保持 `APP_ENV=dev`**，此时 `/docs` 仍可访问（这是有意的：没有 HTTPS 时 Secure Cookie 不会工作，上 prod 反而会让登录失效）。

### 7.5 回滚

| 级别 | 触发 | 操作 | 耗时 |
|---|---|---|---|
| L1 | 管理接口进不去 | 在 `.env` 补 `ADMIN_TOKEN`/`ADMIN_COOKIE_SECRET` 后重启 | 1 分钟 |
| L2 | 限流误伤正常访问 | `.env` 加 `SECURITY_LIMIT_ENABLED=0` 后重启（认证仍在） | 30 秒 |
| L3 | 代码整体有问题 | `git reset --hard $(cat /root/deploy-rollback-head.txt)` + restart | 1 分钟 |
| L4 | 服务起不来 | 从 `/root/.env.backup-*` 恢复 `.env`，回到 L3 | 2 分钟 |

**L1 不是「清空 ADMIN_TOKEN 就放行」**——那个行为在第二阶段已被删除。凭证为空一律 503。

---

## 八、本次未做的事

- ❌ 未部署到生产（前置条件不满足）
- ❌ 未修改线上 `.env`、未动 systemd、未重启线上服务
- ❌ 未调用任何生产写接口
- ❌ 未装 Nginx、未配 HTTPS（无域名）
- ❌ 未做 token 级费用记账
- ❌ 未做演示数据与正式知识库的物理隔离
- ❌ 未改 `app/rag/pipeline.py`、`app/papers.py`、`app/structure.py`、`app/graph.py`、`app/recommend.py`、`app/kb_health.py`、`app/writing.py`
- ❌ 未重建索引、未删除论文
- ❌ 未改 UI 视觉（登录页复用现有设计系统变量）