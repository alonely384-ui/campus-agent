"""FastAPI 入口：把 Agent 包装成一个 HTTP 服务。

启动方式（两种都行）：
    python -m app.main          ← 直接跑
    uvicorn app.main:app --reload   ← 开发时用这个，改代码自动重启

启动后打开 http://127.0.0.1:8000/docs 就能看到自动生成的接口文档。
"""

import difflib
import json
import re
from pathlib import Path

import fitz
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import kb_health, llm, scheduler, settings_api
from app.agent import run_agent, run_agent_stream
from app.chart import make_chart
from app.config import settings
from app.diagram import find_paper_figures, generate_diagram, renderer_available
from app.figures import figure_bytes, page_png
from app.graph import frontier, graph_view, load_graph, survey
from app.layout import detect_layout
from app.logging_config import get_logger, setup_logging
from app.online import (
    OnlineError,
    arxiv_id_of,
    citation_counts,
    download_pdf,
    paper_meta,
    references,
    search_papers,
    write_cache,
)
from app.papers import (
    PAPERS_DIR,
    delete_paper,
    delete_structure,
    get_paper,
    get_structure,
    list_papers,
    load_structures,
    merge_structure,
    paper_path,
    save_paper,
    version_of,
)
from app.rag.pipeline import index_paper, remove_source
from app.reader import list_reader_papers, reader_meta, safe_paper_path
from app.recommend import (
    CACHE_KEY,
    DEFAULT_USER,
    RECOMMEND_FILE,
    build_recommendations,
    cached_recommendations,
    library_rows,
    load_feedback,
    rerank_with_feedback,
    save_feedback,
)
from app.translate import (
    load_translation,
    references_page_of,
    translate_pages,
    translate_text,
)
from app.writing import generate_framework
from app.zh2en import parse_glossary, translate_zh2en

setup_logging()
logger = get_logger(__name__)

# 单一版本号来源：FastAPI 元数据、/health、设置页「关于」都读它
VERSION = "0.1.0"

app = FastAPI(
    title="Campus Agent",
    description="校园智能助手：RAG 问答 + 工具调用 + 流式输出",
    version=VERSION,
)

UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ---------------- 请求 / 响应模型 ----------------
class ChatRequest(BaseModel):
    """POST /chat 的请求体。Pydantic 会自动校验字段，写错了直接返回 422。"""

    message: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    history: list[dict] = Field(default_factory=list, description="历史消息，可留空")
    source: str | None = Field(default=None, description="只在这篇论文里检索（单篇问答）")
    sources: list[str] | None = Field(
        default=None, description="多篇对比：只在这些论文里检索（每篇各取 3 块）"
    )
    use_dense: bool = Field(
        default=True,
        description="是否启用稠密向量召回。前端「智能搜索」关掉时传 false，只跑哈希 + BM25",
    )


class ChatResponse(BaseModel):
    answer: str
    rounds: int
    steps: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list, description="检索证据（证据卡片）")
    roles: list[dict] = Field(default_factory=list, description="四个角色的执行记录（时间线）")
    status: str = Field(default="", description="状态回显：证据数 / 覆盖论文 / 页码")


# ---------------- 写作助手（1.6）请求模型 ----------------
class FrameworkRequest(BaseModel):
    """POST /writing/framework 的请求体。"""

    idea: str = Field(..., min_length=5, max_length=2000, description="研究想法")
    candidates: int = Field(default=16, ge=4, le=40, description="候选文献池大小")


class DiagramRequest(BaseModel):
    """POST /writing/diagram 的请求体。"""

    description: str = Field(..., min_length=5, max_length=2000, description="要画的系统描述")
    kind: str = Field(default="mermaid", description="渲染器：mermaid / graphviz")


class Zh2EnRequest(BaseModel):
    """POST /writing/zh2en 的请求体。"""

    text: str = Field(..., min_length=1, max_length=20000, description="待翻译的中文")
    glossary: str = Field(default="", description="术语表，一行一条「中文=English」")


# ---------------- 接口 ----------------
@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    """浏览器默认会来要 /favicon.ico，页面里只声明了 SVG 版，这里转过去。

    不加这条的话，每个页面开控制台都会有一条 404——虽然不影响功能，
    但看演示时会让人以为哪坏了。用 302 而不是把 ico 静态文件塞进仓库：
    SVG 一份就够，改图标只改一个地方。
    """
    return RedirectResponse(url="/ui/favicon.svg", status_code=302)


@app.get("/health", summary="健康检查")
async def health() -> dict:
    """部署后用这个接口判断服务是不是活着。Nginx / Docker 都靠它做探活。"""
    return {"status": "ok", "model": settings.llm_model, "version": VERSION}


@app.post("/chat", response_model=ChatResponse, summary="对话（一次性返回）")
async def chat(req: ChatRequest) -> ChatResponse:
    """最简单的一问一答。前端想要"打字机效果"请用 /chat/stream。"""
    try:
        result = await run_agent(
            req.message,
            req.history,
            source=req.source,
            sources=req.sources,
            use_dense=req.use_dense,
        )
    except Exception as e:
        logger.exception("处理 /chat 失败")
        # 不要把原始异常直接抛给用户（可能泄漏内部信息），记日志 + 返回通用提示
        raise HTTPException(status_code=500, detail="服务器处理失败，请稍后重试") from e

    return ChatResponse(
        answer=result.answer,
        rounds=result.rounds,
        steps=[
            {"tool": s.tool_name, "arguments": s.arguments, "result": s.result}
            for s in result.steps
        ],
        evidence=[e.to_dict() for e in result.evidence],
        roles=[r.to_dict() for r in result.roles],
        status=result.status,
    )


