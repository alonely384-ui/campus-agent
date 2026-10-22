"""FastAPI 入口：把 Agent 包装成一个 HTTP 服务。

启动方式（两种都行）：
    python -m app.main          ← 直接跑
    uvicorn app.main:app --reload   ← 开发时用这个，改代码自动重启

启动后打开 http://127.0.0.1:8000/docs 就能看到自动生成的接口文档。
"""

import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import run_agent, run_agent_stream
from app.config import settings
from app.logging_config import get_logger, setup_logging
from app.papers import (
    delete_paper,
    get_paper,
    list_papers,
    save_paper,
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


class ChatResponse(BaseModel):
    answer: str
    rounds: int
    steps: list[dict] = Field(default_factory=list)


# ---------------- 接口 ----------------
@app.get("/health", summary="健康检查")
async def health() -> dict:
    """部署后用这个接口判断服务是不是活着。Nginx / Docker 都靠它做探活。"""
    return {"status": "ok", "model": settings.llm_model, "version": "0.1.0"}


@app.post("/chat", response_model=ChatResponse, summary="对话（一次性返回）")
async def chat(req: ChatRequest) -> ChatResponse:
    """最简单的一问一答。前端想要"打字机效果"请用 /chat/stream。"""
    try:
        result = await run_agent(req.message, req.history, source=req.source)
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
            async for event in run_agent_stream(req.message, req.history, source=req.source):
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


@app.post("/papers/upload", summary="上传论文（自动查重）")
async def papers_upload(file: UploadFile = File(...)) -> dict:
    """上传 PDF 并返回是否和库里已有的论文重复。

    重复不会拒绝，而是返回 duplicate_of 让前端提示「与 xxx 相似，保留哪一版？」
    —— 赛题要求就是这个行为。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    content = await file.read()
    try:
        paper, dup, score = save_paper(file.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "paper": PaperOut(**vars(paper)),
        "duplicate": (
            {"paper_id": dup.paper_id, "filename": dup.filename, "score": score}
            if dup
            else None
        ),
    }


@app.delete("/papers/{paper_id}", summary="删除论文")
async def papers_delete(paper_id: str) -> dict:
    if get_paper(paper_id) is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    if not delete_paper(paper_id):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"deleted": paper_id}


# ---------------- 前端页面 ----------------
# 把 web/ 目录挂到 /ui，这样访问 http://127.0.0.1:8000/ui/ 就能看到聊天页面
if Path("web").exists():
    app.mount("/ui", StaticFiles(directory="web", html=True), name="web")


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
