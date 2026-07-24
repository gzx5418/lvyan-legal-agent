"""证据与判例权威模型。

包含先例案件（``CaseAuthority``）、证据要求（``EvidenceRequirement``）与
权威冲突（``AuthorityConflict``）。三者均不依赖 schemas 内其它模型，
``CaseState`` 从本模块导入 ``CaseAuthority`` / ``EvidenceRequirement`` / ``AuthorityConflict``。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel


class CaseAuthority(BaseModel):
    """先例案件：可作为裁判说理参照的生效判例。"""

    case_id: str
    case_number: str | None = None  # 案号
    court: str
    case_type: str
    brief_facts: str
    ruling_summary: str
    ruling_date: date | None = None
    similarity_score: float
    source_url: str | None = None


class EvidenceRequirement(BaseModel):
    """证据要求：为证明某一要件事实所需的证据集合及其当前满足情况。"""

    requirement_id: str
    fact_to_prove: str
    evidence_types: list[str]
    current_status: Literal["met", "partial", "missing"]
    gap_description: str | None = None


class AuthorityConflict(BaseModel):
    """权威冲突：多条权威条目之间存在版本 / 位阶 / 管辖权冲突。"""

    conflict_id: str
    authority_ids: list[str]
    conflict_type: Literal["version", "hierarchy", "jurisdiction"]
    description: str
    resolution: str | None = None


__all__ = ["CaseAuthority", "EvidenceRequirement", "AuthorityConflict"]
