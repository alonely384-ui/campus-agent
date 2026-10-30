# 科研小助手 · 阿里云部署手册

> 目标机器：阿里云轻量应用服务器 **2核4G**（Ubuntu 22.04）
> 预计耗时：40 分钟（不含买机器）
> **每步都有验证命令，做完一步验一步，别一口气跑完**

---

## 第 0 步 · 买机器（10 分钟）

### 0.1 注册与实名

1. 打开 https://www.aliyun.com
2. 注册账号 → **个人实名认证**（身份证，通常即时通过）

### 0.2 学生认证 + 领券

1. 打开「[云工开物](https://university.aliyun.com/)」学生专区
2. 完成学生认证（学信网验证，一般几分钟到几小时）
3. 领 **300 元无门槛优惠券**，记下有效期（1 年）

### 0.3 买服务器（关键：选对配置）

打开 https://www.aliyun.com/product/swas （轻量应用服务器）

| 选项 | 选什么 | 为什么 |
|---|---|---|
| 地域 | **离你近的**（如成都、杭州） | 延迟低 |
| 镜像 | **Ubuntu 22.04** | 别选 CentOS，后面 apt 命令全对不上 |
| 配置 | **2核 4G** | ⚠️ **千万别选 2核2G**，实测跑一次问答峰值 876 MB |
| 套餐 | 标准价 **199 元/年** 那档 | ⚠️ 别买 38 元秒杀款，券不能抵 |
| 时长 | 1 年 | |

**结账时选「使用优惠券」抵 300 元 → 自付 0 元。**

### 0.4 记录三样东西

买完在控制台「服务器详情」里记下：

```
公网 IP:   ____________________
root 密码: ____________________（或你自己设的）
```

### ✅ 验证

浏览器访问 `http://<你的公网IP>`，看到阿里云默认页就算成功。

---

## 第 1 步 · 连上服务器（2 分钟）

Windows 上打开 PowerShell：

```powershell
ssh root@<你的公网IP>
```

第一次会问 `yes/no`，输 `yes`，然后输密码（输密码时屏幕不动是正常的）。

### 顺手改密码（建议）

```bash
passwd
```

### ✅ 验证

提示符变成 `root@iZ...#` 就对了。

---

## 第 2 步 · 配系统环境（5 分钟）

一条一条粘进去，别整段复制。

```bash
# 更新系统
apt update && apt upgrade -y

# 装基础工具（python3.11 + 中文字体 + graphviz 依赖）
apt install -y python3.11 python3.11-venv python3-pip git curl unzip \
               build-essential libgomp1 fonts-noto-cjk

# 建一个普通用户跑服务（不推荐直接用 root 跑应用）
adduser --disabled-password --gecos "" appuser

# 建目录
mkdir -p /opt/campus-agent
chown -R appuser:appuser /opt/campus-agent
```

### ✅ 验证

```bash
python3.11 --version
# 期望：Python 3.11.x

fc-list :lang=zh | head -5
# 期望：能看到 Noto Sans CJK SC / Noto Serif CJK SC 等字体
# 注意：Ubuntu 24.04 的 fonts-noto-cjk 包装的是 Serif 版本，
#       用 grep "noto sans cjk" 去验证会查不到，看起来像装失败了。
#       用 fc-list :lang=zh（按语言查）才准。

fc-match sans-serif:lang=zh
# 期望：返回一个 .ttc/.otf 路径，不是 "DejaVuSans.ttf"
# 这条最关键 —— 它告诉我们「网页请求中文字体时实际返回哪个文件」，
# Chromium 渲染 Mermaid 走的就是这条路径。
```

### 🚨 中文字体：为什么必须单独确认

**现象**：服务器上一切正常，但「写作助手 → 架构图」生成的 PNG 里中文全是方块。
- Mermaid 渲染出来是 `□□□□`
- Graphviz 渲染出来是 `Web[52][74]`（把 Unicode 码位画成了方框）

**根因**：Ubuntu 最小安装默认**不带任何 CJK 字体**。两条渲染路径都会挂：

| 渲染器 | 字体由谁决定 | 缺字体时的表现 |
|---|---|---|
| **Mermaid** | Chromium 的 CSS 字体栈 | 节点文字变 `□□` |
| **Graphviz** | `dot` 的 `fontname` 属性 | 文字变 `[52][74]` 这种码位方框 |
| **TikZ** | `\usepackage{ctex}` 调 LaTeX 字体 | 编译直接失败 |

代码里已经做了两层兜底（`app/diagram.py`）：
- `_ensure_font()` 给 DOT 代码注入 `fontname="Microsoft YaHei"`
- `_ensure_mermaid_font()` 给 Mermaid 代码注入 `%%{init}%` 指令指定字体族

但**这两层都只能「指定字体名」，不能「凭空造出字体文件」**。系统里没有字形，指定什么都是方块。

**所以 `apt install fonts-noto-cjk` 是硬要求，不是可选项。**

装完之后重启服务（字体缓存被清理时 Chromium 可能还握着旧句柄）：

```bash
pkill -f "uvicorn app.main:app"; sleep 2
nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &
```

验证方式：调一次 `/writing/diagram`，**把返回的 PNG 下载下来用眼睛看**——只看 `ok: true` 是不够的，中文成方块时接口照样返回成功。

---

## 第 3 步 · 传代码和数据（10 分钟，最耗时）

**在你本地 Windows 上开一个新的 PowerShell**（别关掉服务器的那个）

### 3.1 传代码

```powershell
cd D:\alonely\deepseek\campus-agent

scp -r app web scripts tests root@<你的公网IP>:/opt/campus-agent/
```

### 3.2 传数据（457 MB，要等几分钟）

```powershell
# 先打包，比一条条传快得多
Compress-Archive -Path data\chroma, data\papers, data\models, data\meta, requirements.txt -DestinationPath data-bundle.zip -Force

scp data-bundle.zip root@<你的公网IP>:/opt/campus-agent/
```

### 🚨 3.3 传 .env（别漏，也别传到 Git）

`.env` 里有你的 API 密钥。

```powershell
scp .env root@<你的公网IP>:/opt/campus-agent/.env
```

### 3.4 回到服务器解压

```bash
cd /opt/campus-agent
unzip -o data-bundle.zip
rm data-bundle.zip
```

### ✅ 验证

```bash
cd /opt/campus-agent
du -sh data
# 期望：约 457M

ls data/models/ms-marco-MiniLM-L-6-v2/
# 期望：能看到 model.onnx 和 tokenizer.json —— 这两个决定重排能不能用

ls -la .env
# 期望：文件存在
```

---

## 第 4 步 · 建虚拟环境装依赖（10 分钟）

```bash
cd /opt/campus-agent
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### ✅ 验证

```bash
.venv/bin/python -c "import fastapi, chromadb, onnxruntime, pymupdf; print('依赖 OK')"
# 期望：依赖 OK
```

---

## 第 5 步 · 先手动跑一次（5 分钟）

**别急着配常驻，先确认能跑起来。**

```bash
cd /opt/campus-agent
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

另开一个终端连服务器，执行下面三条。

### ✅ 验证（三条都要过）

**1. 健康检查**
```bash
curl http://localhost:8000/health
# 期望：{"status":"ok","model":"deepseek-chat","version":"0.1.0"}
```

**2. 完整问答（这条最吃内存，必须测）**
```bash
curl -X POST http://localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"message":"Attention 的核心机制是什么？"}'
# 期望：HTTP 200，返回带证据的答案
```

**3. 看内存**
```bash
free -h
# 跑完问答后 available 应该还剩 1G 以上
```

### ❌ 报错对照表

| 现象 | 原因 | 解法 |
|---|---|---|
| `Address already in use` | 8000 被占 | 换 `--port 8001` |
| `ModuleNotFoundError` | 依赖没装全 | 重跑第 4 步 |
| `/chat` 报 LLM 错误 | `.env` 没传或密钥错 | 检查 `/opt/campus-agent/.env` |
| 进程被 Kill | **内存不够** | 确认买的是 2核4G 不是 2核2G |

按 `Ctrl+C` 停掉，继续下一步。

---

## 第 6 步 · 配成常驻服务（5 分钟）

```bash
cat > /etc/systemd/system/campus-agent.service << 'EOF'
[Unit]
Description=campus-agent 科研小助手
After=network.target

[Service]
Type=simple
User=appuser
Group=appuser
WorkingDirectory=/opt/campus-agent
Environment="PATH=/opt/campus-agent/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/campus-agent/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
MemoryMax=3G
StandardOutput=append:/var/log/campus-agent.log
StandardError=append:/var/log/campus-agent.log

[Install]
WantedBy=multi-user.target
EOF

chown -R appuser:appuser /opt/campus-agent
touch /var/log/campus-agent.log
chown appuser:appuser /var/log/campus-agent.log

systemctl daemon-reload
systemctl enable campus-agent
systemctl start campus-agent
systemctl status campus-agent --no-pager
```

### 关键一句解释

`MemoryMax=3G` 是保险丝：内存超过 3G 会被杀掉，然后 `Restart=always` 自动拉起。
**好过一次卡死整机不响应。**

### ✅ 验证

```bash
systemctl status campus-agent --no-pager
# 期望：Active: active (running)

curl http://localhost:8000/health
# 期望：{"status":"ok",...}
```

---

## 第 7 步 · 开防火墙端口（2 分钟）

阿里云有**两层**防火墙，都要开。

### 7.1 服务器内的 ufw

```bash
ufw allow 22/tcp
ufw allow 8000/tcp
ufw --force enable
ufw status
```

### 7.2 阿里云控制台安全组（这步最容易忘）

1. 进「轻量应用服务器」控制台
2. 点你的实例 → **防火墙**
3. 添加规则：

| 应用类型 | 协议 | 端口 | 授权对象 |
|---|---|---|---|
| 自定义 | TCP | **8000** | 0.0.0.0/0 |

### ✅ 验证

**在你本机浏览器**（不是服务器上）打开：

```
http://<你的公网IP>:8000/ui/
```

能看到「科研小助手」首页就算通了。

---

## 第 8 步 · 最终验收（5 分钟）

逐条打勾：

- [ ] `http://<IP>:8000/health` 返回 ok
- [ ] `http://<IP>:8000/ui/` 首页能打开
- [ ] 点开「论文库」，能看到 39 行论文
- [ ] 在网页里问一个问题，能出**带证据卡片的答案**（四段结构：结论/依据/局限/下一步）
- [ ] 回答下方有「多智能体协作」时间线，五个角色都在
- [ ] `http://<IP>:8000/ui/graph.html` 引用图谱能画出来（节点 36 / 边 189）
- [ ] `http://<IP>:8000/ui/analysis.html` 点「前沿探索」能看到三组结果
- [ ] `http://<IP>:8000/ui/writing.html` 写作助手能打开
- [ ] **写作助手 → 架构图，选 Mermaid 生成一张，下载下来看中文是不是方块**
- [ ] 服务器上 `free -h` 显示 available 还剩 1G 以上
- [ ] `systemctl status campus-agent` 是 running

### 关键接口自检（复制粘贴即跑）

```bash
# 一张表看全部：哪个通、哪个不通，一眼就知道
for p in /health /papers/files /reader/papers /graph /graph/survey /graph/frontier \
         /recommendations /writing/renderers; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000$p)
  printf "%-20s %s\n" "$p" "$code"
done
```

期望全部是 `200`。`/writing/renderers` 返回 `{"mermaid":true,"graphviz":true,"tikz":false}` 是**正常**的
——`tikz` 为 `false` 表示没装 TeX Live，前端会自动把这一项灰掉，不影响演示。

```bash
# 完整出图 + 中文校验（能出图 ≠ 中文对，一定要下载看）
curl -s -X POST http://127.0.0.1:8000/writing/diagram \
  -H "Content-Type: application/json" \
  -d '{"description":"用户提问经意图识别，走BM25检索和大模型生成，输出带引用回答","kind":"mermaid"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok=', d.get('ok')); print('http://<IP>:8000' + str(d.get('image_url')))"
```

把打印出的地址在浏览器打开，**看到中文清晰可读才算通**。

---

## 常见问题速查

### 服务起不来

```bash
tail -50 /var/log/campus-agent.log
journalctl -u campus-agent -n 50 --no-pager
```

### 重启服务

```bash
systemctl restart campus-agent
```

### 改了代码怎么更新

```powershell
# 本地
scp -r app root@<IP>:/opt/campus-agent/
```
```bash
# 服务器
chown -R appuser:appuser /opt/campus-agent
systemctl restart campus-agent
```

### 内存真不够时（治标不治本，但能防 OOM）

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

---

## 一页速查

```bash
ssh root@<IP>                                        # 连服务器
systemctl status campus-agent --no-pager             # 服务状态
tail -f /var/log/campus-agent.log                    # 看日志
systemctl restart campus-agent                       # 重启
free -h                                              # 看内存
curl http://localhost:8000/health                    # 健康检查
curl -X POST http://localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"message":"Attention 的核心机制是什么？"}'  # 完整问答
```