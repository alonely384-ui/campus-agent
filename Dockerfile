# ============================================================================
# 科研小助手（research-agent）
#
# 构建：docker build -t research-agent .
# 运行：docker run -p 8000:8000 --env-file .env research-agent
#
# 说明：
#   - 基础镜像要 Debian 版（不是 alpine）：chromadb、onnxruntime、matplotlib 都依赖
#     glibc 与预编译 wheel，alpine 的 musl 会让 pip 去现场编译，构建时间爆炸。
#   - PDF 渲染依赖中文字体。python:3.11-slim 默认没有 CJK 字体，matplotlib 导出
#     中文图表会全变成方框 —— 装 fonts-noto-cjk 解决。
#   - Graphviz / Mermaid / TikZ 三个渲染器是可选的：容器里只装了 graphviz，
#     Mermaid 需要 Node.js、TikZ 需要完整的 TeX 发行版（约 3GB），不放进镜像。
#     服务启动时会用 /writing/renderers 报告哪些渲染器可用，前端自动置灰不可用项。
# ============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- 系统依赖 -------------------------------------------------------------
# graphviz      : Graphviz 架构图渲染（dot 命令）
# libgomp1      : onnxruntime 运行时依赖（RapidOCR 扫描件识别）
# fonts-noto-cjk: matplotlib 导出中文图表用的字体
# curl          : HEALTHCHECK 探活
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        graphviz \
        libgomp1 \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ----------------------------------------------------------
# 先只拷 requirements.txt，让这一层能被 Docker 缓存住：改业务代码时不用重装依赖。
# requirements.txt 已与 app/ 的实际 import 一一对应（可用 scripts/check_deps.py 核对），
# 所以这里装一次就够了，不需要再补装任何包。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 应用代码 -------------------------------------------------------------
COPY app/ ./app/
COPY web/ ./web/
COPY scripts/ ./scripts/

# 数据目录。本地开发时这里放论文 PDF、解析结果、索引；容器里用 volume 挂载持久化
RUN mkdir -p /app/data/papers /app/data/meta /app/data/chroma /app/data/uploads

# 论文元数据清单（39 篇的标题/年份/分类，13 KB）。
# 它不是运行产生的数据，而是随代码发布的静态清单，所以 .dockerignore 里专门留了例外。
# 不带它的话，按 README 的 docker run（不挂 volume）启动后容器里没有这份清单，
# "库里有哪些论文 / 一共多少篇"这类问题会退化成数检索片段，答案直接失真。
COPY data/catalog.json ./data/catalog.json

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    CHROMA_DIR=/app/data/chroma \
    MPLCONFIGDIR=/tmp/matplotlib

# 不需要 .env 也能启动（配置中心有默认值），有 .env 时自动读
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 注意：不能用 `python -m app.main` —— 它里面写了 reload=True，那是开发模式，
# 在容器里会起 file watcher 并多占一个进程。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]