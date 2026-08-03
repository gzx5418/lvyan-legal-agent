"""数据模型层：案件状态、法律权威、证据、输出等 Pydantic v2 模型。

统一导出入口，支持 ``from lvyan.schemas import CaseState, Authority, ...``。
"""

from __future__ import annotations

from .authority import Authority
from .case import (
    CaseState,
    DocumentRef,
    Fact,
    MissingFact,
    PlanStep,
    RetrievalQuery,
    TimelineEvent,
)
from .evidence import AuthorityConflict, CaseAuthority, EvidenceRequirement
from .legal_answer import (
    ActionItem,
    AnswerMeta,
    CitationLevel,
    EvidenceItem as LegalEvidenceItem,
    ExecutiveSummary,
    FactItem,
    FactStatus,
    LegalAnswerV1,
    LegalCitation,
    LegalIssue,
    RiskItem,
    UncertaintyItem,
)
from .output import CitationAudit, CitationDetail, OutputMode, ReasoningResult

__all__ = [
    # attachment.py
    "AttachmentChunk",
    # case.py
    "CaseState",
    "Fact",
    "TimelineEvent",
    "MissingFact",
    "DocumentRef",
    "PlanStep",
    "RetrievalQuery",
    # authority.py
    "Authority",
    # evidence.py
    "CaseAuthority",
    "EvidenceRequirement",
    "AuthorityConflict",
    # output.py
    "CitationAudit",
    "CitationDetail",
    "ReasoningResult",
    "OutputMode",
    # legal_answer.py
    "LegalAnswerV1",
    "AnswerMeta",
    "ExecutiveSummary",
    "FactItem",
    "FactStatus",
    "LegalIssue",
    "LegalEvidenceItem",
    "RiskItem",
    "ActionItem",
    "LegalCitation",
    "CitationLevel",
    "UncertaintyItem",
]
