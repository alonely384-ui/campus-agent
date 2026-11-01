# 最小风险代码更新清单（P0-1 + 前端 undefined 修复）

- 编写时间：2026-11-01
- 目标提交：`7887fb8`
- **本文档只做一件事**：把服务器上的代码从旧版本换成 `7887fb8`，不改 Nginx、不加域名、不开 HTTPS、不动 `.env`、不动数据。
- 对比参照：`docs/SECURITY-PHASE3-REVIEW.md` 第 6 节的 B0–B7 是**完整生产化方案**（Nginx + TLS + 分档限流 + 改 `APP_ENV=prod`），步骤多、风险高。本文是它的子集，只覆盖换代码。

> **证据等级约定**
> - **【实测】** 本机跑过命令验证
> - **【推断】** 基于代码推理，未在服务器验证
> - **【待验证】** 需要在服务器上跑才知道

---

## 0. 现状

| 项 | 值 | 证据 |
|---|---|---|
| 远端 git `main` | `7887fb8` | 【实测】`git ls-remote origin main` → `7887fb864a0ba07f66017fb035b430bac58cd89c` |
| 本地 `main` | `7887fb8`，已与远端同步 | 【实测】`git status -sb` → `## main...origin/main` |
| 生产 `/health` | HTTP 200，返回含 `model` 字段 | 【实测】`{"status":"ok","model":"deepseek-chat","version":"0.1.0"}` |
| 生产运行的代码 | **旧版**，早于安全层与 P0-1 | 【实测】新版匿名 `/health` 不返回 `model`；另 `/nonexistent` 返回 FastAPI 英文默认 `{"detail":"Not Found"}`，新版为中文 `{"detail":"未找到该接口"}` |
| SSH | **不通** | 【实测】`Permission denied (publickey,password)` |

**结论：代码已推送到 GitHub，但服务器没有拉取，线上仍是旧代码。**

### 部署前基线（2026-11-01 实测，部署后逐项对照）

本机访问生产必须加 `--noproxy "*"`，否则会被本机网络代理拦成 502——**看到 502 先确认是不是代理问题，不要误判成服务器故障**（这一轮我就差点误判）。

```bash
B=http://156.239.2.206:8000
for p in /health /papers /reader/papers /papers/topics /settings /nonexistent; do
  printf "%-18s -> " "$p"
  curl -s --noproxy "*" -o /dev/null -w "%{http_code} %{size_download}\n" --max-time 15 "$B$p"
done
```

| 路径 | 部署前（旧代码，实测） | 部署后应为 |
|---|---|---|
| `/health` | `200 57` `{"status":"ok","model":"deepseek-chat","version":"0.1.0"}` | `200` 且**不含 `model`**：`{"status":"ok","version":"0.1.0"}` |
| `/papers` | `200 14191` | `200`，大小接近 |
| `/reader/papers` | `200 4931` | `200`，大小接近 |
| `/papers/topics` | `200 8679` | `200`，大小接近 |
| `/settings` | `000 0` | `401`（`.env` 未配密钥则 `503`） |
| `/nonexistent` | `404 22` `{"detail":"Not Found"}` | `404` 且为中文：`{"detail":"未找到该接口"}` |

---

## 1. 这次上线会带来什么变化（必读）

`7887fb8` 相对线上旧版，包含 4 个提交。**其中两个会改变外部行为**：

### 1.1 安全层生效（`5c3a6b8`）—— 评委访问方式不变

| 变化 | 影响 |
|---|---|
| `/settings` 等 8 个管理接口需要登录 | 评委本来就不用设置页，**无影响** |
| `/health` 匿名不再返回 `model` 与预算 | 无影响（前端已修） |
| 请求体限 1MB（JSON）/ 24MB（上传） | 正常使用无影响 |
| 业务限流：对话 6 次/分、读页面 2 次/秒等 | 正常演示无影响；评委反复刷新可能触顶 |
| `APP_ENV` 未配置时默认 `dev` | 【重要】默认 `dev` 下 `/docs` 可访问，无碍演示 |

### 1.2 ⚠️ `.env` 必须配两个密钥，否则管理功能全 503

