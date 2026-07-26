"""API 请求/响应数据模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from lvyan.observability.tracing import CostSummary


class AgentRunRequest(BaseModel):
    """POST /api/agent/run 请求体。"""

    query: str = Field(description="用户法律问题或合同文本")
    thread_id: str | None = Field(default=None, description="会话线程 ID；为空时新生成")
    complexity: Literal["light", "deep", "document"] | None = Field(
        default=None, description="输出复杂度档位；为空时默认 light"
    )
    attachments: list[str] | None = Field(
        default=None, description="附件 file_id 列表（由 /api/upload 返回）"
    )
    law_as_of_date: date | None = Field(
        default=None,
        description="案件适用法律的时间点；为空时按系统当前日期检索和校验",
    )


class AgentRunResponse(BaseModel):
    """POST /api/agent/run 响应体。"""

    run_id: str
    thread_id: str
    status: str = "started"


class HITLRequest(BaseModel):
    """POST /api/agent/hitl/{run_id} 请求体。"""

    action: Literal["approve", "reject", "edit"]
    edited_output: str | None = Field(default=None, description="edit 动作时用户改写后的输出")


class HITLResponse(BaseModel):
    """POST /api/agent/hitl/{run_id} 响应体。"""

    run_id: str
    status: str
    message: str | None = None


class HealthResponse(BaseModel):
    """GET /api/health 响应体。"""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    retrieval: Literal["ok", "unavailable"]
    model_gateway: Literal["ok", "unavailable"]


class NodeTrace(BaseModel):
    """单节点执行追踪摘要。"""

    node: str
    duration_ms: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadResponse(BaseModel):
    """POST /api/upload 响应体。"""

    file_id: str
    filename: str
    size: int
    content_type: str
    text_preview: str = Field(default="", description="文本文件前 500 字预览")
    category: str = Field(default="unknown", description="文件类别：text/doc/image/unknown")
    markdown: str = Field(default="", description="转换后的 Markdown 全文（文本/文档/图片识别结果）")
    char_count: int = Field(default=0, description="Markdown 字符数")
    converter: str = Field(default="none", description="使用的转换器：direct/markitdown/vision")


class ThreadSummary(BaseModel):
    """会话摘要（列表/删除接口用）。"""

    thread_id: str
    title: str = ""
    complexity: str = "light"
    created_at: float = 0.0
    has_output: bool = False


class ThreadListResponse(BaseModel):
    """GET /api/agent/threads 响应体。"""

    threads: list[ThreadSummary] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    """DELETE 响应体。"""

    deleted: bool = True
    thread_id: str


__all__ = [
    "AgentRunRequest",
    "AgentRunResponse",
    "HITLRequest",
    "HITLResponse",
    "HealthResponse",
    "NodeTrace",
    "CostSummary",
    "UploadResponse",
    "ThreadSummary",
    "ThreadListResponse",
    "DeleteResponse",
]
