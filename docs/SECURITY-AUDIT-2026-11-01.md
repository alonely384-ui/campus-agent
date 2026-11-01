# 公网安全只读审计报告

- 审计对象：`http://156.239.2.206:8000`
- 审计时间：2026-11-01
- 审计方式：**只读**。全程未修改任何线上配置、未调用任何写接口、未上传文件、未删除数据
- 代码基线：commit `8055410`

> 本报告只找问题、排优先级，不含修复方案的实施。

---

## 一、结论摘要

| 等级 | 数量 | 说明 |
|---|---:|---|
| 🔴 P0 必须赛前修 | 2 | 写接口零认证、无任何限流 |
| 🟠 P1 建议赛前处理 | 3 | 配置全量可读、无 HTTPS、API 文档全公开 |
| 🟡 P2 可赛后处理 | 3 | 无反向代理、OpenAPI 暴露内部路由、无安全响应头 |
| ✅ 检查通过 | 4 | 路径穿越、请求体限制、输入校验、仓库无密钥 |

**一句话结论**：这是一个「功能完整但完全没有防护」的公网部署。所有人都能读写，没有任何一道门。

---

## 二、服务器与域名现状（回答前置问题）

### 商家
```
DMI sys_vendor    : QEMU
DMI product_name  : Standard PC (i440FX + PIIX, 1996)
公网 IP 归属      : AS154321 CORENET CLOUD SDN. BHD.
城市/地区         : Tung Chung, Islands, HK
```

DMI 显示的是 **QEMU 虚拟化标识**，不是云厂商专有标识（阿里云会显示 `Alibaba Cloud`、腾讯云会显示 `Tencent Cloud`）。ASN 归属是 `CORENET CLOUD`。

**从服务器本身无法确认商家名称**，只能以你的购买记录或控制台为准。早前记录的「雨云」来自你的口述，实测数据无法印证。

### 域名与 HTTPS
```
主机名            : instance-T62ybeGJ
/etc/hosts        : 只有 localhost，无自定义域名
nginx             : 未安装
certbot           : 未安装
/etc/letsencrypt  : 不存在
监听端口          : 22 (sshd) / 8000 (uvicorn) / 53 (systemd-resolved)
```

**没有域名、没有反向代理、没有证书。** 当前 uvicorn 直接监听 `0.0.0.0:8000` 对外提供明文 HTTP。

### HTTPS 路线判断

| 路线 | 前提条件 | 当前是否可行 |
|---|---|---|
| Let's Encrypt 免费证书 | **必须有域名**（LE 已停止为纯 IP 签发证书） | ❌ 缺域名 |
| 自签证书 | 无需域名 | ⚠️ 可行但浏览器全页红警告，评委体验差 |
| 云厂商 CDN/反代加证书 | 需域名 | ❌ 缺域名 |
| 维持 HTTP | 无 | ✅ 当前状态 |

**关键点：香港节点不需要工信部 ICP 备案**，选香港地区注册的域名或使用境外 DNS 服务，拿到域名即可直接签发 LE 证书，无需等备案周期。

预计流程：购买域名（`.top`/`.xyz` 约几十元/年）→ DNS A 记录指向 `156.239.2.206` → 安装 nginx + certbot → uvicorn 改监听 `127.0.0.1:8000`，由 nginx 对外提供 443。

**时间约束**：服务器 **2026-11-30 到期**，国赛 12-27。现买域名省赛（11-18）可用上，但国赛期间的可用性取决于是否续费。

---

## 三、问题清单（按优先级）

### 🔴 P0-1：全部 44 个接口零认证，19 个写接口公网可直接调用

**证据**（代码层）：
```
$ grep -c "CORSMiddleware|Depends|add_middleware|HTTPBasic|APIKey|@app.middleware" app/main.py
No matches found
```
代码里**没有任何认证机制的痕迹**：无中间件、无依赖注入守卫、无 BasicAuth、无 Token 校验。

**证据**（接口层）：OpenAPI 文档公示的写接口全部无需凭证即可访问：

| 方法 | 路径 | 危害 |
|---|---|---|
| `DELETE` | `/papers/{paper_id}` | **任意删除库内论文** |
| `POST` | `/papers/upload` | 塞入任意 PDF，撑爆磁盘 |
| `POST` | `/upload` | 同上 |
| `POST` | `/settings` | **改模型、改 API Key、改检索参数** |
| `POST` | `/settings/index/cleanup` | **清空索引** |
| `POST` | `/settings/restore` | 重置配置 |
| `POST` | `/library/download` | 触发外部下载，消耗流量 |
| `POST` | `/recommendations/refresh` | 触发 arXiv 全量扫描，耗时耗资源 |
| `POST` | `/chat`、`/chat/stream` | **消耗 DeepSeek API 额度** |
| 其余 10 个 | 写作、翻译、图表生成 | 消耗 API 额度 + 磁盘 |