@app.post("/chat/stream", summary="对话（SSE 流式返回）")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """流式接口。

    返回的不是普通 JSON，而是 SSE（Server-Sent Events）格式的文本流：
        data: {"type":"tool_call","tool":"calculator",...}

        data: {"type":"answer_delta","text":"答案是"}

    前端用 EventSource 或 fetch + ReadableStream 来消费。
    """

    async def event_generator():
        try:
            async for event in run_agent_stream(
                req.message,
                req.history,
                source=req.source,
                sources=req.sources,
                use_dense=req.use_dense,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("流式处理失败")
            err = {"type": "error", "message": "服务器处理失败"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # ⚠️ 这一行很重要！不加的话 Nginx 会把流缓冲起来，
            #    前端就变成"等半天然后一次性全出来"，流式效果全没了。
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/upload", summary="上传文档")
async def upload(file: UploadFile = File(...)) -> dict:
    """接收文件并存到 data/uploads/。

    目前只是"存下来"，第 2 周你要接着做：
        解析 PDF → 切分 → 存进 Chroma → 之后提问就能检索到。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # 安全：只取文件名，防止有人传 "../../windows/system32" 这种路径穿越
    safe_name = Path(file.filename).name
    if not safe_name.lower().endswith((".pdf", ".md", ".txt")):
        raise HTTPException(status_code=400, detail="只支持 pdf / md / txt")

    dest = UPLOAD_DIR / safe_name
    content = await file.read()

    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 20MB")

    dest.write_bytes(content)
    logger.info("已保存上传文件: %s (%d 字节)", dest, len(content))

    return {"filename": safe_name, "size": len(content), "saved_to": str(dest)}


# ---------------- 论文库 ----------------
# 和 /upload 的区别：/upload 是「随便存个文件」；
# /papers/* 是「存进论文库，抽文字、算相似度、判重」，后续 RAG 从这里取数据。

class PaperOut(BaseModel):
    """论文列表里的单项。显式声明字段，前端能一眼看到结构。"""

    paper_id: str
    filename: str
    size: int
    pages: int
    uploaded_at: str
    duplicate_of: str = ""
    duplicate_reason: str = ""
    # 以下三项来自 app/paper_topics.py 的只读聚合：解析出的真实标题、截断后的短标题、
    # 以及它属于哪个引用图谱社区。取不到时给空串 / -1，前端自己决定怎么退回。
    title: str = ""
    display_title: str = ""
    title_truncated: bool = False
    topic_id: int = -1
    topic_name: str = ""


@app.get("/papers/files", summary="论文库里的文件名（供单篇选择）")
async def papers_files() -> dict:
    """返回 data/papers 下实际存在的 PDF 文件名。

    为什么不复用 /papers：/papers 读的是上传索引，批量解压进来的 41 篇不在索引里；
    单篇问答只需要文件名，直接扫磁盘最准。
    """
    from app.papers import PAPERS_DIR

    return {"files": sorted(p.name for p in PAPERS_DIR.glob("*.pdf"))}

@app.get("/papers", summary="论文列表")
async def papers_list() -> dict:
    """列出论文，顺带附上解析出的真实标题和主题归属（只读聚合，不调大模型）。"""
    items = list_papers()
    topics = {p.filename: p for p in _topic_index([p.filename for p in items])}
    papers: list[PaperOut] = []
    for item in items:
        row = PaperOut(**vars(item))
        topic = topics.get(item.filename)
        if topic is not None:
            row.title = topic.title
            row.display_title = topic.display_title
            row.title_truncated = topic.truncated
            row.topic_id = topic.topic_id
            row.topic_name = topic.topic_name
        papers.append(row)
    return {"total": len(papers), "papers": papers}


def _topic_index(filenames: list[str]):
    """把分组结果摊平，方便按文件名回查。分组失败时返回空列表，不影响论文列表本身。"""
    from app.paper_topics import build_topics

    try:
        groups = build_topics(filenames)
    except Exception as exc:  # noqa: BLE001 - 主题是锦上添花，不能让它拖垮论文库
        logger.warning("论文主题分组失败，本次不展示主题信息：%s", exc, exc_info=True)
        return []
    return [paper for group in groups for paper in group.papers]


@app.get("/papers/topics", summary="论文按主题分组（引用图谱社区）")
async def papers_topics() -> dict:
    """按引用图谱的 Louvain 社区把论文分组，附带每组的真实标题。

    只读聚合：社区号复用 `app/graph.py: communities()`，与引用图谱页同一套算法；
    标题取自 `data/meta/structures.json`。不重新聚类、不调大模型。

    返回 `page_size`，前端据此做分页，避免前后端各写一个魔法数字。
    """
    from app.paper_topics import PAGE_SIZE

    filenames = [p.filename for p in list_papers()]
    groups = _topic_groups(filenames)
    return {
        "total": sum(len(g.papers) for g in groups),
        "page_size": PAGE_SIZE,
        "topics": [
            {
                "topic_id": g.topic_id,
                "name": g.name,
                "count": len(g.papers),
                "papers": [
                    {
                        "filename": p.filename,
                        "title": p.title,
                        "display_title": p.display_title,
                        "truncated": p.truncated,
                        "cited_by": p.cited_by,
                        "node_id": p.node_id,
                    }
                    for p in g.papers
                ],
            }
            for g in groups
        ],
    }


def _topic_groups(filenames: list[str]):
    from app.paper_topics import build_topics

    try:
        return build_topics(filenames)
    except Exception as exc:  # noqa: BLE001 - 分组失败退化成单组，别让页面打不开
        logger.warning("论文主题分组失败，退化成「全部」单组：%s", exc, exc_info=True)
        return []


@app.post("/papers/upload", summary="上传论文（支持批量，自动查重）")
async def papers_upload(
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
) -> dict:
    """上传一篇或一批 PDF，逐个落盘、解析、查重。

    重复不拒绝，而是回 `duplicate`（含依据与版本号），由前端弹窗让用户选
    「保留新版删旧版 / 两个都留」—— 赛题 1.1.1 要求的就是这个交互。

    兼容两种提交方式：`files`（批量，重复字段）与 `file`（老的单文件）。
    """
    uploads = [f for f in (files or []) if f and f.filename]
    if file is not None and file.filename:
        uploads.append(file)
    if not uploads:
        raise HTTPException(status_code=400, detail="没有收到文件")

    results: list[dict] = []
    for item in uploads:
        name = item.filename or ""
        content = await item.read()
        try:
            paper, dup, score = save_paper(name, content)
        except ValueError as exc:
            if len(uploads) == 1:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            results.append({"filename": name, "error": str(exc)})
            continue

        # 保存成功后立刻入库（切块 → 向量 → BM25），否则会出现"上传了但 AI 检索不到"。
        # 入库失败不让整个上传 500：文件已经存好了，返回 indexed=0 提示稍后重建。
        indexed = 0
        try:
            path = paper_path(paper.paper_id)
            if path is not None:
                indexed = index_paper(str(path), paper.filename)
        except Exception:
            logger.exception("论文已保存，但入库失败（paper_id=%s）", paper.paper_id)

        duplicate = None
        if dup is not None:
            old_version = version_of(dup.filename)
            new_version = version_of(paper.filename)
            duplicate = {
                "paper_id": dup.paper_id,
                "filename": dup.filename,
                "score": score,
                "reason": paper.duplicate_reason or "内容相同",
                "old_version": old_version,
                "new_version": new_version,
                # 版本号大的算新版；拿不到版本号时不下结论
                "newer": bool(new_version and old_version and new_version > old_version),
            }

        results.append(
            {
                "filename": paper.filename,
                "paper": PaperOut(**vars(paper)).model_dump(),
                "indexed": indexed,
                "duplicate": duplicate,
            }
        )

    saved = [r for r in results if "paper" in r]
    dups = [r for r in saved if r.get("duplicate")]
    body: dict = {
        "total": len(uploads),
        "saved": len(saved),
        "duplicates": len(dups),
        "results": results,
    }
    if len(uploads) == 1 and saved:
        # 单文件的老接口形态（老前端/老测试只读这三个字段）
        body.update(paper=saved[0]["paper"], indexed=saved[0]["indexed"], duplicate=saved[0]["duplicate"])
    if not saved and len(uploads) > 1:
        raise HTTPException(status_code=400, detail="一个文件都没存成功")
    return body


@app.delete("/papers/{paper_id}", summary="删除论文")
async def papers_delete(paper_id: str) -> dict:
    paper = get_paper(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if not delete_paper(paper_id):
        raise HTTPException(status_code=500, detail="删除失败")

    # 文件删掉后必须同步清索引，否则"删了还能被检索到"（赛题实测会翻车）
    removed_chunks = 0
    try:
        removed_chunks = remove_source(paper.filename)
        delete_structure(paper.filename)
    except Exception:
        logger.exception("论文文件已删，但清理索引失败（source=%s）", paper.filename)

    return {"deleted": paper_id, "removed_chunks": removed_chunks}


# ---------------- 阅读页（1.3.2）----------------
# 聊天页证据卡片点「打开原文」→ /ui/reader.html?paper=xxx.pdf&page=8
# 阅读页需要两样东西：PDF 原件（浏览器渲染）和章节树（左侧导航）。

@app.get("/reader/papers", summary="阅读页可选的论文清单")
async def reader_papers() -> dict:
    items = list_reader_papers()
    return {"total": len(items), "papers": items}


@app.get("/paper/{filename}/structure", summary="阅读页章节树（标题/作者/章节页码）")
async def paper_structure(filename: str) -> dict:
    meta = reader_meta(filename)
    if meta is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return meta


@app.get("/paper/{filename}/layout", summary="原文版式判定（单栏/双栏，赛题 1.3.1）")
async def paper_layout(filename: str) -> dict:
    """逐页判定单双栏。

    前端顶栏的版式徽章和「按栏看」视图都靠它：双栏页按一栏宽度放大，
    才能像读单栏一样顺着往下读，不用横向来回扫。
    """
    path = safe_paper_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return detect_layout(path)


@app.get("/paper/{filename}/figure/{index}", summary="论文里的图表（按需裁成 PNG）")
async def paper_figure(filename: str, index: int) -> Response:
    """把 structures.json 里第 index 张图从 PDF 里裁出来。

    为什么按需裁：全库预裁一次要几分钟，而用户一次只看一张。裁完缓存在
    data/meta/figures/<stem>/fig<index>.png，同一张图第二次是直接读文件。
    """
    path = safe_paper_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    figures = (load_structures().get(filename) or {}).get("figures") or []
    if not (0 <= index < len(figures)):
        raise HTTPException(status_code=404, detail="没有这张图")
    fig = figures[index]
    page_no = int(fig.get("page") or 0)
    data = figure_bytes(path, path.stem, index, page_no, str(fig.get("caption") or ""))
    crop = "figure"
    if data is None:
        # 矢量图（没有位图块）裁不出来 → 退回整页预览，并告诉前端这是整页
        data = page_png(path, page_no)
        crop = "page"
    if data is None:
        raise HTTPException(status_code=404, detail="这张图没能从 PDF 里裁出来")
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Figure-Crop": crop,
        },
    )


@app.get("/paper/{filename}", summary="论文原文件 PDF（阅读页渲染用）")
async def paper_file(filename: str) -> FileResponse:
    """返回 data/papers 下的 PDF。

    安全性交给 safe_paper_path：只认 data/papers 里的裸文件名，
    `../`、绝对路径、非 .pdf 一律 404（不返回 403，避免暴露目录结构）。
    FileResponse 自带 Range 支持，PDF.js 可以按需分段加载大文件。
    """
    path = safe_paper_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------- 论文翻译（1.3.3）----------------
# 1.3.3.1 部分翻译：POST /translate/selection（选中一段直接译）
# 1.3.3.2 整篇翻译：GET /paper/{f}/translate（SSE 逐页返回，按页缓存）
# 1.3.3.3 中英对比：前端拿"页"当锚点做双向滚动同步，翻译结果也按页存

class TranslateSelectionRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=6000, description="要翻译的原文")
    context: str = Field(default="", max_length=600, description="可选上下文（帮助统一术语）")


@app.get("/paper/{filename}/translation", summary="已缓存的译文（按页）")
async def paper_translation(filename: str) -> dict:
    """返回缓存里已经翻好的页；一页都没翻过时 pages 为空对象。"""
    if safe_paper_path(filename) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    data = load_translation(filename)
    pages = data.get("pages") or {}
    ordered = {str(k): v for k, v in sorted(pages.items(), key=lambda kv: int(kv[0]))}
    return {
        "filename": filename,
        "model": data.get("model"),
        "updated_at": data.get("updated_at"),
        "total": len(ordered),
        "references_page": references_page_of(filename),
        "pages": ordered,
    }


@app.get("/paper/{filename}/translate", summary="论文翻译（SSE 逐页流式返回）")
async def paper_translate(filename: str, pages: str = "all", force: bool = False) -> StreamingResponse:
    """逐页翻译并通过 SSE 推送。

    pages=all        → 全文；默认跳过 References 及其后的页面（翻参考文献性价比很低）
    pages=1,3,5      → 只翻这几页（1 基）
    force=true       → 无视缓存重译（改过提示词时才需要）
    """
    path = safe_paper_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if not settings.llm_api_key or settings.llm_api_key.startswith("sk-在这里"):
        raise HTTPException(status_code=503, detail="还没有配置 LLM_API_KEY，无法翻译")

    ref_page = references_page_of(filename)
    if pages.strip().lower() in ("all", "", "*"):
        with fitz.open(path) as doc:
            total_pages = len(doc)
        limit = ref_page - 1 if ref_page and ref_page > 1 else total_pages
        wanted: list[int] | None = list(range(1, limit + 1))
    else:
        wanted = []
        for piece in pages.split(","):
            piece = piece.strip()
            if piece.isdigit():
                wanted.append(int(piece))
        if not wanted:
            raise HTTPException(status_code=400, detail="pages 参数看不懂，用 all 或 1,3,5 这种页码")

    async def event_generator():
        try:
            async for event in translate_pages(filename, path, pages=wanted, force=force):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("翻译失败: %s", filename)
            err = {"type": "error", "message": "翻译失败，请稍后重试"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/translate/selection", summary="翻译一段选中的文字（部分翻译）")
async def translate_selection(req: TranslateSelectionRequest) -> dict:
    if not settings.llm_api_key or settings.llm_api_key.startswith("sk-在这里"):
        raise HTTPException(status_code=503, detail="还没有配置 LLM_API_KEY，无法翻译")
    text = req.text.strip()
    if req.context:
        text = f"（上下文，仅供术语参考，不要翻译它）\n{req.context}\n\n{text}"
    try:
        markdown = await translate_text(text)
    except Exception:
        logger.exception("选中翻译失败")
        raise HTTPException(status_code=502, detail="翻译服务暂时不可用") from None
    return {"markdown": markdown}


# ---------------- 1.4 下载与推荐 ----------------
# 赛题 1.4.1 的两个入口：按论文名下载、顺着参考文献批量下载；
# 1.4.3 的推荐列表与反馈。联网的部分全部丢到线程池（httpx 是同步的），
# 免得一个 20 秒的 arXiv 搜索把整个事件循环卡住。


class DownloadRequest(BaseModel):
    """按论文名下载：`query` 是标题或"标题 + arXiv 号"。"""

    query: str = Field(..., min_length=1, max_length=300)
    force: bool = False


class ReferenceDownloadRequest(BaseModel):
    """批量下载参考文献。`min_cites` 就是"去掉为了引用而引用"的阈值。"""

    limit: int = Field(default=3, ge=1, le=20)
    min_cites: int = Field(default=2, ge=1, le=30)


class FeedbackRequest(BaseModel):
    """推荐反馈：up / down，传空串表示取消。"""

    vote: str = Field(default="", max_length=8)
    # 1.4.3 ④：不同人各记各的反馈，协同过滤才有输入
    user: str = Field(default=DEFAULT_USER, max_length=40)

def _pick_search_result(query: str, results: list[dict]) -> dict | None:
    """从 arXiv 搜索结果里挑最像 query 的那条。

    arXiv 的"按标题搜"并不精确（搜 Layer Normalization 会先给别的论文），
    所以这里按标题相似度重排，低于 0.5 视为没搜到，宁可报错也不乱下。
    """
    if not results:
        return None

    def ratio(candidate: dict) -> float:
        left = query.lower().strip()
        right = str(candidate.get("title") or "").lower().strip()
        return difflib.SequenceMatcher(None, left, right).ratio()

    best = max(results, key=ratio)
    if ratio(best) < 0.5:
        return None
    return best


def _looks_like_paper(path: Path, title: str, min_pages: int = 4) -> bool:
    """下载下来的东西到底是不是论文。

    只挡两种真实发生过的情况：下回来的是 HTML 错误页（不是 PDF），
    以及 PDF 页数过少 / 首页文字与标题完全对不上（可能是别人的附件）。
    标题对不上时用相似度放宽到 0.5，因为 arXiv 标题常带换行与连字符。
    """
    try:
        with fitz.open(path) as doc:
            pages = doc.page_count
            if pages < min_pages:
                return False
            head = (doc[0].get_text() or "")[:2000]
    except Exception:
        return False
    if not title.strip():
        return True
    wanted = re.sub(r"\s+", " ", title.lower()).strip()
    got = re.sub(r"\s+", " ", head.lower())
    if wanted in got:
        return True
    # 标题常被换行/连字符切开："attention is all you\nneed" → 去掉空白再比
    squeezed_wanted = re.sub(r"[^a-z0-9]", "", wanted)
    squeezed_got = re.sub(r"[^a-z0-9]", "", got)
    if squeezed_wanted and squeezed_wanted in squeezed_got:
        return True
    return difflib.SequenceMatcher(None, wanted[:120], got[:400]).ratio() >= 0.5


@app.post("/library/download", summary="按论文名从 arXiv 下载并入库（1.4.1）")
async def library_download(req: DownloadRequest) -> dict:
    """给一个论文名 → 在 arXiv 搜 → 下 PDF → 走上传同一条链路（去重/解析/入库）。

    重复时不重复下载：库里已有同一 arXiv 编号就直接告诉前端"已有"，
    除非显式 `force=true`。
    """
    query = req.query.strip()
    direct = arxiv_id_of(query) if re.fullmatch(r"[\w./:\-]*\d{4}\.\d{4,5}[\w.]*", query) else ""
    if direct:
        # 直接给 arXiv 号（推荐卡片上「下载入库」就是这么调的），不用再搜
        try:
            meta = await run_in_threadpool(paper_meta, direct)
        except OnlineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        best = {"arxiv_id": direct, "title": meta.get("title") or f"arXiv:{direct}"}
    else:
        try:
            results = await run_in_threadpool(search_papers, query, 6)
        except OnlineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        best = _pick_search_result(query, results)
        if best is None:
            raise HTTPException(status_code=404, detail=f"arXiv 上没搜到接近「{query}」的论文")
    arxiv_id = best["arxiv_id"]

    existing = next((row for row in library_rows() if row["arxiv_id"] == arxiv_id), None)
    if existing is not None and not req.force:
        return {
            "downloaded": False,
            "detail": "库里已经有这篇论文",
            "arxiv_id": arxiv_id,
            "title": existing["title"],
            "filename": existing["filename"],
        }

    # 临时文件必须放论文目录外面：放里面会被 sync_index() 的 glob 当论文登记成
    # 幽灵条目（`.download_xxx`），然后真正入库那份反而被判成"重复"。
    tmp_dir = PAPERS_DIR.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{arxiv_id}.part"
    try:
        await run_in_threadpool(download_pdf, arxiv_id, tmp)
        if not _looks_like_paper(tmp, str(best.get("title") or "")):
            # arXiv 偶尔回 HTML 错误页或摘要页；宁可拒收，也不能让垃圾进论文库
            raise HTTPException(
                status_code=422,
                detail="下载到的不是有效论文（页数太少或标题对不上），已丢弃",
            )
        data = tmp.read_bytes()
    except OnlineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    finally:
        tmp.unlink(missing_ok=True)

    paper, dup, score = await run_in_threadpool(save_paper, f"{arxiv_id}.pdf", data)
    # 结构写得进总表，阅读页才有章节树（总表是按文件名索引的）
    structure = get_structure(paper.paper_id)
    if structure:
        merge_structure(paper.filename, structure)

    indexed = 0
    try:
        path = paper_path(paper.paper_id)
        if path is not None:
            indexed = await run_in_threadpool(index_paper, str(path), paper.filename)
    except Exception:
        logger.exception("下载成功但入库失败（paper_id=%s）", paper.paper_id)

    return {
        "downloaded": True,
        "arxiv_id": arxiv_id,
        "title": best["title"],
        "paper": {
            "paper_id": paper.paper_id,
            "filename": paper.filename,
            "pages": paper.pages,
            "duplicate_of": paper.duplicate_of,
            "duplicate_reason": paper.duplicate_reason,
        },
        "duplicate_score": score,
        "duplicate_hit": dup.filename if dup else "",
        "indexed": indexed,
    }


@app.post("/paper/{filename}/references/download", summary="批量下载参考文献入库（1.4.1）")
async def paper_references_download(filename: str, req: ReferenceDownloadRequest) -> dict:
    """按正文引用次数挑重点参考文献下载。

    顺序对齐的前提：Semantic Scholar 返回的引用列表顺序 ≈ 论文里的编号顺序，
    所以用 `[n]` 的计数去给第 n 条加权。对不齐时最坏是"重点挑偏"，不会下错文件
    （下的是 S2 给的那个 arXiv 号）。
    """
    path = safe_paper_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    arxiv_id = arxiv_id_of(filename)
    if not arxiv_id:
        raise HTTPException(status_code=400, detail="这篇论文不是 arXiv 来源，取不到参考文献关系")

    try:
        rows = await run_in_threadpool(references, arxiv_id, 40)
    except OnlineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    text = ""
    try:
        with fitz.open(str(path)) as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception:
        logger.exception("数引用次数时读 PDF 失败：%s", filename)
    counts = citation_counts(text)

    ranked: list[dict] = []
    for index, row in enumerate(rows):
        # S2 列表第 1 条 ≈ 正文 [1]
        count = counts.get(index + 1, 0)
        if count < req.min_cites:
            continue
        if not row.get("arxiv_id"):
            continue
        item = dict(row)
        item["cites"] = count
        ranked.append(item)
    ranked.sort(key=lambda item: item["cites"], reverse=True)

    known = {row["arxiv_id"] for row in library_rows() if row["arxiv_id"]}
    picked = ranked[: req.limit]
    results: list[dict] = []
    for item in picked:
        ref_id = item["arxiv_id"]
        if ref_id in known:
            results.append({"arxiv_id": ref_id, "title": item["title"], "status": "已有", "cites": item["cites"]})
            continue
        tmp = PAPERS_DIR / f".download_{ref_id}.pdf"
        try:
            await run_in_threadpool(download_pdf, ref_id, tmp)
            data = tmp.read_bytes()
            paper, dup, _score = await run_in_threadpool(save_paper, f"{ref_id}.pdf", data)
            structure = get_structure(paper.paper_id)
            if structure:
                merge_structure(paper.filename, structure)
            indexed = 0
            try:
                saved_path = paper_path(paper.paper_id)
                if saved_path is not None:
                    indexed = await run_in_threadpool(index_paper, str(saved_path), paper.filename)
            except Exception:
                logger.exception("参考文献入库失败：%s", ref_id)
            results.append(
                {
                    "arxiv_id": ref_id,
                    "title": item["title"],
                    "status": "已入库",
                    "cites": item["cites"],
                    "filename": paper.filename,
                    "indexed": indexed,
                    "duplicate_hit": dup.filename if dup else "",
                }
            )
            known.add(ref_id)
        except (OnlineError, ValueError) as exc:
            results.append({"arxiv_id": ref_id, "title": item["title"], "status": "失败", "cites": item["cites"], "error": str(exc)})
        finally:
            tmp.unlink(missing_ok=True)

    return {
        "filename": filename,
        "arxiv_id": arxiv_id,
        "min_cites": req.min_cites,
        "candidates": len(ranked),
        "counts": dict(sorted(counts.items())),
        "results": results,
    }


@app.get("/recommendations", summary="新论文推荐（读缓存，1.4.3）")
async def recommendations(user: str = DEFAULT_USER) -> dict:
    """读上次刷新出来的推荐；没刷新过就返回空列表 + offline=true。

    ``user`` 只影响"这条我点过赞没"的投票状态与协同画像，不改动已算好的 base 分。
    """
    payload = await run_in_threadpool(cached_recommendations)
    items = await run_in_threadpool(rerank_with_feedback, list(payload.get("items") or []), load_feedback(user))
    payload["items"] = items
    payload["user"] = user
    payload["stats"] = {
        "votes": load_feedback(user),
    }
    return payload


@app.post("/recommendations/refresh", summary="刷新推荐（扫 arXiv 引用关系 + 近期上架，1.4.3）")
async def recommendations_refresh(
    max_papers: int = 6,
    include_recent: bool = True,
    user: str = DEFAULT_USER,
    period: str = "daily",
) -> dict:
    """实时抓一轮候选并重算推荐；抓取失败时退回过期缓存（offline=true）。

    两路候选：``cited``（别人引用了库里的论文）+ ``recent``（arXiv 近期上架，
    ``include_recent=false`` 可关掉）。``period=daily|weekly`` 只影响新上架的时间窗。
    这一步会串行请求 Semantic Scholar 与 arXiv，通常 20~60 秒。
    """
    try:
        return await build_recommendations(
            max_papers=max(1, min(max_papers, 12)),
            include_recent=include_recent,
            user=user,
            period=period,
        )
    except OnlineError as exc:
        payload = await run_in_threadpool(cached_recommendations)
        payload["offline"] = True
        payload["detail"] = str(exc)
        return payload


@app.post("/recommendations/{key}/feedback", summary="推荐反馈（👍/👎，1.4.3）")
async def recommendation_feedback(key: str, req: FeedbackRequest) -> dict:
    """记一条反馈，并立刻按新票重排缓存里的列表（不用重新抓网）。

    `key` 是推荐条目里的 `key` 字段（有 arXiv 号就用号，没有就用标题哈希）。
    `user` 让不同人各记各的（1.4.3 ④ 多用户）。
    """
    if not key.strip():
        raise HTTPException(status_code=400, detail="缺少推荐条目标识")
    user = (req.user or DEFAULT_USER).strip() or DEFAULT_USER
    try:
        feedback = await run_in_threadpool(save_feedback, key, req.vote, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    payload = await run_in_threadpool(cached_recommendations)
    items = rerank_with_feedback(list(payload.get("items") or []), feedback)
    payload["items"] = items
    payload["user"] = user
    payload["offline"] = False
    write_cache(CACHE_KEY, payload)
    try:
        RECOMMEND_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        logger.warning("写推荐缓存文件失败（不影响本次响应）")
    return {"vote": req.vote, "user": user, "feedback": feedback, "items": items}



@app.get("/recommendations/schedule", summary="定时刷新状态（1.4.2）")
async def recommendations_schedule(enable: bool = False, mode: str = "") -> dict:
    """看定时器状态；``?enable=true`` 可现场启动（``mode=daily|weekly|off``）。"""
    if enable:
        return await run_in_threadpool(scheduler.start, mode or None)
    return scheduler.status()


@app.get("/graph", summary="论文引用关系图谱（读缓存，1.5.1 / 1.5.2）")
async def citation_graph(limit_reviews: int = 60) -> dict:
    """返回库内论文的引用图谱，供 `web/graph.html` 画力导向图。

    数据来自 `data/meta/citation-graph.json`（由 `scripts/build_graph.py` 生成）。
    本接口只做读 + 派生（社区、中介中心性、阅读页跳转链接），不重跑 PDF 解析。

    缓存不存在时返回 `ready=false` 并带上生成命令，前端据此提示用户先跑一次构建。
    """
    raw = await run_in_threadpool(load_graph)
    if not raw:
        return {
            "ready": False,
            "detail": "图谱缓存还没生成，请先运行 python scripts/build_graph.py",
            "summary": {},
            "nodes": [],
            "edges": [],
            "reviews": [],
            "reviews_total": 0,
            "merge_decisions": [],
        }
    view = await run_in_threadpool(graph_view, raw, max(0, min(limit_reviews, 500)))
    view["ready"] = True
    return view


@app.get("/graph/survey", summary="自动综述（1.5.3）")
async def graph_survey(focus: str = "") -> dict:
    """基于引用图谱自动生成领域综述。

    `focus` 传某个主题名（如「模型架构」）时只综述该主题，不传则综述全库。
    每一句都带具体数字和论文标题，可回溯到图上，不调外部大模型也不会有幻觉引用。
    """
    raw = await run_in_threadpool(load_graph)
    if not raw:
        return {"ready": False, "detail": "图谱缓存还没生成，请先运行 python scripts/build_graph.py"}
    view = await run_in_threadpool(graph_view, raw)
    return await run_in_threadpool(survey, view, focus)


@app.get("/graph/frontier", summary="前沿探索（1.5.4）")
async def graph_frontier(limit: int = 8) -> dict:
    """给出值得关注的研究方向，每条都附可核验的量化依据。

    三条线索：新近且被引 / 跨社区桥接 / 未被接入引用网。
    """
    raw = await run_in_threadpool(load_graph)
    if not raw:
        return {"ready": False, "detail": "图谱缓存还没生成，请先运行 python scripts/build_graph.py"}
    view = await run_in_threadpool(graph_view, raw)
    return await run_in_threadpool(frontier, view, max(1, min(limit, 50)))


# ---------------- 写作助手（1.6） ----------------


@app.get("/writing/renderers", summary="本机可用的架构图渲染器（1.6.2）")
async def writing_renderers() -> dict:
    """前端据此灰掉不可用的渲染选项，而不是让用户点了才报错。"""
    return renderer_available()


@app.post("/writing/framework", summary="论文框架生成（1.6.1）")
async def writing_framework(req: FrameworkRequest) -> dict:
    """给定 idea 生成 Abstract / Introduction / Related Work / References。

    参考文献由代码从候选池里取，模型碰不到——这是"无幻觉引用"的保证方式。
    """
    try:
        return await generate_framework(req.idea, total_candidates=req.candidates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/writing/diagram", summary="架构图生成（1.6.2）")
async def writing_diagram(req: DiagramRequest) -> dict:
    """一句话描述 → Mermaid / Graphviz 代码 → 本地渲染成 PNG。"""
    try:
        return await generate_diagram(req.description, req.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/writing/paper-figures", summary="从库内 PDF 找已有架构图（1.6.2 参考）")
async def writing_paper_figures(paper: str, keyword: str = "architecture", limit: int = 6) -> dict:
    """按论文名从 PDF 里找架构图。

    这是"引用真实架构图"而不是"让模型画"——评委问图哪来的，能指回原文第几页。
    """
    items = await run_in_threadpool(find_paper_figures, paper, keyword, max(1, min(limit, 20)))
    return {"paper": paper, "keyword": keyword, "figures": items}


@app.post("/writing/chart", summary="CSV 数据可视化（1.6.3）")
async def writing_chart(file: UploadFile, kind: str = "", note: str = "") -> dict:
    """上传 CSV，返回 ECharts 配置 + 导出的 PNG + 自动写的英文 caption。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的 CSV 是空的")
    try:
        return await make_chart(raw, kind=kind, extra=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/writing/chart/{name}", summary="导出的图表 PNG（1.6.3）")
async def writing_chart_png(name: str) -> FileResponse:
    path = Path("data") / "meta" / "charts" / Path(name).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="图表不存在")
    return FileResponse(path, media_type="image/png")


@app.get("/writing/diagram/{name}", summary="渲染出的架构图 PNG（1.6.2）")
async def writing_diagram_png(name: str) -> FileResponse:
    path = Path("data") / "meta" / "diagrams" / Path(name).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="图不存在")
    return FileResponse(path, media_type="image/png")


@app.post("/writing/zh2en", summary="学术中译英（1.6.4）")
async def writing_zh2en(req: Zh2EnRequest) -> dict:
    """中文论文片段 → 可投稿的英文表达。术语表可选，用于保证全文一致。"""
    glossary = parse_glossary(req.glossary)
    try:
        return await translate_zh2en(req.text, glossary)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------- 设置中心 ----------------
# 只暴露审计确认「改了立即生效」的项；API Key 在响应里永远是掩码。


class SettingsUpdateRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    value: str = Field(default="", max_length=400)


class IndexCleanupRequest(BaseModel):
    """清理幽灵索引的入参。sources 为空表示由后端自己判定全部幽灵。"""

    sources: list[str] = Field(default_factory=list, max_length=500)


@app.get("/settings", summary="读取全部设置（API Key 已脱敏）")
async def settings_read() -> dict:
    return await run_in_threadpool(settings_api.snapshot)


@app.get("/settings/about", summary="项目信息（技术栈与版本）")
async def settings_about() -> dict:
    return settings_api.about()


@app.post("/settings", summary="保存一项设置")
async def settings_save(req: SettingsUpdateRequest) -> dict:
    try:
        result = await run_in_threadpool(settings_api.save, req.key, req.value)
    except settings_api.SettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改了客户端相关配置，旧客户端必须丢掉，否则新 key/base_url 不生效
    item = settings_api.SETTINGS.get(req.key)
    if item is not None and item.needs_client_reset:
        await llm.reset_client_async()
    return {**result, "settings": settings_api.snapshot()}


@app.post("/settings/restore", summary="单项恢复默认值")
async def settings_restore(req: SettingsUpdateRequest) -> dict:
    try:
        result = await run_in_threadpool(settings_api.restore_default, req.key)
    except settings_api.SettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "settings": settings_api.snapshot()}


