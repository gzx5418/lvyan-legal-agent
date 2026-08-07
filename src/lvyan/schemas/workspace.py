"""案件工作空间与文书审阅生命周期模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


CaseStatus = Literal["active", "archived", "closed"]
DocumentStatus = Literal["draft", "in_review", "approved", "superseded"]
FindingSeverity = Literal["low", "medium", "high", "critical"]
FindingStatus = Literal["open", "resolved", "waived"]
ApprovalDecision = Literal["approved", "rejected"]


class LegalCase(BaseModel):
    case_id: str
    user_id: str
    title: str
    description: str = ""
    status: CaseStatus = "active"
    thread_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CaseEvidence(BaseModel):
    evidence_id: str
    case_id: str
    file_id: str
    filename: str
    evidence_type: str = "other"
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


class LegalDocument(BaseModel):
    document_id: str
    case_id: str
    title: str
    document_type: str
    status: DocumentStatus = "draft"
    current_version_id: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentVersion(BaseModel):
    version_id: str
    document_id: str
    version_number: int
    content: str
    change_summary: str = ""
    source_run_id: str | None = None
    created_by: str
    created_at: datetime


class ReviewFinding(BaseModel):
    finding_id: str
    document_id: str
    version_id: str
    severity: FindingSeverity
    status: FindingStatus = "open"
    title: str
    description: str
    suggestion: str = ""
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class DocumentApproval(BaseModel):
    approval_id: str
    document_id: str
    version_id: str
    decision: ApprovalDecision
    comment: str = ""
    decided_by: str
    decided_at: datetime


class WorkspaceAuditEvent(BaseModel):
    event_id: str
    case_id: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


__all__ = [
    "ApprovalDecision",
    "CaseEvidence",
    "CaseStatus",
    "DocumentApproval",
    "DocumentStatus",
    "DocumentVersion",
    "FindingSeverity",
    "FindingStatus",
    "LegalCase",
    "LegalDocument",
    "ReviewFinding",
    "WorkspaceAuditEvent",
]
