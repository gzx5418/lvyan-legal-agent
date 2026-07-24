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
from .output import CitationAudit, CitationDetail, OutputMode, ReasoningResult

__all__ = [
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
]
