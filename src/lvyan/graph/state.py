"""LangGraph 状态图 State schema。

定义 ``GraphState``，与 :class:`lvyan.schemas.CaseState` 字段一一对齐，
作为 LangGraph ``StateGraph`` 的状态 schema。

设计要点
--------
- 采用 ``TypedDict`` 而非直接复用 ``CaseState``（Pydantic 模型）：
  TypedDict 在 LangGraph 中序列化开销更低，且可在字段注解上挂载
  ``Annotated[list[X], operator.add]`` 显式声明「追加」reducer 语义。
- 「追加」语义字段（事实 / 时间线 / 检索结果等列表）：节点返回的新增元素会
  被 ``operator.add`` 拼接到既有列表末尾，而非整体覆盖。这保证多轮检索、
  多节点写入的事实不会相互冲掉。
- 「覆盖」语义字段（运行标识 / 元信息 / 推理结果 / 迭代计数等）：节点返回
  的值直接覆盖旧值，符合「最新一次计算为准」的直觉。

字段清单与 :class:`CaseState` 完全一致，便于在节点边界处用
``CaseState.model_validate(dict_state)`` 还原为带校验的 Pydantic 视图。
"""

from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, Literal, TypedDict

from lvyan.schemas.authority import Authority
from lvyan.schemas.case import (
    DocumentRef,
    Fact,
    MissingFact,
    PlanStep,
    RetrievalQuery,
    TimelineEvent,
)
from lvyan.schemas.evidence import AuthorityConflict, CaseAuthority, EvidenceRequirement
from lvyan.schemas.output import CitationAudit, ReasoningResult

__all__ = ["GraphState"]


class GraphState(TypedDict):
    """LangGraph 跨节点流转状态，镜像 :class:`CaseState` 全部字段，并新增
    ``critic_report`` / ``critic_feedback`` 供 Critic 节点使用，
    ``pending_human_approval`` / ``output_iteration`` / ``output_retry_needed``
    供 output_guardrail 节点使用。

    追加语义字段（``Annotated[list[...], operator.add]``）：
        facts, disputed_facts, timeline, missing_facts, uploaded_documents,
        plan, retrieval_queries, statutes, cases, evidence_requirements, conflicts

    覆盖语义字段：
        run_id, thread_id, current_date, user_goal, jurisdiction, case_type,
        complexity, reasoning_result, citation_audit, critic_report,
        critic_feedback, risk_level, confidence, iteration, final_output,
        pending_human_approval, output_iteration, output_retry_needed,
        document_payload
    """

    # --- 必填：运行标识与用户目标（覆盖） ---
    run_id: str
    thread_id: str
    current_date: date
    user_goal: str

    # --- 案件元信息（覆盖） ---
    jurisdiction: str | None
    case_type: str | None
    complexity: Literal["light", "deep", "document"]

    # --- 事实与文档（追加） ---
    facts: Annotated[list[Fact], operator.add]
    disputed_facts: Annotated[list[Fact], operator.add]
    timeline: Annotated[list[TimelineEvent], operator.add]
    missing_facts: Annotated[list[MissingFact], operator.add]
    uploaded_documents: Annotated[list[DocumentRef], operator.add]

    # --- 计划与检索（追加） ---
    plan: Annotated[list[PlanStep], operator.add]
    retrieval_queries: Annotated[list[RetrievalQuery], operator.add]

    # --- 权威与证据（追加） ---
    statutes: Annotated[list[Authority], operator.add]
    cases: Annotated[list[CaseAuthority], operator.add]
    evidence_requirements: Annotated[list[EvidenceRequirement], operator.add]
    conflicts: Annotated[list[AuthorityConflict], operator.add]

    # --- 推理与审计（覆盖） ---
    reasoning_result: ReasoningResult | None
    citation_audit: CitationAudit | None
    critic_report: dict | None  # Critic 评审报告（序列化为 dict）
    critic_feedback: list[str]  # Critic 反馈给 legal_reasoner 的问题清单（覆盖语义）

    # --- 风险与置信度（覆盖） ---
    risk_level: Literal["low", "medium", "high"]
    confidence: Literal["high", "medium", "low", "insufficient"]

    # --- 迭代与产出（覆盖） ---
    iteration: int
    final_output: str | None

    # --- 输出守卫专用（覆盖） ---
    # 不可逆操作待人工审批（Human-in-the-loop），无待审批时为 None
    pending_human_approval: dict | None
    # output_guardrail → composer 回退重试计数（上限 MAX_OUTPUT_ITERATIONS）
    output_iteration: int
    # 是否需要回退 composer 重新生成（由 output_guardrail 设置，路由读取）
    output_retry_needed: bool

    # --- 文书生成专用（覆盖） ---
    # document 模式下 composer 写入的文书载荷（template_name + filled_fields），
    # 供 tools/export.render_docx 渲染 DOCX；非 document 模式为 None
    document_payload: dict | None
