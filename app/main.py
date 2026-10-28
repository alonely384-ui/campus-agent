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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import scheduler
from app.agent import run_agent, run_agent_stream
from app.config import settings
from app.figures import figure_bytes, page_png
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
    collaborative_scores,
    library_rows,
    load_events,
    load_feedback,
    record_event,
    rerank_with_feedback,
    save_feedback,
)
from app.translate import (
    load_translation,
    references_page_of,
    translate_pages,
    translate_text,
)

setup_logging()
logger = get_logger(__name__)

app = FastAPI(
    title="Campus Agent",
    description="校园智能助手：RAG 问答 + 工具调用 + 流式输出",
    version="0.1.0",
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


class ChatResponse(BaseModel):
    answer: str
    rounds: int
    steps: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list, description="检索证据（证据卡片）")


# ---------------- 接口 ----------------
@app.get("/health", summary="健康检查")
async def health() -> dict:
    """部署后用这个接口判断服务是不是活着。Nginx / Docker 都靠它做探活。"""
    return {"status": "ok", "model": settings.llm_model, "version": "0.1.0"}


@app.post("/chat", response_model=ChatResponse, summary="对话（一次性返回）")
async def chat(req: ChatRequest) -> ChatResponse:
    """最简单的一问一答。前端想要"打字机效果"请用 /chat/stream。"""
    try:
        result = await run_agent(
            req.message, req.history, source=req.source, sources=req.sources
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
                req.message, req.history, source=req.source, sources=req.sources
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
    items = list_papers()
    return {"total": len(items), "papers": [PaperOut(**vars(p)) for p in items]}


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


class EventRequest(BaseModel):
    """点击/下载事件（1.4.3 ③）：kind=view|download。"""

    kind: str = Field(default="view", max_length=12)
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
        "events": load_events(),
        "collab_keys": len(collaborative_scores(user)),
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


@app.post("/recommendations/{key}/event", summary="记录点击/下载（1.4.3 ③）")
async def recommendation_event(key: str, req: EventRequest) -> dict:
    """记一次点击或下载；返回累计计数。

    ``key.download`` 只在用户真的点了"下载 PDF"时记，页面渲染记 ``view``。
    计数参与排序（权重 0.05，影响很小，只当微调）。
    """
    if not key.strip():
        raise HTTPException(status_code=400, detail="缺少推荐条目标识")
    try:
        row = await run_in_threadpool(record_event, key, req.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"key": key, "kind": req.kind, "events": row}


@app.get("/recommendations/schedule", summary="定时刷新状态（1.4.2）")
async def recommendations_schedule(enable: bool = False, mode: str = "") -> dict:
    """看定时器状态；``?enable=true`` 可现场启动（``mode=daily|weekly|off``）。"""
    if enable:
        return await run_in_threadpool(scheduler.start, mode or None)
    return scheduler.status()


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