@app.post("/settings/test-connection", summary="测试模型连接")
async def settings_test_connection() -> dict:
    probe = await settings_api.test_connection()
    return {
        "ok": probe.ok,
        "model": probe.model,
        "base_url": probe.base_url,
        "latency_ms": probe.latency_ms,
        "detail": probe.detail,
        "error": probe.error,
    }


@app.get("/settings/schedule", summary="定时刷新状态")
async def settings_schedule() -> dict:
    return await run_in_threadpool(scheduler.status)


@app.post("/settings/schedule", summary="应用定时刷新设置")
async def settings_schedule_apply(req: SettingsUpdateRequest) -> dict:
    """先写配置、再动运行时；运行时起不来就把配置回滚。

    这条路径专门对付「界面显示成功、后台其实没启动」：
    只要 scheduler.start() 返回的 running 为 False，一律判失败并回滚。
    """
    key = req.key or "DISCOVER_SCHEDULE"
    if key not in ("DISCOVER_SCHEDULE", "DISCOVER_HOUR"):
        raise HTTPException(status_code=400, detail=f"不支持的配置项: {key}")

    before = settings_api.snapshot()
    old_mode = str(before["groups"]["schedule"][0]["value"])
    try:
        settings_api.save("DISCOVER_SCHEDULE", req.value)
    except settings_api.SettingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mode = req.value.strip().lower()
    info = await run_in_threadpool(scheduler.start, mode)

    running = bool(info.get("running"))
    if mode != "off" and not running:
        # 运行时没起来：把磁盘和内存都退回改动前
        logger.error("定时器启动失败，回滚为 %s", old_mode)
        settings_api.save("DISCOVER_SCHEDULE", old_mode)
        await run_in_threadpool(scheduler.start, old_mode)
        raise HTTPException(status_code=500, detail="定时器未能启动，已回滚为原来的设置")

    return {"ok": True, "mode": info.get("mode"), "running": running,
            "next_run": info.get("next_run") or "", "settings": settings_api.snapshot()}


