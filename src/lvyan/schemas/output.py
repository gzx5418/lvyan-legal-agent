"""输出与审计模型。

包含引用审计（``CitationAudit`` / ``CitationDetail``）、推理结果（``ReasoningResult``）
与输出模式（``OutputMode``）。

设计原则：在概率校准完成前，``ReasoningResult`` **不**包含任何数字概率 / 百分比字段，
仅保留定性趋势判断，避免给用户呈现未经验证的「胜诉率」等数字。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CitationDetail(BaseModel):
    """单条引用的核对结果。"""

    citation_text: str
    status: Literal["verified", "fabricated", "repealed", "unsupported", "mismatched"]
    matched_source_id: str | None = None
    note: str | None = None


class CitationAudit(BaseModel):
    """引用审计汇总：核对最终输出中所有法条 / 判例引用的真实性。"""

    passed: bool
    total_citations: int
    verified: int
    fabricated: int
    repealed_cited: int
    unsupported: int
    details: list[CitationDetail]
    reretrieval_count: int = 0


class ReasoningResult(BaseModel):
    """法律推理结果。

    仅含定性判断，**禁止**包含任何数字概率 / 百分比字段
    （如 ``probability`` / ``win_rate`` / ``percentage``），在校准流程落地前不引入。
    """

    legal_relationship: str | None = None  # 法律关系定性
    elements: list[str] = []  # 常要件
    disputed_focus: list[str] = []  # 争议焦点
    plaintiff_arguments: list[str] = []
    defendant_arguments: list[str] = []
    evidence_mapping: list[str] = []  # 证据-要件映射
    judicial_tendency: Literal[
        "favorable",
        "somewhat_favorable",
        "even",
        "somewhat_unfavorable",
        "insufficient",
    ]
    evidence_confidence: Literal["high", "medium", "low"]
    key_factors: list[str] = []


OutputMode = Literal["light", "deep", "document"]


__all__ = [
    "CitationDetail",
    "CitationAudit",
    "ReasoningResult",
    "OutputMode",
]
