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

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from lvyan.config import (
    AGENT_DIR,
    LAWTEXT_DIR,
    is_official_db_available,
    is_official_law_db_required,
    settings,
)
from lvyan.memory.store import CaseMemory
from lvyan.memory.run_metadata import (
    PostgresRunMetadataStore,
    RunMetadataStore,
    RunMetadataUnavailable,
    ThreadOwnershipError,
)
from lvyan.runtime import get_case_memory
from lvyan.tools.file_converter import convert_to_markdown

from .auth import (
    ANONYMOUS_USER,
    assert_run_owner,
    assert_thread_owner,
    get_current_user_id,
    is_auth_enabled,
)
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
_ALLOWED_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".log"}
_ALLOWED_OFFICE_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx"}
_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
_ALLOWED_ALL_EXTS = _ALLOWED_TEXT_EXTS | _ALLOWED_OFFICE_EXTS | _ALLOWED_IMAGE_EXTS
_UPLOAD_FILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# 扩展名 → 期望 MIME 前缀（用于一致性校验，避免扩展名伪装）
_EXT_TO_MIME_PREFIX: dict[str, str] = {
    ".txt": "text/",
    ".md": "text/",
    ".csv": "text/",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".log": "text/",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}

# Magic bytes（文件头）白名单：避免扩展名伪造
_MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    # W9 修复：补全 .webp 与 .tiff 的 magic bytes 校验，避免伪装上传
    ".webp": (b"RIFF",),  # RIFF....WEBP（前 4 字节 RIFF，第 8-12 字节 WEBP）
    ".tiff": (b"II\x2a\x00", b"MM\x00\x2a"),  # 小端 II*\0 或大端 MM\0*
    ".docx": (b"PK\x03\x04",),  # ZIP-based Office
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".doc": (b"\xd0\xcf\x11\xe0",),  # OLE Compound
    ".xls": (b"\xd0\xcf\x11\xe0",),
}

# W9：.webp 需要二次校验（RIFF 头后第 8-12 字节必须是 WEBP）
_WEBP_SECONDARY_TAG = b"WEBP"


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
        "legal_answer": getattr(state, "legal_answer", None),
        "facts_count": len(state.facts),
        "statutes_count": len(state.statutes),
        "cases_count": len(state.cases),
        "pending_human_approval": getattr(state, "pending_human_approval", None) is not None,
    }


def _as_unix_timestamp(value: Any) -> float:
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _detect_checkpointer_kind_from_instance(checkpointer: Any) -> str:
    """根据 checkpointer 实例类名判断后端类型（W1 修复辅助）。

    与 ``runtime._detect_checkpointer_kind`` 逻辑一致，但接受任意实例而非
    依赖全局变量，供 ``/readyz`` 实时探测当前 CaseMemory 实际使用的图。
    """
    if checkpointer is None:
        return "unknown"
    name = type(checkpointer).__name__.lower()
    if "postgres" in name:
        return "postgres"
    if "memory" in name:
        return "memory"
    return "unknown"


async def _mem_aload(mem: Any, thread_id: str) -> Any:
    """异步加载会话状态，兼容异步 / 同步 CaseMemory 实现。

    生产 ``CaseMemory`` 提供 ``aload``（使用异步图 ``aget_state``，不阻塞事件循环，
    修复 C2）；旧测试桩（如 ``FakeCaseMemory``）仅实现同步 ``load``，回退到直接
    调用（测试场景可接受同步阻塞）。
    """
    aload = getattr(mem, "aload", None)
    if callable(aload):
        return await aload(thread_id)
    return mem.load(thread_id)


async def _mem_aload_strict(mem: Any, thread_id: str) -> Any:
    """异步严格加载会话状态，兼容异步 / 同步 CaseMemory 实现。

    生产 ``CaseMemory`` 提供 ``aload_strict``（使用异步图 ``aget_state``）；
    旧测试桩仅实现同步 ``load_strict``，回退到直接调用。
    """
    aload_strict = getattr(mem, "aload_strict", None)
    if callable(aload_strict):
        return await aload_strict(thread_id)
    return mem.load_strict(thread_id)


async def _mem_adelete_strict(mem: Any, thread_id: str) -> bool:
    """异步严格删除会话，兼容异步 / 同步 CaseMemory 实现。

    生产 ``CaseMemory`` 提供 ``adelete_strict``（通过 ``asyncio.to_thread``
    卸载同步 ``delete_thread``，不阻塞事件循环，修复 C2）；旧测试桩仅实现
    同步 ``delete_strict``，回退到直接调用。
    """
    adelete_strict = getattr(mem, "adelete_strict", None)
    if callable(adelete_strict):
        return await adelete_strict(thread_id)
    return mem.delete_strict(thread_id)


async def _mem_alist_threads_recoverable(mem: Any) -> list[tuple[str, dict[str, Any]]]:
    """异步列出可恢复的会话，兼容异步 / 同步 CaseMemory 实现。

    生产 ``CaseMemory`` 提供 ``alist_threads_strict``（异步校验每个索引项的
    checkpoint 是否仍存在）；旧测试桩无此方法，回退到同步遍历 + ``load_strict``。
    """
    alist_strict = getattr(mem, "alist_threads_strict", None)
    if callable(alist_strict):
        return await alist_strict()
    # 回退：同步遍历索引，逐项校验 checkpoint（兼容旧测试桩）
    recoverable: list[tuple[str, dict[str, Any]]] = []
    for tid, meta in mem.list_threads():
        try:
            if mem.load_strict(tid) is not None:
                recoverable.append((tid, meta))
        except Exception:  # noqa: BLE001
            continue
    return recoverable


