"""API 请求/响应数据模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lvyan.observability.tracing import CostSummary


class AgentRunRequest(BaseModel):
    """POST /api/agent/run 请求体。

    四（输入模型约束）：
    - ``extra="forbid"``：拒绝未知字段，避免客户端拼错字段被静默忽略。
    - ``thread_id`` 限制长度与字符集，防止注入路径片段。
    - ``edited_output``（见 HITLRequest）有长度上限。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=50_000,
        description="用户法律问题或合同文本",
    )
    thread_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="会话线程 ID；为空时新生成。仅允许字母、数字、下划线、连字符。",
    )
    complexity: Literal["light", "deep", "document"] | None = Field(
        default=None,
        deprecated=True,
        description="已弃用，仅兼容旧客户端；回答形态由当前问题意图判定",
    )
    attachments: list[str] | None = Field(
        default=None,
        max_length=20,
        description="附件 file_id 列表（由 /api/upload 返回）",
    )
    law_as_of_date: date | None = Field(
        default=None,
        description="案件适用法律的时间点；为空时按系统当前日期检索和校验",
    )

    @field_validator("query")
    @classmethod
    def query_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题内容不能为空")
        return value

    @field_validator("law_as_of_date")
    @classmethod
    def law_as_of_date_must_be_reasonable(cls, value: date | None) -> date | None:
        """四：``law_as_of_date`` 合理范围提示（禁止明显非法的未来/远古日期）。"""
        if value is None:
            return value
        import datetime as _dt

        # 《民法典》施行日（2021-01-01）作为下限参考；早于 1900 视为明显错误。
        if value < _dt.date(1900, 1, 1):
            raise ValueError("law_as_of_date 不能早于 1900-01-01")
        # 不允许设定超过当前日期 + 10 年的未来点
        far_future = _dt.date.today().replace(year=_dt.date.today().year + 10)
        if value > far_future:
            raise ValueError("law_as_of_date 超出合理未来范围")
        return value


class AgentRunResponse(BaseModel):
    """POST /api/agent/run 响应体。"""

    run_id: str
    thread_id: str
    status: str = "started"


class HITLRequest(BaseModel):
    """POST /api/agent/hitl/{run_id} 请求体。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "edit"]
    edited_output: str | None = Field(
        default=None,
        max_length=100_000,
        description="edit 动作时用户改写后的输出（四：限制最大长度）",
    )


class HITLResponse(BaseModel):
    """POST /api/agent/hitl/{run_id} 响应体。"""

    run_id: str
    status: str
    message: str | None = None


class HealthResponse(BaseModel):
    """GET /api/health 响应体。"""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    # P0-B：retrieval 可能为 degraded（法库/索引不一致），不仅是 ok/unavailable
    retrieval: Literal["ok", "degraded", "unavailable"]
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
    markdown: str = Field(
        default="", description="转换后的 Markdown 全文（文本/文档/图片识别结果）"
    )
    char_count: int = Field(default=0, description="Markdown 字符数")
    converter: str = Field(default="none", description="使用的转换器：direct/markitdown/vision")


class ThreadSummary(BaseModel):
    """会话摘要（列表/删除接口用）。"""

    thread_id: str
    title: str = ""
    complexity: str = "light"
    created_at: float = 0.0
    updated_at: float = 0.0
    has_output: bool = False


class ThreadListResponse(BaseModel):
    """GET /api/agent/threads 响应体。"""

    threads: list[ThreadSummary] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    """DELETE 响应体。"""

    deleted: bool = True
    thread_id: str


class CaseCreateRequest(BaseModel):
    """创建一个受当前用户隔离的案件工作空间。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=5_000)
    thread_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("title")
    @classmethod
    def title_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("案件标题不能为空")
        return value


class EvidenceLinkRequest(BaseModel):
    """将已经上传且归属当前用户的文件链接为案件证据。"""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    evidence_type: str = Field(default="other", min_length=1, max_length=80)
    summary: str = Field(default="", max_length=5_000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class DocumentCreateRequest(BaseModel):
    """在案件内创建一份带初始版本的法律文书。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    document_type: str = Field(min_length=1, max_length=80)
    content: str = Field(max_length=300_000)
    source_run_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class DocumentVersionCreateRequest(BaseModel):
    """为文书创建一个不可变的新版本。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=300_000)
    change_summary: str = Field(default="", max_length=2_000)
    source_run_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ReviewFindingCreateRequest(BaseModel):
    """记录一项可追踪的文书审阅问题。"""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=128)
    severity: Literal["low", "medium", "high", "critical"]
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=10_000)
    suggestion: str = Field(default="", max_length=10_000)
    source_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class ReviewFindingStatusRequest(BaseModel):
    """关闭或豁免一项审阅问题。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["resolved", "waived"]


class DocumentApprovalRequest(BaseModel):
    """对指定版本作出审批决定。"""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=5_000)


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
    "CaseCreateRequest",
    "EvidenceLinkRequest",
    "DocumentCreateRequest",
    "DocumentVersionCreateRequest",
    "ReviewFindingCreateRequest",
    "ReviewFindingStatusRequest",
    "DocumentApprovalRequest",
]
