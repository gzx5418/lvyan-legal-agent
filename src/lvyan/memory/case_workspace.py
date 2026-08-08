"""案件工作空间的开发内存仓库与生产 PostgreSQL 仓库。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from lvyan.memory.run_metadata import PostgresRunMetadataStore
from lvyan.schemas.workspace import (
    CaseEvidence,
    DocumentApproval,
    DocumentVersion,
    LegalCase,
    LegalDocument,
    ReviewFinding,
    WorkspaceAuditEvent,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class CaseWorkspaceStore(Protocol):
    def create_case(
        self, user_id: str, title: str, description: str = "", thread_id: str | None = None
    ) -> LegalCase: ...
    def list_cases(self, user_id: str) -> list[LegalCase]: ...
    def get_case(self, user_id: str, case_id: str) -> LegalCase | None: ...
    def create_evidence(
        self,
        user_id: str,
        case_id: str,
        file_id: str,
        filename: str,
        evidence_type: str,
        summary: str,
        tags: list[str],
    ) -> CaseEvidence | None: ...
    def list_evidence(self, user_id: str, case_id: str) -> list[CaseEvidence] | None: ...
    def create_document(
        self,
        user_id: str,
        case_id: str,
        title: str,
        document_type: str,
        content: str,
        source_run_id: str | None = None,
    ) -> tuple[LegalDocument, DocumentVersion] | None: ...
    def get_document(self, user_id: str, document_id: str) -> LegalDocument | None: ...
    def list_documents(self, user_id: str, case_id: str) -> list[LegalDocument] | None: ...
    def create_version(
        self,
        user_id: str,
        document_id: str,
        content: str,
        change_summary: str = "",
        source_run_id: str | None = None,
    ) -> DocumentVersion | None: ...
    def list_versions(self, user_id: str, document_id: str) -> list[DocumentVersion] | None: ...
    def create_finding(
        self,
        user_id: str,
        document_id: str,
        version_id: str,
        severity: str,
        title: str,
        description: str,
        suggestion: str = "",
        source_refs: list[dict[str, Any]] | None = None,
    ) -> ReviewFinding | None: ...
    def list_findings(self, user_id: str, document_id: str) -> list[ReviewFinding] | None: ...
    def resolve_finding(
        self, user_id: str, finding_id: str, status: str
    ) -> ReviewFinding | None: ...
    def decide_approval(
        self, user_id: str, document_id: str, version_id: str, decision: str, comment: str = ""
    ) -> DocumentApproval | None: ...
    def list_approvals(self, user_id: str, document_id: str) -> list[DocumentApproval] | None: ...
    def list_audit_events(self, user_id: str, case_id: str) -> list[WorkspaceAuditEvent] | None: ...


class InMemoryCaseWorkspaceStore:
    """开发和测试使用的进程内仓库；进程重启后数据会清空。"""

    def __init__(self) -> None:
        self._cases: dict[str, LegalCase] = {}
        self._evidence: dict[str, CaseEvidence] = {}
        self._documents: dict[str, LegalDocument] = {}
        self._versions: dict[str, DocumentVersion] = {}
        self._findings: dict[str, ReviewFinding] = {}
        self._approvals: dict[str, DocumentApproval] = {}
        self._audit: list[WorkspaceAuditEvent] = []
        self._lock = threading.RLock()

    def _case(self, user_id: str, case_id: str) -> LegalCase | None:
        item = self._cases.get(case_id)
        return item if item and item.user_id == user_id else None

    def _document(self, user_id: str, document_id: str) -> LegalDocument | None:
        document = self._documents.get(document_id)
        return document if document and self._case(user_id, document.case_id) else None

    def _audit_event(
        self,
        case_id: str,
        actor_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._audit.append(
            WorkspaceAuditEvent(
                event_id=_id("audit"),
                case_id=case_id,
                actor_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload or {},
                created_at=_now(),
            )
        )

    def create_case(
        self, user_id: str, title: str, description: str = "", thread_id: str | None = None
    ) -> LegalCase:
        now = _now()
        case = LegalCase(
            case_id=_id("case"),
            user_id=user_id,
            title=title,
            description=description,
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._cases[case.case_id] = case
            self._audit_event(case.case_id, user_id, "case.created", "case", case.case_id)
        return case

    def list_cases(self, user_id: str) -> list[LegalCase]:
        with self._lock:
            return sorted(
                (item for item in self._cases.values() if item.user_id == user_id),
                key=lambda item: item.updated_at,
                reverse=True,
            )

    def get_case(self, user_id: str, case_id: str) -> LegalCase | None:
        with self._lock:
            return self._case(user_id, case_id)

    def create_evidence(
        self,
        user_id: str,
        case_id: str,
        file_id: str,
        filename: str,
        evidence_type: str,
        summary: str,
        tags: list[str],
    ) -> CaseEvidence | None:
        with self._lock:
            case = self._case(user_id, case_id)
            if case is None:
                return None
            existing = next(
                (
                    item
                    for item in self._evidence.values()
                    if item.case_id == case_id and item.file_id == file_id
                ),
                None,
            )
            if existing is not None:
                return existing
            evidence = CaseEvidence(
                evidence_id=_id("evidence"),
                case_id=case_id,
                file_id=file_id,
                filename=filename,
                evidence_type=evidence_type,
                summary=summary,
                tags=tags,
                created_at=_now(),
            )
            self._evidence[evidence.evidence_id] = evidence
            self._cases[case_id] = case.model_copy(update={"updated_at": _now()})
            self._audit_event(
                case_id,
                user_id,
                "evidence.linked",
                "evidence",
                evidence.evidence_id,
                {"file_id": file_id},
            )
            return evidence

    def list_evidence(self, user_id: str, case_id: str) -> list[CaseEvidence] | None:
        with self._lock:
            if self._case(user_id, case_id) is None:
                return None
            return sorted(
                (item for item in self._evidence.values() if item.case_id == case_id),
                key=lambda item: item.created_at,
            )

    def create_document(
        self,
        user_id: str,
        case_id: str,
        title: str,
        document_type: str,
        content: str,
        source_run_id: str | None = None,
    ) -> tuple[LegalDocument, DocumentVersion] | None:
        with self._lock:
            case = self._case(user_id, case_id)
            if case is None:
                return None
            now = _now()
            document = LegalDocument(
                document_id=_id("document"),
                case_id=case_id,
                title=title,
                document_type=document_type,
                created_at=now,
                updated_at=now,
            )
            version = DocumentVersion(
                version_id=_id("version"),
                document_id=document.document_id,
                version_number=1,
                content=content,
                source_run_id=source_run_id,
                created_by=user_id,
                created_at=now,
            )
            document = document.model_copy(update={"current_version_id": version.version_id})
            self._documents[document.document_id] = document
            self._versions[version.version_id] = version
            self._cases[case_id] = case.model_copy(update={"updated_at": now})
            self._audit_event(
                case_id,
                user_id,
                "document.created",
                "document",
                document.document_id,
                {"version_id": version.version_id},
            )
            return document, version

    def get_document(self, user_id: str, document_id: str) -> LegalDocument | None:
        with self._lock:
            return self._document(user_id, document_id)

    def list_documents(self, user_id: str, case_id: str) -> list[LegalDocument] | None:
        with self._lock:
            if self._case(user_id, case_id) is None:
                return None
            return sorted(
                (item for item in self._documents.values() if item.case_id == case_id),
                key=lambda item: item.updated_at,
                reverse=True,
            )

    def create_version(
        self,
        user_id: str,
        document_id: str,
        content: str,
        change_summary: str = "",
        source_run_id: str | None = None,
    ) -> DocumentVersion | None:
        with self._lock:
            document = self._document(user_id, document_id)
            if document is None:
                return None
            existing = [
                item.version_number
                for item in self._versions.values()
                if item.document_id == document_id
            ]
            version = DocumentVersion(
                version_id=_id("version"),
                document_id=document_id,
                version_number=max(existing, default=0) + 1,
                content=content,
                change_summary=change_summary,
                source_run_id=source_run_id,
                created_by=user_id,
                created_at=_now(),
            )
            self._versions[version.version_id] = version
            self._documents[document_id] = document.model_copy(
                update={
                    "current_version_id": version.version_id,
                    "status": "draft",
                    "updated_at": _now(),
                }
            )
            self._audit_event(
                document.case_id,
                user_id,
                "document.version_created",
                "document_version",
                version.version_id,
                {"document_id": document_id},
            )
            return version

    def list_versions(self, user_id: str, document_id: str) -> list[DocumentVersion] | None:
        with self._lock:
            if self._document(user_id, document_id) is None:
                return None
            return sorted(
                (item for item in self._versions.values() if item.document_id == document_id),
                key=lambda item: item.version_number,
                reverse=True,
            )

    def create_finding(
        self,
        user_id: str,
        document_id: str,
        version_id: str,
        severity: str,
        title: str,
        description: str,
        suggestion: str = "",
        source_refs: list[dict[str, Any]] | None = None,
    ) -> ReviewFinding | None:
        with self._lock:
            document = self._document(user_id, document_id)
            version = self._versions.get(version_id)
            if document is None or version is None or version.document_id != document_id:
                return None
            finding = ReviewFinding(
                finding_id=_id("finding"),
                document_id=document_id,
                version_id=version_id,
                severity=severity,
                title=title,
                description=description,
                suggestion=suggestion,
                source_refs=source_refs or [],
                created_at=_now(),
            )
            self._findings[finding.finding_id] = finding
            self._documents[document_id] = document.model_copy(
                update={"status": "in_review", "updated_at": _now()}
            )
            self._audit_event(
                document.case_id,
                user_id,
                "review.finding_created",
                "review_finding",
                finding.finding_id,
                {"severity": severity},
            )
            return finding

    def list_findings(self, user_id: str, document_id: str) -> list[ReviewFinding] | None:
        with self._lock:
            if self._document(user_id, document_id) is None:
                return None
            return sorted(
                (item for item in self._findings.values() if item.document_id == document_id),
                key=lambda item: item.created_at,
                reverse=True,
            )

    def resolve_finding(self, user_id: str, finding_id: str, status: str) -> ReviewFinding | None:
        with self._lock:
            finding = self._findings.get(finding_id)
            document = self._documents.get(finding.document_id) if finding else None
            if finding is None or document is None or self._case(user_id, document.case_id) is None:
                return None
            updated = finding.model_copy(
                update={"status": status, "resolved_by": user_id, "resolved_at": _now()}
            )
            self._findings[finding_id] = updated
            self._audit_event(
                document.case_id, user_id, f"review.finding_{status}", "review_finding", finding_id
            )
            return updated

    def decide_approval(
        self, user_id: str, document_id: str, version_id: str, decision: str, comment: str = ""
    ) -> DocumentApproval | None:
        with self._lock:
            document = self._document(user_id, document_id)
            version = self._versions.get(version_id)
            if document is None or version is None or version.document_id != document_id:
                return None
            if decision == "approved" and any(
                item.document_id == document_id
                and item.version_id == version_id
                and item.status == "open"
                and item.severity in {"high", "critical"}
                for item in self._findings.values()
            ):
                raise ValueError("存在未处理的高风险审阅问题，不能批准")
            approval = DocumentApproval(
                approval_id=_id("approval"),
                document_id=document_id,
                version_id=version_id,
                decision=decision,
                comment=comment,
                decided_by=user_id,
                decided_at=_now(),
            )
            self._approvals[approval.approval_id] = approval
            next_status = "approved" if decision == "approved" else "draft"
            self._documents[document_id] = document.model_copy(
                update={
                    "status": next_status,
                    "current_version_id": version_id,
                    "updated_at": _now(),
                }
            )
            self._audit_event(
                document.case_id,
                user_id,
                f"document.{decision}",
                "document_approval",
                approval.approval_id,
                {"version_id": version_id},
            )
            return approval

    def list_approvals(self, user_id: str, document_id: str) -> list[DocumentApproval] | None:
        with self._lock:
            if self._document(user_id, document_id) is None:
                return None
            return sorted(
                (item for item in self._approvals.values() if item.document_id == document_id),
                key=lambda item: item.decided_at,
                reverse=True,
            )

    def list_audit_events(self, user_id: str, case_id: str) -> list[WorkspaceAuditEvent] | None:
        with self._lock:
            if self._case(user_id, case_id) is None:
                return None
            return sorted(
                (item for item in self._audit if item.case_id == case_id),
                key=lambda item: item.created_at,
                reverse=True,
            )


class PostgresCaseWorkspaceStore:
    """生产用 PostgreSQL 仓库；复用统一 migration 与连接策略。"""

    def __init__(self, dsn: str | None = None) -> None:
        self._metadata = PostgresRunMetadataStore(dsn)

    def _connect(self):
        return self._metadata._connect()

    def _ensure_schema(self, conn: Any) -> None:
        self._metadata._ensure_schema(conn)

    @staticmethod
    def _case_model(row: dict[str, Any]) -> LegalCase:
        return LegalCase.model_validate(dict(row))

    @staticmethod
    def _document_model(row: dict[str, Any]) -> LegalDocument:
        return LegalDocument.model_validate(dict(row))

    def _audit(
        self,
        cur: Any,
        case_id: str,
        actor_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        cur.execute(
            "INSERT INTO workspace_audit_events (event_id, case_id, actor_id, action, entity_type, entity_id, payload) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (_id("audit"), case_id, actor_id, action, entity_type, entity_id, Jsonb(payload or {})),
        )

    def create_case(
        self, user_id: str, title: str, description: str = "", thread_id: str | None = None
    ) -> LegalCase:
        case_id = _id("case")
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO legal_cases (case_id, user_id, title, description, thread_id) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                    (case_id, user_id, title, description, thread_id),
                )
                row = cur.fetchone()
                self._audit(cur, case_id, user_id, "case.created", "case", case_id)
        return self._case_model(row)

    def list_cases(self, user_id: str) -> list[LegalCase]:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM legal_cases WHERE user_id = %s ORDER BY updated_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
        return [self._case_model(row) for row in rows]

    def get_case(self, user_id: str, case_id: str) -> LegalCase | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM legal_cases WHERE case_id = %s AND user_id = %s",
                    (case_id, user_id),
                )
                row = cur.fetchone()
        return self._case_model(row) if row else None

    def create_evidence(
        self,
        user_id: str,
        case_id: str,
        file_id: str,
        filename: str,
        evidence_type: str,
        summary: str,
        tags: list[str],
    ) -> CaseEvidence | None:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT case_id FROM legal_cases WHERE case_id = %s AND user_id = %s FOR UPDATE",
                    (case_id, user_id),
                )
                if cur.fetchone() is None:
                    return None
                evidence_id = _id("evidence")
                cur.execute(
                    "INSERT INTO case_evidence (evidence_id, case_id, file_id, filename, evidence_type, summary, tags) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (case_id, file_id) DO UPDATE SET filename = EXCLUDED.filename RETURNING *",
                    (evidence_id, case_id, file_id, filename, evidence_type, summary, Jsonb(tags)),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE legal_cases SET updated_at = now() WHERE case_id = %s", (case_id,)
                )
                self._audit(
                    cur,
                    case_id,
                    user_id,
                    "evidence.linked",
                    "evidence",
                    str(row["evidence_id"]),
                    {"file_id": file_id},
                )
        return CaseEvidence.model_validate(dict(row))

    def list_evidence(self, user_id: str, case_id: str) -> list[CaseEvidence] | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM legal_cases WHERE case_id = %s AND user_id = %s",
                    (case_id, user_id),
                )
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "SELECT * FROM case_evidence WHERE case_id = %s ORDER BY created_at", (case_id,)
                )
                rows = cur.fetchall()
        return [CaseEvidence.model_validate(dict(row)) for row in rows]

    def create_document(
        self,
        user_id: str,
        case_id: str,
        title: str,
        document_type: str,
        content: str,
        source_run_id: str | None = None,
    ) -> tuple[LegalDocument, DocumentVersion] | None:
        document_id, version_id = _id("document"), _id("version")
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM legal_cases WHERE case_id = %s AND user_id = %s FOR UPDATE",
                    (case_id, user_id),
                )
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "INSERT INTO legal_documents (document_id, case_id, title, document_type, current_version_id) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                    (document_id, case_id, title, document_type, version_id),
                )
                document_row = cur.fetchone()
                cur.execute(
                    "INSERT INTO document_versions (version_id, document_id, version_number, content, source_run_id, created_by) VALUES (%s, %s, 1, %s, %s, %s) RETURNING *",
                    (version_id, document_id, content, source_run_id, user_id),
                )
                version_row = cur.fetchone()
                cur.execute(
                    "UPDATE legal_cases SET updated_at = now() WHERE case_id = %s", (case_id,)
                )
                self._audit(
                    cur,
                    case_id,
                    user_id,
                    "document.created",
                    "document",
                    document_id,
                    {"version_id": version_id},
                )
        return self._document_model(document_row), DocumentVersion.model_validate(dict(version_row))

    def get_document(self, user_id: str, document_id: str) -> LegalDocument | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.* FROM legal_documents d JOIN legal_cases c ON c.case_id = d.case_id WHERE d.document_id = %s AND c.user_id = %s",
                    (document_id, user_id),
                )
                row = cur.fetchone()
        return self._document_model(row) if row else None

    def list_documents(self, user_id: str, case_id: str) -> list[LegalDocument] | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM legal_cases WHERE case_id = %s AND user_id = %s",
                    (case_id, user_id),
                )
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "SELECT * FROM legal_documents WHERE case_id = %s ORDER BY updated_at DESC",
                    (case_id,),
                )
                rows = cur.fetchall()
        return [self._document_model(row) for row in rows]

    def create_version(
        self,
        user_id: str,
        document_id: str,
        content: str,
        change_summary: str = "",
        source_run_id: str | None = None,
    ) -> DocumentVersion | None:
        version_id = _id("version")
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT d.case_id FROM legal_documents d JOIN legal_cases c ON c.case_id = d.case_id WHERE d.document_id = %s AND c.user_id = %s FOR UPDATE",
                    (document_id, user_id),
                )
                document = cur.fetchone()
                if document is None:
                    return None
                cur.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number FROM document_versions WHERE document_id = %s",
                    (document_id,),
                )
                next_number = int(cur.fetchone()["next_number"])
                cur.execute(
                    "INSERT INTO document_versions (version_id, document_id, version_number, content, change_summary, source_run_id, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        version_id,
                        document_id,
                        next_number,
                        content,
                        change_summary,
                        source_run_id,
                        user_id,
                    ),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE legal_documents SET current_version_id = %s, status = 'draft', updated_at = now() WHERE document_id = %s",
                    (version_id, document_id),
                )
                self._audit(
                    cur,
                    str(document["case_id"]),
                    user_id,
                    "document.version_created",
                    "document_version",
                    version_id,
                    {"document_id": document_id},
                )
        return DocumentVersion.model_validate(dict(row))

    def list_versions(self, user_id: str, document_id: str) -> list[DocumentVersion] | None:
        if self.get_document(user_id, document_id) is None:
            return None
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_versions WHERE document_id = %s ORDER BY version_number DESC",
                    (document_id,),
                )
                rows = cur.fetchall()
        return [DocumentVersion.model_validate(dict(row)) for row in rows]

    def create_finding(
        self,
        user_id: str,
        document_id: str,
        version_id: str,
        severity: str,
        title: str,
        description: str,
        suggestion: str = "",
        source_refs: list[dict[str, Any]] | None = None,
    ) -> ReviewFinding | None:
        from psycopg.types.json import Jsonb

        finding_id = _id("finding")
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT d.case_id FROM legal_documents d JOIN legal_cases c ON c.case_id = d.case_id JOIN document_versions v ON v.version_id = %s AND v.document_id = d.document_id WHERE d.document_id = %s AND c.user_id = %s FOR UPDATE",
                    (version_id, document_id, user_id),
                )
                document = cur.fetchone()
                if document is None:
                    return None
                cur.execute(
                    "INSERT INTO review_findings (finding_id, document_id, version_id, severity, title, description, suggestion, source_refs) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        finding_id,
                        document_id,
                        version_id,
                        severity,
                        title,
                        description,
                        suggestion,
                        Jsonb(source_refs or []),
                    ),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE legal_documents SET status = 'in_review', updated_at = now() WHERE document_id = %s",
                    (document_id,),
                )
                self._audit(
                    cur,
                    str(document["case_id"]),
                    user_id,
                    "review.finding_created",
                    "review_finding",
                    finding_id,
                    {"severity": severity},
                )
        return ReviewFinding.model_validate(dict(row))

    def list_findings(self, user_id: str, document_id: str) -> list[ReviewFinding] | None:
        if self.get_document(user_id, document_id) is None:
            return None
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM review_findings WHERE document_id = %s ORDER BY created_at DESC",
                    (document_id,),
                )
                rows = cur.fetchall()
        return [ReviewFinding.model_validate(dict(row)) for row in rows]

    def resolve_finding(self, user_id: str, finding_id: str, status: str) -> ReviewFinding | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT d.case_id FROM review_findings f JOIN legal_documents d ON d.document_id = f.document_id JOIN legal_cases c ON c.case_id = d.case_id WHERE f.finding_id = %s AND c.user_id = %s FOR UPDATE",
                    (finding_id, user_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(
                    "UPDATE review_findings SET status = %s, resolved_by = %s, resolved_at = now() WHERE finding_id = %s RETURNING *",
                    (status, user_id, finding_id),
                )
                finding = cur.fetchone()
                self._audit(
                    cur,
                    str(row["case_id"]),
                    user_id,
                    f"review.finding_{status}",
                    "review_finding",
                    finding_id,
                )
        return ReviewFinding.model_validate(dict(finding))

    def decide_approval(
        self, user_id: str, document_id: str, version_id: str, decision: str, comment: str = ""
    ) -> DocumentApproval | None:
        approval_id = _id("approval")
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT d.case_id FROM legal_documents d JOIN legal_cases c ON c.case_id = d.case_id JOIN document_versions v ON v.version_id = %s AND v.document_id = d.document_id WHERE d.document_id = %s AND c.user_id = %s FOR UPDATE",
                    (version_id, document_id, user_id),
                )
                document = cur.fetchone()
                if document is None:
                    return None
                if decision == "approved":
                    cur.execute(
                        "SELECT 1 FROM review_findings WHERE document_id = %s AND version_id = %s AND status = 'open' AND severity IN ('high', 'critical') LIMIT 1",
                        (document_id, version_id),
                    )
                    if cur.fetchone() is not None:
                        raise ValueError("存在未处理的高风险审阅问题，不能批准")
                cur.execute(
                    "INSERT INTO document_approvals (approval_id, document_id, version_id, decision, comment, decided_by) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    (approval_id, document_id, version_id, decision, comment, user_id),
                )
                row = cur.fetchone()
                status = "approved" if decision == "approved" else "draft"
                cur.execute(
                    "UPDATE legal_documents SET status = %s, current_version_id = %s, updated_at = now() WHERE document_id = %s",
                    (status, version_id, document_id),
                )
                self._audit(
                    cur,
                    str(document["case_id"]),
                    user_id,
                    f"document.{decision}",
                    "document_approval",
                    approval_id,
                    {"version_id": version_id},
                )
        return DocumentApproval.model_validate(dict(row))

    def list_approvals(self, user_id: str, document_id: str) -> list[DocumentApproval] | None:
        if self.get_document(user_id, document_id) is None:
            return None
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_approvals WHERE document_id = %s ORDER BY decided_at DESC",
                    (document_id,),
                )
                rows = cur.fetchall()
        return [DocumentApproval.model_validate(dict(row)) for row in rows]

    def list_audit_events(self, user_id: str, case_id: str) -> list[WorkspaceAuditEvent] | None:
        if self.get_case(user_id, case_id) is None:
            return None
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM workspace_audit_events WHERE case_id = %s ORDER BY created_at DESC",
                    (case_id,),
                )
                rows = cur.fetchall()
        return [WorkspaceAuditEvent.model_validate(dict(row)) for row in rows]


__all__ = ["CaseWorkspaceStore", "InMemoryCaseWorkspaceStore", "PostgresCaseWorkspaceStore"]