**实际验证**：浏览器直接打开 `http://156.239.2.206:8000/ui/settings.html`，页面完整渲染，保存按钮可点（截图 `_public/audit-02-settings.png`、`_public/audit-04-kb-panel.png`）。

**风险量化**：
- 评委看到的是公开 URL，任何人在大赛期间都可能扫到
- 一个脚本可以在几分钟内删光 39 篇论文、清空索引
- DeepSeek API 额度可被刷爆，账单由你承担

**建议修复方向**（未实施）：全局 HTTP Basic Auth 中间件，或基于 Cookie 的简单口令登录；对写接口单独加守卫；对 `/ui/settings.html` 整页拦截。

---

### 🔴 P0-2：零限流，任何接口可被无限调用

**证据**：
```
连续 5 次快速请求 /health -> 状态码 [200, 200, 200, 200, 200]，耗时 1.99s
是否有任何限流: 否
```
代码中无 `slowapi`、无 `nginx limit_req`、无自实现计数器。

**风险**：即使加了认证，单个凭证仍可被反复调用。`/chat` 每次调用都真实消耗 DeepSeek 额度，`/recommendations/refresh` 实测单次耗时 14~34 秒，并发几次就能占满 2 核 CPU（`MemoryMax=3G`）。

**建议修复方向**（未实施）：nginx 层 `limit_req` 最快；或在 FastAPI 加基于 IP 的令牌桶。

---

### 🟠 P1-1：`GET /settings` 把全部 13 项配置公开

**证据**：无认证即可读取完整配置，实测输出：

```
[model]
  LLM_MODEL     = deepseek-chat              <-- 环境变量覆盖
  LLM_BASE_URL  = https://api.deepseek.com   <-- 环境变量覆盖
  LLM_API_KEY   = ***                        <-- 环境变量覆盖
[rag]
  RAG_W_DENSE = 2.0 / RAG_W_BM25 = 1.0 / RAG_W_HASH = 0.5
  RAG_RRF_K = 60 / RAG_RERANK = 1 / RAG_RERANK_POOL = 15
  RAG_RECALL_DEPTH = 50 / RAG_CITATION_PENALTY = 0.6
[schedule]
  DISCOVER_SCHEDULE = daily / DISCOVER_HOUR = 8
共 13 项全部公开可读
```

**API Key 值没有泄露**（`sk-` 出现 0 次，仅返回 `***` 与 `key_configured: true`）——脱敏逻辑是有效的。

**但仍然泄露**：模型名称、API 端点、全部检索参数。这些属于技术选型信息，对竞赛对手有价值。

---

### 🟠 P1-2：无 HTTPS，所有流量明文传输

**证据**：`http://156.239.2.206:8000`，443 端口未监听，无证书。

**风险**：
- 若加了口令登录，口令将明文过网
- 部分浏览器对 HTTP 页面标记「不安全」，影响评委观感
- 官方 3.4 在线演示未强制要求 HTTPS，但用户体验评分（10%）会受影响

---

### 🟠 P1-3：`/docs` 与 `/redoc` 全公开，暴露 44 个接口签名

**证据**：
```
GET /docs         -> 200   1011B  Swagger UI
GET /redoc        -> 200    893B  ReDoc
GET /openapi.json -> 200  36154B  完整 schema
```

截图 `_public/audit-03-openapi.png`。

**风险**：攻击者无需读代码即可获知全部参数结构与端点，P0-1 的可利用性因此大幅提升。

**建议**：生产环境将 `docs_url` / `redoc_url` / `openapi_url` 设为 `None`，或加认证。

---

### 🟡 P2-1：uvicorn 直接对外，无反向代理

**证据**：`ss -lntp` 显示 `0.0.0.0:8000` 由 python 进程直接监听，无 nginx。

**影响**：无法使用 nginx 的限流、静态文件缓存、安全响应头、TLS 终止等能力。这是 P0-2、P1-2 的**共同前置依赖**——装 nginx 能一次解决三个问题。

---

### 🟡 P2-2：无安全响应头

未配置 `X-Content-Type-Options`、`X-Frame-Options`、`Content-Security-Policy`、`Strict-Transport-Security`。

