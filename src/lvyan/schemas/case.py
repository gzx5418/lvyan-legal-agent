"""案件状态与核心辅助数据模型。

``CaseState`` 是律言 Agent 在 LangGraph 各节点之间流转的唯一状态载体，
聚合事实、时间线、计划、检索结果、权威条目、证据要求、冲突、推理结果与引用审计等全部
中间产物。

导入依赖方向（无循环）：
    authority.py  <- (无内部依赖)
    evidence.py   <- (无内部依赖)
    output.py     <- (无内部依赖)
    case.py       <- authority.py, evidence.py, output.py
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .authority import Authority
from .evidence import AuthorityConflict, CaseAuthority, EvidenceRequirement
from .output import CitationAudit, ReasoningResult


# ---------------------------------------------------------------------------
# 辅助模型
# ---------------------------------------------------------------------------
class Fact(BaseModel):
    """单条事实陈述。"""

    fact_id: str
    category: Literal["当事人", "时间", "金额", "行为", "证据", "其他"]
    content: str
    # P0-3 修复：新增 "llm" 与 "document" 来源
    # - "user"：用户原始陈述
    # - "extracted"：规则/正则抽取
    # - "llm"：LLM 结构化抽取（与规则降级区分）
    # - "document"：从上传文档解析（markitdown 转换后）
    source: Literal["user", "extracted", "llm", "document"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # 原始证据片段（LLM 抽取或文档解析时填，便于追溯）
    source_ref: str | None = None
    evidence_span: str | None = None


class TimelineEvent(BaseModel):
    """时间线上的单个事件节点。"""

    event_id: str
    date: str | None = None  # 用 str 兼容「2023年5月」「2023-05-01前后」等模糊表述
    description: str
    involved_parties: list[str] = []


class MissingFact(BaseModel):
    """缺失事实：需向用户追问的关键事实点。"""

    fact_key: str
    question: str
    reason: str
    is_blocking: bool = False


class DocumentRef(BaseModel):
    """已上传文档的引用（指向对象存储中的实际文件）。"""

    doc_id: str
    filename: str
    doc_type: str
    content_hash: str
    stored_path: str
    uploaded_at: datetime


class PlanStep(BaseModel):
    """执行计划中的单步。"""

    step_id: str
    action: str
    tool: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    result_summary: str | None = None


class RetrievalQuery(BaseModel):
    """单次检索查询及其命中数。"""

    query_id: str
    query_text: str
    rewritten: str | None = None
    route: Literal["bm25", "dense", "article_no", "case_rule", "hybrid"]
    result_count: int = 0


# ---------------------------------------------------------------------------
# CaseState
# ---------------------------------------------------------------------------
class CaseState(BaseModel):
    """Agent 跨节点唯一状态载体。

    必填字段（run_id / thread_id / current_date / user_goal）在前，可选字段带默认值在后。
    """

    # --- 必填：运行标识与用户目标 ---
    run_id: str
    thread_id: str
    current_date: date
    user_goal: str
    # P0 性能：附件按需检索后的紧凑上下文（覆盖语义）
    relevant_attachment_context: str = ""
    user_id: str = "anonymous"
    law_as_of_date: date | None = None

    # --- 案件元信息 ---
    jurisdiction: str | None = None  # 中国大陆/港澳台/涉外
    case_type: str | None = None
    complexity: Literal["light", "deep", "document"] = "light"

    # --- 事实与文档 ---
    facts: list[Fact] = []
    disputed_facts: list[Fact] = []
    timeline: list[TimelineEvent] = []
    missing_facts: list[MissingFact] = []
    uploaded_documents: list[DocumentRef] = []

    # --- 计划与检索 ---
    plan: list[PlanStep] = []
    retrieval_queries: list[RetrievalQuery] = []

    # --- 权威与证据 ---
    statutes: list[Authority] = []
    cases: list[CaseAuthority] = []
    evidence_requirements: list[EvidenceRequirement] = []
    conflicts: list[AuthorityConflict] = []

    # --- 推理与审计 ---
    reasoning_result: ReasoningResult | None = None
    citation_audit: CitationAudit | None = None

    # --- 风险与置信度 ---
    risk_level: Literal["low", "medium", "high"] = "low"
    confidence: Literal["high", "medium", "low", "insufficient"] = "insufficient"

    # --- 迭代与产出 ---
    iteration: int = 0
    final_output: str | None = None


__all__ = [
    "Fact",
    "TimelineEvent",
    "MissingFact",
    "DocumentRef",
    "PlanStep",
    "RetrievalQuery",
    "CaseState",
]