@app.get("/settings/index/health", summary="索引健康检查（只读）")
async def settings_index_health() -> dict:
    report = await run_in_threadpool(kb_health.health_check)
    return report.to_dict()


@app.post("/settings/index/cleanup", summary="清理幽灵索引（三路同步）")
async def settings_index_cleanup(req: IndexCleanupRequest | None = None) -> dict:
    """只清理磁盘上已不存在的 source；有效论文一律跳过（在 cleanup_ghosts 里保证）。"""
    sources: list[str] | None = None
    if req is not None and req.sources:
        sources = [s.strip() for s in req.sources if s and s.strip()]
    result = await run_in_threadpool(kb_health.cleanup_ghosts, sources)
    verify = await run_in_threadpool(kb_health.verify_consistency)
    return {"result": result.to_dict(), "verify": verify}


# ---------------- 前端页面 ----------------
# 把 web/ 目录挂到 /ui，这样访问 http://127.0.0.1:8000/ui/ 就能看到聊天页面
if Path("web").exists():
    app.mount("/ui", StaticFiles(directory="web", html=True), name="web")

# ---------------- 定时刷新推荐（1.4.2） ----------------
# 默认每天跑一次；`DISCOVER_SCHEDULE=off` 可关掉，`weekly` 改每周一。
# pytest 运行时不会启动（见 app/scheduler.py 的 autostart）。
try:
    _SCHEDULE_INFO = scheduler.autostart()
    if _SCHEDULE_INFO:
        logger.info("定时推荐刷新：%s，下次 %s", _SCHEDULE_INFO["mode"], _SCHEDULE_INFO["next_run"])
except Exception as _exc:  # 定时器起不来不影响主服务
    logger.warning("定时推荐刷新启动失败：%s", _exc)


# 直接 `python -m app.main` 时走这里
if __name__ == "__main__":
    import uvicorn

    logger.info("服务启动中: http://127.0.0.1:%d", settings.app_port)
    logger.info("接口文档: http://127.0.0.1:%d/docs", settings.app_port)
    logger.info("聊天页面: http://127.0.0.1:%d/ui/", settings.app_port)

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
