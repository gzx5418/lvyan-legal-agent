"""FastAPI + SSE API 入口。

端点
----
- ``POST /api/agent/run`` —— 异步启动 Agent 运行
- ``GET  /api/agent/stream/{run_id}`` —— SSE 流式返回节点执行进度
- ``GET  /api/agent/state/{thread_id}`` —— 返回 CaseState 摘要（不含敏感字段）
- ``DELETE /api/agent/state/{thread_id}`` —— 删除会话
- ``GET  /api/agent/threads`` —— 列出所有会话摘要
- ``POST /api/agent/hitl/{run_id}`` —— Human-in-the-loop 响应
- ``GET  /api/health`` —— 健康检查
- ``POST /api/upload`` —— 文件上传

实现策略
--------
- Agent 运行通过 ``asyncio.create_task`` 异步执行（见 :class:`RunManager`）。
- SSE 流使用 FastAPI 内置 ``StreamingResponse``（``text/event-stream``）。
- HITL 通过 LangGraph ``interrupt()`` + ``Command(resume=...)`` 恢复执行。
- ``create_app`` 接受可注入的 ``runner`` 与 ``memory``，便于测试隔离。
- 文件上传存到 ``settings.data_dir / uploads``，返回 file_id 供 /api/agent/run 引用。

PR1 改进
--------
- 状态源统一为 ``CaseMemory``（基于 LangGraph checkpointer），不再使用
  ``ShortTermMemory`` 的 JSON 文件双轨存储。
- ``create_app`` 默认调用 ``get_case_memory()`` 获取共享实例；测试可注入
  兼容 ``CaseMemory`` 协议的桩。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from lvyan.config import AGENT_DIR, is_official_db_available, settings
from lvyan.memory.store import CaseMemory
from lvyan.runtime import get_case_memory
from lvyan.tools.file_converter import convert_to_markdown, get_file_category

from .models import (
    AgentRunRequest,
    AgentRunResponse,
    HITLRequest,
    HITLResponse,
    HealthResponse,
    UploadResponse,
    ThreadListResponse,
    ThreadSummary,
    DeleteResponse,
)
from .sse import RunManager, format_sse_event

_logger = logging.getLogger("lvyan.api.server")

# 文件上传相关常量
_UPLOAD_DIR = AGENT_DIR / "data" / "uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".log"}
_ALLOWED_OFFICE_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx"}


def _state_summary(state: Any) -> dict[str, Any]:
    """从 CaseState 提取非敏感摘要字段。"""
    return {
        "run_id": state.run_id,
        "thread_id": state.thread_id,
        "jurisdiction": state.jurisdiction,
        "case_type": state.case_type,
        "complexity": state.complexity,
        "risk_level": state.risk_level,
        "confidence": state.confidence,
        "iteration": state.iteration,
        "final_output": state.final_output,
        "facts_count": len(state.facts),
        "statutes_count": len(state.statutes),
        "cases_count": len(state.cases),
        "pending_human_approval": getattr(state, "pending_human_approval", None) is not None,
    }


def _check_database() -> str:
    """轻量数据库可用性检查：探测 psycopg 是否可导入。"""
    try:
        import psycopg  # noqa: F401
        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _check_retrieval() -> str:
    """检索服务可用性：精编知识库目录存在视为可用。"""
    try:
        return "ok" if is_official_db_available() or settings.knowledge_dir.is_dir() else "unavailable"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _check_model_gateway() -> str:
    """模型网关可用性：URL 已配置视为可用。"""
    return "ok" if settings.model_gateway_url.strip() else "unavailable"


def _read_text_preview(file_path: Path, max_chars: int = 500) -> str:
    """读取文本文件前 N 字符作为预览；非文本或读取失败返回空串。"""
    try:
        for enc_name in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                text = file_path.read_text(encoding=enc_name)
                return text[:max_chars]
            except UnicodeDecodeError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return ""


def create_app(
    runner: Any = None,
    memory: CaseMemory | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        runner: 可注入的异步 runner，用于测试替代真实图执行。
        memory: 可注入的 CaseMemory 实例；None 时使用共享实例（绑定共享图）。
    """
    app = FastAPI(title="律言法律智能体 API", version="0.2.0")
    manager = RunManager(runner=runner)
    # 优先使用注入的 memory（测试隔离），否则使用共享 CaseMemory（生产单源）
    mem = memory if memory is not None else get_case_memory()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        db = _check_database()
        ret = _check_retrieval()
        gw = _check_model_gateway()
        overall = "ok" if (db == "ok" and ret == "ok" and gw == "ok") else "degraded"
        return HealthResponse(
            status=overall, database=db, retrieval=ret, model_gateway=gw
        )

    @app.post("/api/agent/run", response_model=AgentRunResponse)
    async def run(req: AgentRunRequest) -> AgentRunResponse:
        complexity = req.complexity or "light"
        # 如有附件，把附件 Markdown 全文拼到 query 前面，让 Agent 能看到附件内容
        query_text = req.query
        if req.attachments:
            attachment_parts: list[str] = []
            for fid in req.attachments:
                meta_path = _UPLOAD_DIR / f"{fid}.json"
                if meta_path.is_file():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        # 优先使用 markdown 全文，回退到 text_preview
                        md = meta.get("markdown", "") or meta.get("text_preview", "")
                        if md:
                            attachment_parts.append(
                                f"【附件：{meta.get('filename', fid)}（{meta.get('category', 'unknown')}）】\n{md}"
                            )
                    except Exception:  # noqa: BLE001
                        continue
            if attachment_parts:
                query_text = "\n\n".join(attachment_parts) + "\n\n" + req.query

        ctx = manager.create_run(query_text, req.thread_id, complexity)
        return AgentRunResponse(
            run_id=ctx.run_id, thread_id=ctx.thread_id, status="started"
        )

    @app.get("/api/agent/stream/{run_id}")
    async def stream(run_id: str) -> StreamingResponse:
        ctx = manager.get(run_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")

        async def event_generator():
            while True:
                event = await ctx.queue.get()
                if event is None:  # 哨兵：运行结束
                    break
                yield format_sse_event(event)

        return StreamingResponse(
            event_generator(), media_type="text/event-stream"
        )

    @app.get("/api/agent/state/{thread_id}")
    async def state(thread_id: str) -> dict[str, Any]:
        cs = mem.load(thread_id)
        if cs is None:
            raise HTTPException(status_code=404, detail=f"thread {thread_id} 无记录")
        return _state_summary(cs)

    @app.delete("/api/agent/state/{thread_id}", response_model=DeleteResponse)
    async def delete_thread(thread_id: str) -> DeleteResponse:
        """删除指定会话：从 checkpointer 与索引中移除。"""
        cs = mem.load(thread_id)
        if cs is None:
            raise HTTPException(status_code=404, detail=f"thread {thread_id} 无记录")
        mem.delete(thread_id)
        return DeleteResponse(deleted=True, thread_id=thread_id)

    @app.get("/api/agent/threads", response_model=ThreadListResponse)
    async def list_threads() -> ThreadListResponse:
        """列出所有会话摘要。

        从 CaseMemory 索引读取 thread_id 与元数据（title/complexity/created_at/
        has_output），避免对每个 thread 都调用 ``load`` 触发 checkpointer 查询。
        """
        threads = mem.list_threads()  # list[tuple[str, dict]]
        summaries: list[ThreadSummary] = []
        for tid, meta in threads:
            # meta: {"title", "complexity", "created_at", "has_output"}
            title = (meta.get("title") or tid)[:40]
            summaries.append(ThreadSummary(
                thread_id=tid,
                title=title,
                complexity=meta.get("complexity") or "light",
                created_at=float(meta.get("created_at") or time.time()),
                has_output=bool(meta.get("has_output")),
            ))
        return ThreadListResponse(threads=summaries)

    @app.post("/api/upload", response_model=UploadResponse)
    async def upload_file(file: UploadFile = File(...)) -> UploadResponse:
        """上传文件，自动转为 Markdown，返回 file_id 供 /api/agent/run 引用。

        支持类型：
        - 文本：txt/md/csv/json/xml/html/log/yaml/toml → 直接读取
        - 文档：pdf/docx/pptx/xlsx/html/odt/rtf → markitdown 转 Markdown
        - 图片：png/jpg/jpeg/webp/gif/bmp/tiff → 视觉模型识别

        限制：单文件 10 MB
        """
        content = await file.read()
        if len(content) > _MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大（{len(content)} bytes），上限 10 MB",
            )

        filename = file.filename or "unnamed"
        ext = Path(filename).suffix.lower()
        content_type = file.content_type or "application/octet-stream"
        file_id = uuid.uuid4().hex[:16]

        # 存原始文件
        raw_path = _UPLOAD_DIR / f"{file_id}{ext}"
        raw_path.write_bytes(content)

        # 调用 file_converter 转换为 Markdown
        convert_result = convert_to_markdown(raw_path)
        markdown_text = convert_result["markdown"]
        category = convert_result["category"]
        converter = convert_result["converter"]
        char_count = convert_result["char_count"]

        # 文本预览（前 500 字）
        text_preview = markdown_text[:500] if markdown_text else ""

        # 存元数据 JSON（包含 markdown 全文供 /api/agent/run 引用）
        meta = {
            "file_id": file_id,
            "filename": filename,
            "size": len(content),
            "content_type": content_type,
            "ext": ext,
            "category": category,
            "converter": converter,
            "raw_path": str(raw_path),
            "text_preview": text_preview,
            "markdown": markdown_text,
            "char_count": char_count,
            "uploaded_at": time.time(),
        }
        meta_path = _UPLOAD_DIR / f"{file_id}.json"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _logger.info(
            "文件上传: %s (%d bytes, %s via %s, %d chars) -> file_id=%s",
            filename, len(content), category, converter, char_count, file_id,
        )

        return UploadResponse(
            file_id=file_id,
            filename=filename,
            size=len(content),
            content_type=content_type,
            text_preview=text_preview,
            category=category,
            markdown=markdown_text,
            char_count=char_count,
            converter=converter,
        )

    @app.post("/api/agent/hitl/{run_id}", response_model=HITLResponse)
    async def hitl(run_id: str, req: HITLRequest) -> HITLResponse:
        status, message = await manager.resolve_hitl(run_id, req)
        if status == "not_found":
            raise HTTPException(status_code=404, detail=message)
        return HITLResponse(run_id=run_id, status=status, message=message)

    # --- 静态文件与前端页面 ---
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            """返回前端首页。"""
            return FileResponse(str(_static_dir / "index.html"))

    return app


# 模块级默认应用，供 ``uvicorn lvyan.api.server:app`` 使用
app = create_app()


__all__ = ["create_app", "app"]