**这一条是最大的坑**：安全层要求 `ADMIN_TOKEN` 与 `ADMIN_COOKIE_SECRET` **都非空**（`app/security.py:158-167` `admin_configured()`），否则所有管理接口返回 503。

- 评委演示**不受影响**（演示功能都是公开路由）
- 但你自己登录设置中心会失败
- 且启动日志会多一行 503 相关提示

**如果 `.env` 里没有这两项，可以先不管，等需要管理功能时再补。** 不影响评委。

### 1.3 P0-1 事件循环修复（`f624dc0`）—— 纯性能改善

12 处同步阻塞调用移入线程池。**【实测】本机**：一个重请求执行期间 `/health` 从 2263.8ms 降到正常水平。所有接口的响应体 sha256 修复前后完全一致。

### 1.4 前端 undefined 修复（`7887fb8`）

页脚不再显示「模型 undefined」。**注意：这条修复依赖 1.1（`/health` 匿名不返回 model）。如果只上代码不重启，旧进程仍返回 model，前端判断 `d.model ? ... : ''` 依然正常工作——两个方向都安全。**

---

## 2. 部署步骤

> 前提：**必须能 SSH 或通过雨云控制台 VNC 进入 root shell**。SSH 恢复见 `SECURITY-PHASE3-REVIEW.md` 第 6.3 节 B0。
> 在你拿到任一登录入口之前，下面所有步骤都不可执行。

### S1 · 备份（先做，不要跳过）

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p /root/ca-update-$STAMP
cd /opt/campus-agent

# 记录当前版本（回滚靠它）
git rev-parse HEAD | tee /root/ca-update-$STAMP/head-before.txt

# 配置与 systemd 单元
cp -a .env /root/ca-update-$STAMP/.env.bak
systemctl cat campus-agent > /root/ca-update-$STAMP/campus-agent.service.bak

# 只备份代码，不含 data / .venv（这两样本次不动）
tar czf /root/ca-update-$STAMP/code-before.tar.gz \
    --exclude=data --exclude=.venv --exclude=__pycache__ --exclude=.git .

ls -lh /root/ca-update-$STAMP/
```

**验收**：`head-before.txt` 有内容；`code-before.tar.gz` 大小约 20–50MB。

### S2 · 拉取代码

```bash
cd /opt/campus-agent
git status --porcelain        # 期望：空。若有输出，先看清楚是不是服务器被手工改过
git fetch origin
git reset --hard origin/main  # 期望输出 HEAD is now at 7887fb8 fix(web): ...
git rev-parse HEAD            # 期望：7887fb86...（与 S1 记录的 head-before 不同才对）
```

> 用 `reset --hard` 而不是 `pull`：服务器上不应该有本地改动，`reset --hard` 能保证工作区与远端完全一致。**这会丢弃服务器上任何未提交的改动**——所以 S1 的备份必须有。
>
> ⚠️ `data/` 下的 `structures.json`、`recommendations.json` 是**运行时会被服务写入**的文件。如果它们被 git 跟踪且服务在运行中改过它们，`reset --hard` 会把它们还原成仓库版本。这是**预期行为**（仓库版本就是正确的），但如果你在服务器上手改过这两个文件，会丢失。

### S3 · 依赖检查

```bash
cd /opt/campus-agent
.venv/bin/python -m pip install -r requirements.txt --dry-run 2>&1 | tail -20
```

**验收**：不应出现 `Would install` 列表里的大批新包。本次改动**没有新增依赖**（`starlette` 是 uvicorn 的既有依赖）。若出现意外差异，先停下排查，不要盲目安装。

### S4 · 重启服务

```bash
systemctl restart campus-agent
sleep 8
systemctl is-active campus-agent
systemctl status campus-agent --no-pager -l | tail -25
```

**验收**：
- `active`
- 日志里应出现 `安全层路由对账通过：管理 8 条，公开 39 条`
- **若出现 `RuntimeError: 安全层登记了不存在的路由` 或 `有路由未在安全层登记`，说明代码不完整，立刻按第 3 节回滚**

### S5 · 功能验证（在你自己电脑上跑）

```bash
# 1) 匿名 /health —— 关键判据：不应再有 model 字段
curl -s --noproxy "*" http://156.239.2.206:8000/health
# 期望：{"status":"ok","version":"0.1.0"}
# 如果仍含 "model":"deepseek-chat" → 没重启成功，回到 S4