**影响**：页面的 iframe 嵌入、MIME 嗅探类攻击无防护。实际危害低（本项目不是高价值目标），但属于工程规范缺口。

---

### 🟡 P2-3：论文数据与图谱结构全部公网可读

**证据**：
```
GET /papers          -> 200  14191B  39 篇论文完整元数据
GET /papers/topics   -> 200   8679B  主题分组
GET /graph           -> 200  79230B  36 节点 189 边完整图谱
GET /recommendations -> 200  14316B  推荐列表
```

截图 `_public/audit-05-library.png`。

**影响**：这是产品本身要展示的内容，**泄露影响低**，无需处理。列在此处是为完整性。

---

## 四、检查通过的项目（无问题）

| 项 | 测试方式 | 结果 |
|---|---|---|
| 路径穿越 / 任意文件读取 | 10 种 payload 打 `/paper/{filename}`、`/writing/chart/{name}`、`/writing/diagram/{name}` | **全部 404**，FastAPI 路径参数做了有效约束 |
| `/ui/` 静态目录穿越 | `/ui/../.env`、`/ui/%2e%2e/.env`、`/ui/..%2F.env`、`/ui/../../app/config.py` | **全部 404**，StaticFiles 有内建防护 |
| 请求体大小限制 | `/translate/selection` 提交 200KB 文本 | **422 `string_too_long`**，限制 6000 字符 |
| 输入校验 | 空 text、非法 mode、非法路径 | **全部 422/404**，Pydantic 校验层有效 |
| 仓库密钥泄露 | `git grep` 扫 `sk-*`、`password=`、IP、服务器口令 | **无真实密钥**。命中的 4 处全是 `tests/test_settings.py` 的假密钥 `sk-realkey1234567890abcd` |
| `.env` 是否入库 | `git check-ignore .env` → `.gitignore:10:.env` | **未入库，已忽略** |
| 上传体积限制 | 代码审读 | `UPLOAD_MAX_BYTES = 20MB` + 分块读取 + 超限删半截文件（已在前序修复中实现） |

---

## 五、优先级排序与建议动作

假设**省赛 11-18**、**国赛 12-27**，服务器 **11-30 到期**。

| 序号 | 事项 | 优先级 | 预估工作量 | 前置依赖 |
|---:|---|---|---|---|
| 1 | 全局认证（写接口 + 设置页） | 🔴 P0 | 2~3 小时 | 无 |
| 2 | 关闭 `/docs`、`/redoc`、`/openapi.json` | 🟠 P1 | 5 分钟 | 无 |
| 3 | 购买域名 | 🟠 P1 | 30 分钟 + 解析生效等待 | 无 |
| 4 | 装 nginx + certbot，uvicorn 降为 127.0.0.1 | 🔴 P0 | 2 小时 | 第 3 项完成 |
| 5 | nginx 层限流 | 🔴 P0 | 20 分钟 | 第 4 项完成 |
| 6 | 安全响应头 | 🟡 P2 | 15 分钟 | 第 4 项完成 |
| 7 | 服务器续费（覆盖到 12-27） | 🔴 业务阻断 | — | 无 |

**最小闭环建议**：第 1、2 项立刻做（约半天），第 3~6 项在第 1、2 项之后合并实施（约半天）。第 7 项是**硬约束**，不做则国赛期间站点直接不可用。

**注意**：第 1 项加认证后，评委访问需要输入口令。需在提交说明里写清凭证，或采用「演示时开放、平时关闭」的开关（例如按 IP 白名单放行大赛评审环境）。

---

## 六、审计素材

| 文件 | 内容 |
|---|---|
| `_public/audit-01-home.png` | 公网首页，无登录直接进入 |
| `_public/audit-02-settings.png` | 设置中心完整可操作，含保存按钮 |
| `_public/audit-03-openapi.png` | Swagger 文档，44 个接口全公开 |
| `_public/audit-04-kb-panel.png` | 知识库管理面板，含清理按钮 |
| `_public/audit-05-library.png` | 论文库，39 篇元数据公网可读 |

---

## 七、本次未做的事

- 未调用任何写接口验证危害（只从 OpenAPI 清单与代码确认可调用性）
- 未做 DoS 压测（会真实打挂服务，违背只读原则）
- 未做认证绕过测试（不存在认证机制，无绕过可言）
- 未审计第三方依赖的已知 CVE
- 未检查防火墙规则 / 云安全组（需控制台权限，服务器内部看不到）