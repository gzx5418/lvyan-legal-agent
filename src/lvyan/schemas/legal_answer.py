"""LegalAnswerV1：法律 Agent 最终输出的结构化数据协议。

设计原则：
1. 所有法律结论、事实、证据、争点、法条、行动建议均为显式字段，不依赖自由文本。
2. 禁止字段化「胜诉率」「概率百分比」等未经校准的数字，风险仅用定性维度表达。
3. 法条引用必须包含完整名称 + 条款序号 + 效力状态，便于审计防幻觉。
4. 事实必须标注四态来源（已确认/用户陈述/系统推断/缺失），防止把推断写成事实。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AnswerMeta(BaseModel):
    """输出顶部身份与适用范围信息（程序生成，不由模型自由修改）。"""

    title: str
    jurisdiction: str
    case_type: str
    law_as_of_date: str
    risk_level: Literal["low", "medium", "high"]
    material_completeness: Literal["complete", "partial", "insufficient"]
    analysis_mode: Literal["light", "deep", "document"] = "deep"


class ExecutiveSummary(BaseModel):
    """核心判断摘要（3-5 条，普通用户优先看到）。"""

    conclusion: str
    key_reasons: list[str] = []
    main_uncertainty: str


FactStatus = Literal["confirmed", "claimed", "inferred", "missing"]


class FactItem(BaseModel):
    """单条事实，带来源状态标签。

    - confirmed: 有合同/转账/聊天记录等支撑
    - claimed: 仅来自用户描述
    - inferred: Agent 根据上下文推测
    - missing: 会影响结论但尚未提供
    """

    fact_id: str
    content: str
    status: FactStatus
    source_ref: str | None = None
    detail: str | None = None


class LegalIssue(BaseModel):
    """单个争点，采用结论-规则-分析-反方-小结五段式。"""

    issue_id: str
    question: str
    conclusion: str
    rules: list[str] = []
    supporting_facts: list[str] = []
    analysis: str = ""
    counterarguments: list[str] = []


class EvidenceItem(BaseModel):
    """证据分析表的单行。"""

    evidence_id: str
    name: str
    purpose: str
    status: Literal["provided", "missing", "partial"]
    probative_force: Literal["key", "strong", "medium", "weak"]
    next_step: str | None = None


class RiskItem(BaseModel):
    """风险的单一维度评估。score 可选，仅用于可计算指标。"""

    dimension: str
    rating: Literal["high", "medium", "low"]
    detail: str
    score: float | None = None


class ActionItem(BaseModel):
    """下一步行动的单项。"""

    phase: Literal["immediate", "short_term", "contingency"]
    description: str
    target: str
    required_materials: list[str] = []
    deadline: str | None = None
    risk: str | None = None


CitationLevel = Literal[
    "law",
    "regulation",
    "judicial_interpretation",
    "guiding_case",
    "reference_case",
    "normative",
]


class LegalCitation(BaseModel):
    """单条法律引用卡片。正文显示名称+条款，详情折叠展示。"""

    citation_id: str
    full_name: str
    article_number: str
    article_text: str = ""
    level: CitationLevel
    status: Literal["effective", "repealed", "not_yet_effective", "unknown"] = "unknown"
    effective_date: str | None = None
    official_source: str | None = None
    role_in_analysis: str | None = None


class UncertaintyItem(BaseModel):
    """显式声明的不确定点，避免模型伪装确定。"""

    description: str
    impact: str
    resolution: str | None = None


class LegalAnswerV1(BaseModel):
    """法律 Agent 最终结构化输出协议。"""

    schema_version: Literal["legal_answer_v1"] = "legal_answer_v1"
    meta: AnswerMeta
    executive_summary: ExecutiveSummary
    facts: list[FactItem] = []
    issues: list[LegalIssue] = []
    evidence: list[EvidenceItem] = []
    risks: list[RiskItem] = []
    action_plan: list[ActionItem] = []
    citations: list[LegalCitation] = []
    uncertainties: list[UncertaintyItem] = []
    disclaimer: str


__all__ = [
    "LegalAnswerV1",
    "AnswerMeta",
    "ExecutiveSummary",
    "FactItem",
    "FactStatus",
    "LegalIssue",
    "EvidenceItem",
    "RiskItem",
    "ActionItem",
    "LegalCitation",
    "CitationLevel",
    "UncertaintyItem",
]