def _check_database() -> str:
    """轻量数据库可用性检查：探测 psycopg 是否可导入。
    P2-19：这是 ``/livez`` 级别的检查（仅探测 import）；``/readyz`` 使用
    :func:`_check_database_ready` 实际 ``SELECT 1``。
    """
    try:
        import psycopg  # noqa: F401

        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _check_database_ready() -> str:
    """``/readyz`` 级别：实际尝试连接 PostgreSQL 并 ``SELECT 1``。

    W2 修复：实时读取 ``DATABASE_URL`` 环境变量，与 builder.py /
    run_metadata.py 保持一致，避免 settings 单例在 import 时冻结导致
    monkeypatch 不生效。
    """
    try:
        import os

        import psycopg

        # 实时读环境变量，避免 settings 单例冻结（与 builder.py 一致）
        dsn = os.getenv("DATABASE_URL", settings.database_url)
        # 把 SQLAlchemy 风格连接串转为 psycopg 原生 DSN
        if dsn.startswith("postgresql+psycopg://"):
            dsn = "postgresql://" + dsn[len("postgresql+psycopg://") :]
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _check_retrieval() -> str:
    """检索服务可用性。

    P0-4：``REQUIRE_OFFICIAL_LAW_DB=true``（生产默认）时，官方法律库缺失即视为
    ``degraded``（/readyz 据此返回 not-ready），避免在仅精编知识库子集下承接
    生产流量。未要求完整库时，精编知识库目录存在即视为 ``ok``（降级可用）。
    """
    try:
        if is_official_db_available():
            return "ok"
        if is_official_law_db_required():
            return "degraded"
        # 未强制要求完整库：精编知识库存在即降级可用
        return "ok" if settings.knowledge_dir.is_dir() else "unavailable"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _legal_corpus_status() -> dict[str, Any]:
    """P0-4：法律语料库详细状态（供 /readyz 透明披露，区分「完整库」与「精编子集」）。

    返回结构：
        - mode: ``official_full``（官方法律库可用）/ ``curated_only``（仅精编子集）
        - required: 是否强制要求完整库
        - available: 完整库是否可用
        - documents / chunks: 规模统计（chunks 读 article_index 缓存，缺失时为 null）
    """
    available = is_official_db_available()
    required = is_official_law_db_required()
    info: dict[str, Any] = {
        "mode": "official_full" if available else "curated_only",
        "required": required,
        "available": available,
        "lawtext_dir": str(LAWTEXT_DIR),
        "documents": None,
        "chunks": None,
    }
    # 尽力统计规模；缓存缺失或读取异常不阻断就绪检查
    try:
        if available:
            info["documents"] = sum(1 for _ in LAWTEXT_DIR.rglob("*.md"))
        pkl = AGENT_DIR / "knowledge" / "manifests" / "article_index_v2.pkl"
        js = AGENT_DIR / "knowledge" / "manifests" / "article_index_v2.json"
        if pkl.is_file():
            import pickle

            with open(pkl, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and isinstance(cached.get("chunks"), list):
                info["chunks"] = len(cached["chunks"])
        elif js.is_file():
            import json

            with open(js, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("chunks"), list):
                info["chunks"] = len(raw["chunks"])
    except Exception:  # noqa: BLE001
        pass
    return info


def _check_model_gateway() -> str:
    """模型网关可用性：以可访问的健康端点为准，而非仅检查 URL 配置。"""
    return _check_model_gateway_ready()


def _check_model_gateway_ready() -> str:
    """``/readyz`` 级别：实际访问 ``{gateway}/models`` 或 ``/health``。"""
    gateway = settings.model_gateway_url.strip()
    if not gateway:
        return "unavailable"
    try:
        import httpx  # type: ignore[import-untyped]

        with httpx.Client(timeout=3.0) as client:
            # 优先 /v1/models，失败则尝试 /health
            try:
                r = client.get(
                    f"{gateway.rstrip('/')}/v1/models",
                    headers=(
                        {"Authorization": f"Bearer {settings.model_gateway_api_key}"}
                        if settings.model_gateway_api_key
                        else {}
                    ),
                )
                if 200 <= r.status_code < 300:
                    return "ok"
            except Exception:  # noqa: BLE001
                pass
            r = client.get(f"{gateway.rstrip('/')}/health")
            return "ok" if 200 <= r.status_code < 300 else "unavailable"
    except Exception:  # noqa: BLE001
        return "unavailable"


def _get_cors_origins() -> list[str]:
    """读取 CORS 白名单。

    通过 ``CORS_ALLOWED_ORIGINS`` 环境变量配置，逗号分隔；
    未配置时仅允许内置前端的本地来源。鉴于 API 支持凭据与 ``X-User-ID``，
    不允许通配符来源。
    """
    import os

    raw = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:8000", "http://127.0.0.1:8000"]
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise ValueError("CORS_ALLOWED_ORIGINS 不能包含 '*'：启用凭据时必须配置明确来源")
    return origins


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


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """原子写入字节文件：先写临时文件再 ``os.replace`` 覆盖目标。"""
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _atomic_write_text(target: Path, data: str, encoding: str = "utf-8") -> None:
    """原子写入文本文件。"""
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(data, encoding=encoding)
    os.replace(tmp, target)


def _resolve_upload_path(path_str: str) -> Path:
    """把 ``path_str`` 解析为绝对路径，并校验仍在 ``_UPLOAD_DIR`` 内。

    M5：防止 ``markdown_path`` / ``raw_path`` 被构造为 ``../../etc/passwd`` 等
    路径穿越。任一路径逃逸出 ``_UPLOAD_DIR`` 抛 :class:`HTTPException(404)`。
    """
    candidate = (Path(path_str)).resolve()
    try:
        candidate.relative_to(_UPLOAD_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"附件路径非法（逃逸出上传目录）：{path_str}",
        ) from exc
    return candidate


def _metadata_path_for_file_id(file_id: str) -> Path:
    """返回附件元数据路径，并拒绝把 file_id 解释为路径。

    ``file_id`` 来自 API 请求体；即使后缀固定为 ``.json``，也不能允许
    ``../``、盘符或路径分隔符参与路径拼接。当前上传生成 16 位 hex，放宽
    为安全的字母数字、下划线和连字符以兼容旧数据。
    """
    if not _UPLOAD_FILE_ID_RE.fullmatch(file_id):
        raise HTTPException(status_code=422, detail="附件 file_id 格式非法")
    return _resolve_upload_path(str(_UPLOAD_DIR / f"{file_id}.json"))


# P1-5：附件包装的闭合标签。选择一个正文里几乎不会出现的随机后缀，
# 即便附件正文含 ``</untrusted_document>`` 也无法提前关闭包装边界。
_UNTRUSTED_DOC_CLOSE = "</untrusted_document>"


def _xml_attr_escape(value: str) -> str:
    """转义字符串以安全嵌入 XML 属性值（双引号上下文）。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _harden_attachment_content(content: str) -> str:
    """中性化附件正文中可能出现的闭合标签，防止逃逸包装边界（P1-5）。"""
    return content.replace(_UNTRUSTED_DOC_CLOSE, "&lt;/untrusted_document&gt;")


def _enforce_zip_uncompressed_limit(content: bytes, limit: int) -> None:
    """P1-4 / W10：扫描 ZIP 累计未压缩大小，超限即拒绝（防 ZIP bomb）。

    W10 修复：原实现仅扫描 local file header（``PK\\x03\\x04``），当 ZIP 使用
    data descriptor 模式时（general purpose bit flag 第 3 位置 1），local header
    中的 comp_size/uncomp_size 均为 0，实际大小记录在压缩数据之后的 data
    descriptor 中，导致 ZIP bomb 检查被绕过。

    新实现优先解析 central directory（位于文件末尾，``PK\\x01\\x02`` 记录），
    其中 ``uncomp_size`` 字段始终包含真实大小，不受 data descriptor 影响。
    回退路径：central directory 不存在 / 解析失败时，仍扫描 local file header
    （覆盖非标准 ZIP）。对加密 / 异常 ZIP 直接放行（markitdown 自行处理或失败）。
    """
    import struct

    # ------------------------------------------------------------------
    # 策略 1：解析 central directory（优先，不受 data descriptor 影响）
    # ------------------------------------------------------------------
    eocd_sig = b"PK\x05\x06"
    eocd_idx = content.rfind(eocd_sig)
    if eocd_idx != -1 and eocd_idx + 22 <= len(content):
        try:
            # EOCD: sig(4) disk(2) cd_disk(2) disk_entries(2)
            #       total_entries(2) cd_size(4) cd_offset(4) comment_len(2)
            (_sig, _disk, _cd_disk, _disk_entries,
             total_entries, cd_size, cd_offset, _comment_len) = struct.unpack(
                "<IHHHHIIH", content[eocd_idx : eocd_idx + 22]
            )
            total = 0
            cd_sig = b"PK\x01\x02"
            offset = cd_offset
            entries_scanned = 0
            # 防御性上限：避免畸形 cd_offset 导致无限循环
            max_entries = max(total_entries, 0) + 1024
            while entries_scanned < max_entries:
                idx = content.find(cd_sig, offset)
                if idx == -1 or idx + 46 > len(content):
                    break
                try:
                    # central directory file header:
                    # sig(4) ver_made(2) ver_need(2) flag(2) method(2) time(2) date(2)
                    # crc(4) comp_size(4) uncomp_size(4) name_len(2) extra_len(2)
                    # comment_len(2) disk_start(2) int_attr(2) ext_attr(4) local_offset(4)
                    (_s, _vm, _vn, _flag, _method, _t, _d, _crc,
                     comp_size, uncomp_size, name_len, extra_len,
                     comment_len, _ds, _ia, _ea, _lo) = struct.unpack(
                        "<IHHHHHHIIIHHHHHII", content[idx : idx + 46]
                    )
                except struct.error:
                    break
                # central directory 中 uncomp_size 永远是真实值（无 data descriptor 问题）
                total += uncomp_size or comp_size
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Office 文件解压后总大小 {total} bytes 超过上限 {limit} bytes"
                            "（疑似 ZIP bomb）"
                        ),
                    )
                offset = idx + 46 + name_len + extra_len + comment_len
                entries_scanned += 1
            if entries_scanned > 0:
                # central directory 解析成功，直接返回
                return
        except HTTPException:
            # 413（ZIP bomb 命中）必须向上传播
            raise
        except struct.error:
            # EOCD / central directory 解析失败，回退到 local header 扫描
            pass

    # ------------------------------------------------------------------
    # 策略 2：回退到 local file header 扫描（覆盖非标准 ZIP / EOCD 损坏）
    # ------------------------------------------------------------------
    total = 0
    offset = 0
    sig = b"PK\x03\x04"
    while True:
        idx = content.find(sig, offset)
        if idx == -1 or idx + 30 > len(content):
            break
        try:
            (_sig, _ver, _flag, _method, _t, _d, _crc,
             comp_size, uncomp_size, name_len, extra_len) = struct.unpack(
                "<IHHHHHIIIHH", content[idx : idx + 30]
            )
        except struct.error:
            break
        # data descriptor（flag bit 3）时 comp/uncomp 可能为 0，跳过该项
        if uncomp_size:
            total += uncomp_size
        elif comp_size:
            total += comp_size
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Office 文件解压后总大小 {total} bytes 超过上限 {limit} bytes"
                    "（疑似 ZIP bomb）"
                ),
            )
        # 跳过本条 entry（header + name + extra + data）
        offset = idx + 30 + name_len + extra_len + max(comp_size, 0)


# P1-4：文档转换并发信号量（惰性创建，按 settings.max_concurrent_conversions）
_conversion_semaphore: asyncio.Semaphore | None = None


def _get_conversion_semaphore() -> asyncio.Semaphore:
    global _conversion_semaphore
    if _conversion_semaphore is None:
        from lvyan.config import settings as _settings

        _conversion_semaphore = asyncio.Semaphore(
            max(1, _settings.max_concurrent_conversions)
        )
    return _conversion_semaphore


def _load_attachment_markdown(meta: dict[str, Any], fid: str) -> str:
    """从附件元数据读取 Markdown 正文，按以下顺序：

    1. 优先读取 ``markdown_path`` 指向的 ``.md`` 文件（M5 新格式）。
    2. 路径不存在或读取失败 → 回退到旧 JSON 中的 ``markdown`` 字段（M5 兼容）。
    3. 最后回退 ``text_preview``。

    Markdown 文件被声明却不存在时，按场景返回 404；不能静默忽略。
    """
    markdown_path_str = meta.get("markdown_path")
    if markdown_path_str:
        md_path = _resolve_upload_path(markdown_path_str)
        if not md_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"附件 {fid} 的 Markdown 文件不存在：{md_path.name}",
            )
        try:
            return md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"附件 {fid} 的 Markdown 文件读取失败：{exc}",
            ) from exc

    # 兼容旧 JSON：markdown 字段直接存了全文
    legacy_md = meta.get("markdown")
    if legacy_md:
        return str(legacy_md)

    return meta.get("text_preview", "") or ""


def create_app(
    runner: Any = None,
    memory: CaseMemory | None = None,
    metadata_store: RunMetadataStore | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        runner: 可注入的异步 runner，用于测试替代真实图执行。
        memory: 可注入的 CaseMemory 实例；None 时使用共享实例（绑定共享图）。
    """
    # P1-2 / P1-4：启动期验证运行时配置（非法 backend、冲突组合、生产认证）
    from lvyan.config import is_auth_enabled_env, is_production, validate_runtime_config

    validate_runtime_config()

    # W12：生产模式禁用 /docs /redoc /openapi.json，避免 API 结构泄露
    _is_prod = is_production()

    # W8：生产模式 + AUTH_ENABLED=false 启动告警（不阻断启动，仅显眼日志）
    # 默认配置为 AUTH_ENABLED=false 方便首次部署，但生产部署若忘记开启，
    # 所有接口将无认证暴露，因此打印 WARNING 提醒运维。
    if _is_prod and not is_auth_enabled_env():
        _logger.warning(
            "⚠️  生产模式（RUNTIME_MODE=production）下 AUTH_ENABLED=false："
            "API 完全无认证暴露，任何人都可发起 Agent run / 上传文件 / 查看历史会话。"
            "请通过 .env 设置 AUTH_ENABLED=true 并配置 AUTH_MODE=jwt 或 trusted_proxy。"
        )
    app = FastAPI(
        title="律言法律智能体 API",
        version="0.2.0",
        docs_url=None if _is_prod else "/docs",
        redoc_url=None if _is_prod else "/redoc",
        openapi_url=None if _is_prod else "/openapi.json",
    )

    if metadata_store is None and runner is None and memory is None:
        from lvyan.config import durable_runtime_required

        try:
            metadata_store = PostgresRunMetadataStore()
            # 做一次轻量探测，确认数据库可达。
            metadata_store.healthcheck()
        except Exception as exc:
            # P0-1 / P1-3：durable_runtime_required（含 CHECKPOINTER_BACKEND=postgres）
            # 时，metadata store 初始化失败必须让服务启动失败。
            if durable_runtime_required():
                from lvyan.graph.builder import PersistenceUnavailable

                raise PersistenceUnavailable(
                    f"PostgreSQL run metadata store 不可用，且当前为强制持久化模式，"
                    f"拒绝降级: {exc}"
                ) from exc
            _logger.warning(
                "PostgreSQL 不可达，RunMetadata 持久化已禁用；"
                "run 恢复、跨实例 HITL 等功能将不可用"
            )
            metadata_store = None
    manager = RunManager(runner=runner, metadata_store=metadata_store)
    # 优先使用注入的 memory（测试隔离），否则使用共享 CaseMemory（生产单源）
    mem = memory if memory is not None else get_case_memory()

    app.add_middleware(
        CORSMiddleware,
        # P2-13：CORS 白名单；通过 CORS_ALLOWED_ORIGINS 环境变量覆盖。
        # 默认 localhost 用于本地开发；未配置时回退到 ["*"] 仅本地开发可用。
        allow_origins=_get_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-User-ID"],
    )

    # W11：安全响应头中间件，防止 MIME 嗅探 / 点击劫持 / 协议降级
    @app.middleware("http")
    async def _security_headers_middleware(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        return response

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """向后兼容的总体健康检查（livez + readyz 的混合视图）。"""
        db = _check_database()
        ret = _check_retrieval()
        gw = _check_model_gateway()
        overall = "ok" if (db == "ok" and ret == "ok" and gw == "ok") else "degraded"
        return HealthResponse(status=overall, database=db, retrieval=ret, model_gateway=gw)

    # P2-19：分离 livez（进程活着）与 readyz（依赖就绪）
    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """存活检查：进程能响应即为 ok，不查任何依赖。"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """就绪检查：实际探测 DB / 检索 / 模型网关 / checkpointer，任一关键依赖
        不可用则 not-ready。

        - ``database``：``SELECT 1``
        - ``retrieval``：知识库目录可读
        - ``model_gateway``：访问 ``/v1/models`` 或 ``/health``
        - ``checkpointer``：P0-1 新增。实际 checkpointer 后端（postgres/memory）。
          生产模式下若为 memory（静默降级）→ not-ready。
        - ``object_storage``：保留位（当前未实现）
        """
        db = _check_database_ready()
        if db == "ok" and metadata_store is not None:
            healthcheck = getattr(metadata_store, "healthcheck", None)
            if callable(healthcheck):
                try:
                    if not healthcheck():
                        db = "unavailable"
                except Exception:  # noqa: BLE001
                    db = "unavailable"
        ret = _check_retrieval()
        gw = _check_model_gateway_ready()

        # P0-1：暴露实际 checkpointer 类型；生产模式下 memory = 降级
        # W1 修复：实时探测当前 CaseMemory 实际使用的图实例的 checkpointer 类型，
        # 而非依赖 _checkpointer_kind 全局变量（后者只在同步图首次创建时设置，
        # 异步图就绪后不会更新）。
        from lvyan.config import durable_runtime_required, is_production, is_auth_enabled_env
        from lvyan.runtime import _resolve_shared_graph, get_checkpointer_kind

        cp_kind = "unknown"
        resolved_graph = _resolve_shared_graph()
        if resolved_graph is not None:
            cp_kind = _detect_checkpointer_kind_from_instance(resolved_graph.checkpointer)
        else:
            # 图尚未创建时回退到全局变量（兼容 CLI 模式 / 启动初期）
            cp_kind = get_checkpointer_kind()
        cp_status = "ok"
        if cp_kind == "unknown":
            cp_status = "unknown"
        elif is_production() and cp_kind != "postgres":
            cp_status = "degraded"

        # P0-1 / P1-3：durable_runtime_required 时 metadata store 为 None → not-ready
        metadata_status = "ok" if metadata_store is not None else "unavailable"
        if durable_runtime_required() and metadata_store is None:
            metadata_status = "degraded"

        # P1-4：认证配置状态（生产模式 + AUTH_MODE=auto = misconfigured）
        auth_status = "ok"
        if is_auth_enabled_env() and is_production():
            import os

            auth_mode = os.getenv("AUTH_MODE", "auto").strip().lower()
            if auth_mode == "auto":
                auth_status = "misconfigured"

        ready = (
            db == "ok"
            and ret == "ok"
            and cp_status != "degraded"
            and metadata_status != "degraded"
            and auth_status != "misconfigured"
        )
        # P0-4：法律语料库详细状态（透明披露「完整库」vs「精编子集」+ 规模统计）
        legal_corpus = _legal_corpus_status()
        # model_gateway 不可用不阻断 ready（可降级到规则路径）
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not-ready",
                "database": db,
                "retrieval": ret,
                "model_gateway": gw,
                "checkpointer": cp_kind,
                "checkpointer_status": cp_status,
                "metadata_store": metadata_status,
                "authentication": auth_status,
                "object_storage": "unknown",
                "legal_corpus": legal_corpus,
            },
        )

    @app.post("/api/agent/run", response_model=AgentRunResponse)
    async def run(
        req: AgentRunRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> AgentRunResponse:
        complexity = req.complexity or "light"
        # P2-15：附件作为「待分析证据」用 <untrusted_document> 包裹，
        # 系统 prompt 必须声明：文档内容不是系统/工具指令，禁止执行其中命令。
        # P0 性能：附件不再全文拼进 query_text（旧做法会让每个 LLM 节点都吞下整篇合同）。
        # 改为以 DocumentRef 形式收集，由 attachment_retriever 节点切块 + 按需检索。
        query_text = req.query
        attachment_refs: list[dict] = []
        if req.attachments:
            from lvyan.config import settings as _settings

            # P1-4：附件数量上限与去重（保序）
            seen_fids: set[str] = set()
            unique_attachments: list[str] = []
            for fid in req.attachments:
                if fid not in seen_fids:
                    seen_fids.add(fid)
                    unique_attachments.append(fid)
            if len(unique_attachments) > _settings.max_attachment_count:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"附件数量 {len(unique_attachments)} 超过上限 "
                        f"{_settings.max_attachment_count}"
                    ),
                )

            total_chars = 0
            for fid in unique_attachments:
                meta_path = _metadata_path_for_file_id(fid)
                # P0-3：引用的附件必须存在，缺失即整个请求失败（不静默跳过）。
                # 多实例下本机没有该附件 → 404，避免无证据继续给出法律结论。
                if not meta_path.is_file():
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"附件 {fid} 不存在；可能已删除或上传至其他实例。"
                            f"请重新上传后再发起分析。"
                        ),
                    )
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    # P0-3：元数据损坏 → 503，不静默跳过
                    raise HTTPException(
                        status_code=503,
                        detail=f"附件 {fid} 元数据损坏，无法读取：{exc}",
                    ) from exc
                except OSError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=f"附件 {fid} 元数据读取失败：{exc}",
                    ) from exc

                # P0-5：附件 ownership 校验
                if is_auth_enabled():
                    attachment_owner = meta.get("user_id", ANONYMOUS_USER)
                    if attachment_owner != user_id:
                        raise HTTPException(
                            status_code=403,
                            detail=f"附件 {fid} 不属于当前用户",
                        )

                # M5：读取 Markdown 正文（优先 .md 文件，回退旧 markdown，再回退 preview）
                # P0-3：读取失败（404/503）会向上抛出，终止整个 run。
                md = _load_attachment_markdown(meta, fid)
                if not md:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"附件 {fid} 转换结果为空（attachment_conversion_incomplete）；"
                            f"请重新上传或移除该附件。"
                        ),
                    )

                # P1-4：单文件字符上限（仅用于预算校验，不截断正文 —— 正文由 chunker 切块）
                if len(md) > _settings.max_extracted_chars_per_file:
                    md = md[: _settings.max_extracted_chars_per_file]
                total_chars += len(md)
                if total_chars > _settings.max_total_attachment_chars:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"附件总字符数 {total_chars} 超过上限 "
                            f"{_settings.max_total_attachment_chars}"
                        ),
                    )

                # P0 性能：构造 DocumentRef；stored_path 指向 markdown 文件，
                # attachment_retriever 节点据此切块 + 按需检索。
                markdown_path_str = meta.get("markdown_path", "")
                stored_path = str(_resolve_upload_path(markdown_path_str)) if markdown_path_str else ""
                attachment_refs.append(
                    {
                        "doc_id": fid,
                        "filename": str(meta.get("filename", meta.get("original_filename", fid))),
                        "doc_type": str(meta.get("doc_type", "unknown")),
                        "content_hash": str(meta.get("content_hash", "")),
                        "stored_path": stored_path,
                        "uploaded_at": meta.get("uploaded_at"),
                    }
                )

        # P0-5：已有 thread 的 ownership 校验（防止用他人 thread_id 继续运行）
        if req.thread_id:
            if metadata_store is not None:
                try:
                    durable_thread = metadata_store.get_thread(req.thread_id)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(
                        status_code=503,
                        detail="run metadata 暂时不可用",
                    ) from exc
                if durable_thread is not None and str(durable_thread.get("user_id", "")) != user_id:
                    raise HTTPException(
                        status_code=403,
                        detail=f"thread {req.thread_id} 不属于当前用户",
                    )
            existing_meta = dict(mem.list_threads()).get(req.thread_id)
            if existing_meta is not None:
                assert_thread_owner(existing_meta, user_id, req.thread_id)
            elif is_auth_enabled():
                # sidecar 索引缺失时，从 checkpoint 状态中恢复 owner 校验
                cs = await _mem_aload(mem, req.thread_id)
                if cs is not None:
                    cp_user_id = str(getattr(cs, "user_id", ANONYMOUS_USER) or ANONYMOUS_USER)
                    if cp_user_id != user_id:
                        raise HTTPException(
                            status_code=403,
                            detail=f"thread {req.thread_id} 不属于当前用户（owner={cp_user_id}）",
                        )

        try:
            ctx = manager.create_run(
                query_text,
                req.thread_id,
                complexity,
                user_id=user_id,
                law_as_of_date=req.law_as_of_date,
                attachments=req.attachments,
                attachment_refs=attachment_refs,
                display_query=req.query,
            )
        except ThreadOwnershipError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"thread {req.thread_id} 不属于当前用户",
            ) from exc
        except RunMetadataUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="无法创建可恢复的 Agent run",
            ) from exc
        # P2-13：把 user_id 写入 CaseMemory 索引，便于后续 ownership 过滤
        try:
            mem.register(
                ctx.thread_id,
                title=(req.query[:40] if req.query else ctx.thread_id),
                complexity=complexity,
                user_id=user_id,
            )
        except TypeError:
            # 兼容旧 CaseMemory.register（无 user_id 参数）
            mem.register(
                ctx.thread_id,
                title=(req.query[:40] if req.query else ctx.thread_id),
                complexity=complexity,
            )
        return AgentRunResponse(run_id=ctx.run_id, thread_id=ctx.thread_id, status="started")

    @app.get("/api/agent/stream/{run_id}")
    async def stream(
        run_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> StreamingResponse:
        ctx = manager.get(run_id)
        if ctx is None:
            if metadata_store is None:
                raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")
            try:
                durable_run = metadata_store.get_run(run_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="run metadata 暂时不可用",
                ) from exc
            if durable_run is None:
                raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")
            if is_auth_enabled() and str(durable_run.get("user_id", "")) != user_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"run {run_id} 不属于当前用户",
                )

            status = str(durable_run.get("status", "unknown"))
            if status not in ("completed", "failed", "cancelled"):
                raise HTTPException(
                    status_code=409,
                    detail=("该 run 正在另一实例执行；流式事件需要 session affinity"),
                )

            async def durable_event_generator():
                if status == "completed":
                    final_md = durable_run.get("final_output") or ""
                    legal_answer = durable_run.get("legal_answer")
                    # W4 修复：与 live 路径 _build_final_output_event 保持一致，
                    # 不再单独发送 markdown_fallback（output 字段已承担该角色）。
                    # 避免前端在首次运行与刷新恢复时收到不同事件结构。
                    event: dict[str, Any] = {"event": "final_output", "output": final_md}
                    if legal_answer:
                        event["schema_version"] = legal_answer.get("schema_version", "legal_answer_v1")
                        event["answer"] = legal_answer
                    yield format_sse_event(event)
                elif status == "failed":
                    # W4：与 live 路径 _fail_run 保持一致，包含 code 字段供前端分类
                    yield format_sse_event(
                        {
                            "event": "error",
                            "code": "run_failed",
                            "message": durable_run.get("error") or "Agent run failed",
                        }
                    )
                else:
                    yield format_sse_event(
                        {
                            "event": "cancelled",
                            "message": durable_run.get("error") or "已停止生成",
                        }
                    )

            return StreamingResponse(
                durable_event_generator(),
                media_type="text/event-stream",
            )
        # P2-13：run ownership 校验
        assert_run_owner(ctx, user_id, run_id)

        async def event_generator():
            try:
                while True:
                    event = await ctx.queue.get()
                    if event is None:  # 哨兵：运行结束
                        break
                    yield format_sse_event(event)
            except asyncio.CancelledError:
                # 客户端断开连接；记录日志，由 GC 收尾 RunContext
                _logger.info("SSE 客户端断开 run %s", run_id)
                raise

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/api/agent/state/{thread_id}")
    async def state(
        thread_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        if metadata_store is not None:
            try:
                meta = metadata_store.get_thread(thread_id)
                messages = metadata_store.list_messages(thread_id, user_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="thread metadata 暂时不可用",
                ) from exc
        else:
            meta = dict(mem.list_threads()).get(thread_id)
            messages = []
        # P1-3：ownership 以可信元数据为准；meta 缺失才 404。
        assert_thread_owner(meta, user_id, thread_id)

        # P1-3：checkpoint 读取与对话消息解耦——checkpoint 过期/损坏不再让整个
        # 会话返回 404。仅 best-effort 读取，失败时标记 checkpoint_available=false。
        cs: Any = None
        checkpoint_available = False
        try:
            cs = await _mem_aload_strict(mem, thread_id)
            checkpoint_available = cs is not None
        except Exception as exc:  # noqa: BLE001
            _logger.warning("加载 thread %s checkpoint 失败（仅降级标记）: %s", thread_id, exc)
            checkpoint_available = False

        formatted_messages = [
            {
                **message,
                "created_at": _as_unix_timestamp(message.get("created_at")),
            }
            for message in messages
        ]

        if cs is not None:
            summary = _state_summary(cs)
        else:
            # P1-3：checkpoint 不可用但消息仍在 → 返回可恢复的占位摘要
            summary = {
                "run_id": None,
                "thread_id": thread_id,
                "jurisdiction": None,
                "case_type": None,
                "complexity": meta.get("complexity") if meta else "light",
                "risk_level": None,
                "confidence": None,
                "iteration": 0,
                "final_output": None,
                "facts_count": 0,
                "statutes_count": 0,
                "cases_count": 0,
                "pending_human_approval": False,
                "state_summary": None,
            }
        summary["messages"] = formatted_messages
        summary["checkpoint_available"] = checkpoint_available
        summary["recoverable"] = bool(formatted_messages) or checkpoint_available
        return summary

    @app.delete("/api/agent/state/{thread_id}", response_model=DeleteResponse)
    async def delete_thread(
        thread_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> DeleteResponse:
        """删除指定会话：从 checkpointer 与索引中移除。"""
        if manager.has_active_thread_runs(thread_id):
            raise HTTPException(
                status_code=409,
                detail="该会话仍在运行，请先终止运行",
            )
        if metadata_store is not None:
            try:
                meta = metadata_store.get_thread(thread_id)
                assert_thread_owner(meta, user_id, thread_id)
                if metadata_store.has_active_runs(thread_id):
                    raise HTTPException(
                        status_code=409,
                        detail="该会话仍在运行，请先终止运行",
                    )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="thread metadata 暂时不可用",
                ) from exc
            try:
                await _mem_adelete_strict(mem, thread_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="checkpoint 删除失败",
                ) from exc
            try:
                deleted = metadata_store.delete_thread(thread_id, user_id)
                if not deleted and metadata_store.has_active_runs(thread_id):
                    raise HTTPException(
                        status_code=409,
                        detail="该会话仍在运行，请先终止运行",
                    )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="thread metadata 删除失败",
                ) from exc
            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"thread {thread_id} 无记录",
                )
        else:
            meta = dict(mem.list_threads()).get(thread_id)
            assert_thread_owner(meta, user_id, thread_id)
            try:
                await _mem_adelete_strict(mem, thread_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="checkpoint 删除失败",
                ) from exc
        return DeleteResponse(deleted=True, thread_id=thread_id)

    @app.get("/api/agent/threads", response_model=ThreadListResponse)
    async def list_threads(
        user_id: str = Depends(get_current_user_id),
    ) -> ThreadListResponse:
        """列出当前用户的会话摘要。

        P2-13：认证启用时只返回属于当前 user_id 的 thread；
        单租户模式下返回全部。
        """
        from .auth import ANONYMOUS_USER, is_auth_enabled

        if metadata_store is not None:
            try:
                threads = metadata_store.list_threads(user_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="thread metadata 暂时不可用",
                ) from exc
        else:
            # 元数据索引可能在 checkpointer 被清理或故障恢复后遗留条目。
            # 只向前端暴露仍可恢复的会话，避免点击历史记录后看到空白页。
            # C2 修复：使用异步方法避免同步 get_state 阻塞事件循环。
            try:
                threads = await _mem_alist_threads_recoverable(mem)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="checkpoint 读取失败，暂时无法列出会话",
                ) from exc
        summaries: list[ThreadSummary] = []
        for tid, meta in threads:
            # ownership 过滤
            if is_auth_enabled() and meta.get("user_id", ANONYMOUS_USER) != user_id:
                continue
            title = (meta.get("title") or tid)[:40]
            summaries.append(
                ThreadSummary(
                    thread_id=tid,
                    title=title,
                    complexity=meta.get("complexity") or "light",
                    created_at=_as_unix_timestamp(meta.get("created_at")),
                    updated_at=_as_unix_timestamp(meta.get("updated_at") or meta.get("created_at")),
                    has_output=bool(meta.get("has_output")),
                )
            )
        return ThreadListResponse(threads=summaries)

    @app.post("/api/upload", response_model=UploadResponse)
    async def upload_file(
        file: UploadFile = File(...),
        user_id: str = Depends(get_current_user_id),
    ) -> UploadResponse:
        """上传文件，自动转为 Markdown，返回 file_id 供 /api/agent/run 引用。

        支持类型：
        - 文本：txt/md/csv/json/xml/html/log/yaml/toml → 直接读取
        - 文档：pdf/docx/pptx/xlsx/html/odt/rtf → markitdown 转 Markdown
        - 图片：png/jpg/jpeg/webp/gif/bmp/tiff → 视觉模型识别

        P2-14 安全校验
        --------------
        - 扩展名白名单（拒绝未知类型，415）
        - MIME 与扩展名一致性校验（拒绝伪装，415）
        - Magic bytes 文件头校验（拒绝伪造，415）
        - 大小上限 10 MB（413）

        限制：单文件 10 MB
        """
        from lvyan.config import settings as _settings

        # P1-4：流式读取上传内容，边读边累计，一旦超过上限立即中止，避免一次性
        # 把超大请求体读入内存。``file.read(chunk)`` 在 starlette 中是异步流式。
        max_bytes = _settings.max_upload_bytes
        buf = bytearray()
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（>{len(buf)} bytes），上限 {max_bytes} bytes",
                )
        content = bytes(buf)

        filename = file.filename or "unnamed"
        ext = Path(filename).suffix.lower()
        content_type = file.content_type or "application/octet-stream"

        # P2-14：扩展名白名单
        if ext not in _ALLOWED_ALL_EXTS:
            raise HTTPException(
                status_code=415,
                detail=f"不支持的文件类型：{ext}（允许：{sorted(_ALLOWED_ALL_EXTS)}）",
            )

        # P1-4：ZIP-based Office 文件防 ZIP-bomb：校验解压后总字节数
        if ext in {".docx", ".xlsx", ".pptx"} and content.startswith(b"PK\x03\x04"):
            _enforce_zip_uncompressed_limit(content, _settings.zip_uncompressed_bytes_limit)

        # P2-14：MIME 与扩展名一致性校验（容忍 application/octet-stream）
        expected_mime = _EXT_TO_MIME_PREFIX.get(ext, "")
        if (
            expected_mime
            and content_type != "application/octet-stream"
            and not content_type.startswith(expected_mime.split("/")[0] + "/")
            and content_type != expected_mime
        ):
            raise HTTPException(
                status_code=415,
                detail=f"MIME 类型 {content_type} 与扩展名 {ext} 不一致",
            )

        # P2-14：Magic bytes 校验（仅对二进制格式）
        magic_signatures = _MAGIC_BYTES.get(ext)
        if magic_signatures and content:
            if not any(content.startswith(sig) for sig in magic_signatures):
                raise HTTPException(
                    status_code=415,
                    detail=f"文件头 magic bytes 与扩展名 {ext} 不符（疑似伪装）",
                )
            # W9：.webp 二次校验 —— RIFF 容器也用于 WAV/AVI，需确认第 8-12 字节是 WEBP
            if ext == ".webp" and len(content) >= 12 and content[8:12] != _WEBP_SECONDARY_TAG:
                raise HTTPException(
                    status_code=415,
                    detail="文件头 RIFF 但 secondary tag 不是 WEBP（疑似伪装）",
                )

        file_id = uuid.uuid4().hex[:16]

        # M5：原子写入原始文件、Markdown、JSON 元数据。
        # 任一步失败均清理本次产生的残留文件，避免半成品状态。
        raw_path = _UPLOAD_DIR / f"{file_id}{ext}"
        markdown_path = _UPLOAD_DIR / f"{file_id}.md"
        meta_path = _UPLOAD_DIR / f"{file_id}.json"
        created: list[Path] = []

        try:
            # 1) 原始文件（直接写二进制）
            _atomic_write_bytes(raw_path, content)
            created.append(raw_path)

            # 2) 调用 file_converter 转换为 Markdown（CPU/IO 密集）。
            # P1-4：并发信号量限流 + 超时保护，避免 markitdown 卡死拖垮 worker。
            from lvyan.config import settings as _settings

            try:
                async with _get_conversion_semaphore():
                    convert_result = await asyncio.wait_for(
                        asyncio.to_thread(convert_to_markdown, raw_path),
                        timeout=_settings.document_conversion_timeout_seconds,
                    )
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"文档转换超时（>{_settings.document_conversion_timeout_seconds}s），"
                        "请精简文件后重试"
                    ),
                ) from exc
            markdown_text = convert_result["markdown"]
            category = convert_result["category"]
            converter = convert_result["converter"]
            char_count = convert_result["char_count"]

            # 文本预览（前 500 字）
            text_preview = markdown_text[:500] if markdown_text else ""

            # 3) Markdown 独立文件（原子写入）
            _atomic_write_text(markdown_path, markdown_text, encoding="utf-8")
            created.append(markdown_path)

            # 4) JSON 元数据（只存路径与预览，不再嵌入全文）
            meta = {
                "file_id": file_id,
                "filename": filename,
                "size": len(content),
                "content_type": content_type,
                "ext": ext,
                "category": category,
                "converter": converter,
                "raw_path": str(raw_path),
                # M5：Markdown 全文存独立 .md 文件，JSON 仅记录路径
                "markdown_path": str(markdown_path),
                "text_preview": text_preview,
                "char_count": char_count,
                "uploaded_at": time.time(),
                "user_id": user_id,
            }
            _atomic_write_text(
                meta_path,
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            created.append(meta_path)
        except Exception:
            # 清理本次产生的残留文件
            for path in created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        _logger.info(
            "文件上传: %s (%d bytes, %s via %s, %d chars) -> file_id=%s",
            filename,
            len(content),
            category,
            converter,
            char_count,
            file_id,
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
    async def hitl(
        run_id: str,
        req: HITLRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> HITLResponse:
        ctx = manager.get(run_id)
        if ctx is not None:
            # P2-13：HITL 决策必须由发起 run 的同一用户做出
            assert_run_owner(ctx, user_id, run_id)
        status, message = await manager.resolve_hitl(run_id, req, current_user_id=user_id)
        if status == "not_found":
            raise HTTPException(status_code=404, detail=message)
        if status == "forbidden":
            raise HTTPException(status_code=403, detail=message)
        if status == "unavailable":
            raise HTTPException(status_code=503, detail=message)
        if status == "error":
            raise HTTPException(status_code=409, detail=message)
        return HITLResponse(run_id=run_id, status=status, message=message)

    @app.post("/api/agent/cancel/{run_id}", response_model=HITLResponse)
    async def cancel_run(
        run_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> HITLResponse:
        status, message = await manager.cancel_run(run_id, user_id)
        if status == "not_found":
            raise HTTPException(status_code=404, detail=message)
        if status == "forbidden":
            raise HTTPException(status_code=403, detail=message)
        if status == "conflict":
            raise HTTPException(status_code=409, detail=message)
        # P1-2：持久化失败 → 503；cancel_requested → 202（已接受，远端将停止）
        if status == "unavailable":
            raise HTTPException(status_code=503, detail=message)
        if status == "cancel_requested":
            return HITLResponse(
                run_id=run_id, status=status, message=message
            )
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
