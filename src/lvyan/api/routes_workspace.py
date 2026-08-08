"""案件工作空间 CRUD 路由（从 server.py 拆分）。

端点
----
- ``POST /api/cases`` — 创建案件
- ``GET  /api/cases`` — 列出案件
- ``GET  /api/cases/{case_id}`` — 获取案件详情
- ``GET  /api/cases/{case_id}/evidence`` — 列出案件证据
- ``POST /api/cases/{case_id}/evidence`` — 关联证据
- ``GET  /api/cases/{case_id}/documents`` — 列出案件文档
- ``POST /api/cases/{case_id}/documents`` — 创建文档
- ``GET  /api/legal-documents/{document_id}`` — 获取文档
- ``GET  /api/legal-documents/{document_id}/versions`` — 文档版本列表
- ``POST /api/legal-documents/{document_id}/versions`` — 创建文档版本
- ``GET  /api/legal-documents/{document_id}/findings`` — 审查发现列表
- ``POST /api/legal-documents/{document_id}/findings`` — 创建审查发现
- ``PATCH /api/review-findings/{finding_id}`` — 更新审查发现状态
- ``GET  /api/legal-documents/{document_id}/approvals`` — 审批列表
- ``POST /api/legal-documents/{document_id}/approvals`` — 提交审批决定
- ``GET  /api/cases/{case_id}/audit-events`` — 审计事件
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from lvyan.memory.case_workspace import CaseWorkspaceStore
from lvyan.schemas.workspace import (
    CaseEvidence,
    DocumentApproval,
    DocumentVersion,
    LegalCase,
    LegalDocument,
    ReviewFinding,
    WorkspaceAuditEvent,
)

from .auth import ANONYMOUS_USER, get_current_user_id, is_auth_enabled
from .models import (
    CaseCreateRequest,
    DocumentApprovalRequest,
    DocumentCreateRequest,
    DocumentVersionCreateRequest,
    EvidenceLinkRequest,
    ReviewFindingCreateRequest,
    ReviewFindingStatusRequest,
)

_logger = logging.getLogger("lvyan.api.routes_workspace")


def _workspace_missing() -> None:
    """资源不存在与无权访问使用同一响应，避免跨租户枚举。"""
    raise HTTPException(status_code=404, detail="资源不存在")


async def _workspace_call(method: Any, *args: Any) -> Any:
    """把同步仓库 I/O 移出事件循环，并隐藏底层数据库细节。"""
    try:
        return await asyncio.to_thread(method, *args)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _logger.exception("案件工作空间存储操作失败")
        raise HTTPException(status_code=503, detail="案件工作空间暂时不可用") from exc


def create_workspace_router(
    workspace_store: CaseWorkspaceStore,
    metadata_path_resolver: Any,
) -> APIRouter:
    """构造案件工作空间路由。

    Args:
        workspace_store: 案件工作空间仓库实例。
        metadata_path_resolver: 可调用对象，接受 file_id 返回元数据文件 Path。
    """
    router = APIRouter(tags=["workspace"])

    @router.post("/api/cases", response_model=LegalCase, status_code=201)
    async def create_case(
        req: CaseCreateRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> LegalCase:
        return await _workspace_call(
            workspace_store.create_case,
            user_id,
            req.title,
            req.description,
            req.thread_id,
        )

    @router.get("/api/cases", response_model=list[LegalCase])
    async def list_cases(
        user_id: str = Depends(get_current_user_id),
    ) -> list[LegalCase]:
        return await _workspace_call(workspace_store.list_cases, user_id)

    @router.get("/api/cases/{case_id}", response_model=LegalCase)
    async def get_case(
        case_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> LegalCase:
        case = await _workspace_call(workspace_store.get_case, user_id, case_id)
        if case is None:
            _workspace_missing()
        return case

    @router.get("/api/cases/{case_id}/evidence", response_model=list[CaseEvidence])
    async def list_case_evidence(
        case_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[CaseEvidence]:
        evidence = await _workspace_call(workspace_store.list_evidence, user_id, case_id)
        if evidence is None:
            _workspace_missing()
        return evidence

    @router.post(
        "/api/cases/{case_id}/evidence",
        response_model=CaseEvidence,
        status_code=201,
    )
    async def link_case_evidence(
        case_id: str,
        req: EvidenceLinkRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> CaseEvidence:
        meta_path = metadata_path_resolver(req.file_id)
        if not meta_path.is_file():
            _workspace_missing()
        try:
            attachment_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise HTTPException(status_code=503, detail="附件元数据暂时不可用") from exc
        if is_auth_enabled() and attachment_meta.get("user_id", ANONYMOUS_USER) != user_id:
            _workspace_missing()
        evidence = await _workspace_call(
            workspace_store.create_evidence,
            user_id,
            case_id,
            req.file_id,
            str(attachment_meta.get("filename", req.file_id)),
            req.evidence_type,
            req.summary,
            req.tags,
        )
        if evidence is None:
            _workspace_missing()
        return evidence

    @router.get("/api/cases/{case_id}/documents", response_model=list[LegalDocument])
    async def list_case_documents(
        case_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[LegalDocument]:
        documents = await _workspace_call(workspace_store.list_documents, user_id, case_id)
        if documents is None:
            _workspace_missing()
        return documents

    @router.post(
        "/api/cases/{case_id}/documents",
        response_model=dict[str, Any],
        status_code=201,
    )
    async def create_case_document(
        case_id: str,
        req: DocumentCreateRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> dict[str, Any]:
        result = await _workspace_call(
            workspace_store.create_document,
            user_id,
            case_id,
            req.title,
            req.document_type,
            req.content,
            req.source_run_id,
        )
        if result is None:
            _workspace_missing()
        document, version = result
        return {"document": document, "version": version}

    @router.get("/api/legal-documents/{document_id}", response_model=LegalDocument)
    async def get_legal_document(
        document_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> LegalDocument:
        document = await _workspace_call(workspace_store.get_document, user_id, document_id)
        if document is None:
            _workspace_missing()
        return document

    @router.get(
        "/api/legal-documents/{document_id}/versions",
        response_model=list[DocumentVersion],
    )
    async def list_document_versions(
        document_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[DocumentVersion]:
        versions = await _workspace_call(workspace_store.list_versions, user_id, document_id)
        if versions is None:
            _workspace_missing()
        return versions

    @router.post(
        "/api/legal-documents/{document_id}/versions",
        response_model=DocumentVersion,
        status_code=201,
    )
    async def create_document_version(
        document_id: str,
        req: DocumentVersionCreateRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> DocumentVersion:
        version = await _workspace_call(
            workspace_store.create_version,
            user_id,
            document_id,
            req.content,
            req.change_summary,
            req.source_run_id,
        )
        if version is None:
            _workspace_missing()
        return version

    @router.get(
        "/api/legal-documents/{document_id}/findings",
        response_model=list[ReviewFinding],
    )
    async def list_review_findings(
        document_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[ReviewFinding]:
        findings = await _workspace_call(workspace_store.list_findings, user_id, document_id)
        if findings is None:
            _workspace_missing()
        return findings

    @router.post(
        "/api/legal-documents/{document_id}/findings",
        response_model=ReviewFinding,
        status_code=201,
    )
    async def create_review_finding(
        document_id: str,
        req: ReviewFindingCreateRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> ReviewFinding:
        finding = await _workspace_call(
            workspace_store.create_finding,
            user_id,
            document_id,
            req.version_id,
            req.severity,
            req.title,
            req.description,
            req.suggestion,
            req.source_refs,
        )
        if finding is None:
            _workspace_missing()
        return finding

    @router.patch("/api/review-findings/{finding_id}", response_model=ReviewFinding)
    async def update_review_finding(
        finding_id: str,
        req: ReviewFindingStatusRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> ReviewFinding:
        finding = await _workspace_call(
            workspace_store.resolve_finding,
            user_id,
            finding_id,
            req.status,
        )
        if finding is None:
            _workspace_missing()
        return finding

    @router.get(
        "/api/legal-documents/{document_id}/approvals",
        response_model=list[DocumentApproval],
    )
    async def list_document_approvals(
        document_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[DocumentApproval]:
        approvals = await _workspace_call(workspace_store.list_approvals, user_id, document_id)
        if approvals is None:
            _workspace_missing()
        return approvals

    @router.post(
        "/api/legal-documents/{document_id}/approvals",
        response_model=DocumentApproval,
        status_code=201,
    )
    async def decide_document_approval(
        document_id: str,
        req: DocumentApprovalRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> DocumentApproval:
        approval = await _workspace_call(
            workspace_store.decide_approval,
            user_id,
            document_id,
            req.version_id,
            req.decision,
            req.comment,
        )
        if approval is None:
            _workspace_missing()
        return approval

    @router.get(
        "/api/cases/{case_id}/audit-events",
        response_model=list[WorkspaceAuditEvent],
    )
    async def list_case_audit_events(
        case_id: str,
        user_id: str = Depends(get_current_user_id),
    ) -> list[WorkspaceAuditEvent]:
        events = await _workspace_call(workspace_store.list_audit_events, user_id, case_id)
        if events is None:
            _workspace_missing()
        return events

    return router