# 2) 公开读接口正常（大小应与第 0 节基线接近）
curl -s --noproxy "*" -o /dev/null -w "%{http_code} %{size_download}\n" http://156.239.2.206:8000/papers
curl -s --noproxy "*" -o /dev/null -w "%{http_code} %{size_download}\n" http://156.239.2.206:8000/reader/papers
# 期望：200 且 size > 4000

# 3) 管理接口被挡（证明安全层生效）
curl -s --noproxy "*" -o /dev/null -w "%{http_code}\n" http://156.239.2.206:8000/settings
# 期望：401（若 .env 没配密钥则为 503，属正常）

# 4) 未知路径文案变成中文（证明新代码确实在跑）
curl -s --noproxy "*" http://156.239.2.206:8000/nonexistent
# 期望：{"detail":"未找到该接口"}（旧代码是英文 "Not Found"）
```

### S6 · 浏览器验收

1. 打开 `http://156.239.2.206:8000/ui/index.html`
2. 页脚应显示「服务正常 · v0.1.0」，**没有「模型 undefined」**
3. 上传一篇 PDF → 进入阅读页 → 章节树正常
4. 提一个问题 → 回答正常流式返回
5. 打开图谱、写作页 → 正常渲染

---

## 3. 回滚

### 3.1 代码回滚（60 秒内完成）

```bash
STAMP=<S1 里记的那个时间戳>
cd /opt/campus-agent
git reset --hard $(cat /root/ca-update-$STAMP/head-before.txt)
systemctl restart campus-agent
sleep 8
systemctl is-active campus-agent
curl -s http://127.0.0.1:8000/health
```

**判据**：`/health` 重新出现 `model` 字段 = 已回到旧版。

### 3.2 如果 git 都坏了（极端情况）

```bash
cd /opt/campus-agent
tar xzf /root/ca-update-$STAMP/code-before.tar.gz -C /opt/campus-agent
systemctl restart campus-agent
```

### 3.3 回滚不影响数据

本次不碰 `data/`、不重建索引、不改 `.env`。所以回滚只是把代码换回去，**数据库和其他数据都还在**。

---

## 4. 本次不做的事（明确边界）

以下事项**不在本文档范围内**，需要单独排期（见 `SECURITY-PHASE3-REVIEW.md`）：

- ❌ 安装配置 Nginx
- ❌ 申请域名 / 配置 HTTPS
- ❌ 修改 `APP_ENV=prod`（**这一项在纯 HTTP 下绝对不要做**，会导致登录 cookie 带 `Secure` 后被浏览器丢弃，登录陷入死循环）
- ❌ 改 `APP_HOST` 为 `127.0.0.1`（需配合 Nginx，否则公网就访问不到了）
- ❌ 配置 `TRUSTED_PROXIES`
- ❌ 重建 Chroma / BM25 索引

---

## 5. 已知遗留问题（本次不修）

| 问题 | 影响 | 来源 |
|---|---|---|
| `LLM_MAX_CONCURRENCY` 是死配置，代码零引用 | 配置了也不生效，并发不受限 | 【实测】第三阶段审查 1.x |
| 分块上传（无 `Content-Length`）时体积超限返回 400 而非 413 | 浏览器/curl 都带 Content-Length，线上无实际影响 | 【实测】`tests/test_event_loop_blocking.py` 有对应用例记录现状 |
| `test_connection()` 调模型不扣每日预算 | 管理员可绕过预算限制做连接测试 | 【实测】第三阶段审查 |
| 限流器多进程不共享 | 当前单进程部署，无影响；若将来加 `--workers` 需重评 | 【实测】 |
| `_today()` 用服务器本地时区 | 【待验证】取决于服务器时区设置 | 第三阶段审查 |