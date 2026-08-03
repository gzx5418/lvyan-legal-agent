"""LangGraph 状态图 State schema。

定义 ``GraphState``，与 :class:`lvyan.schemas.CaseState` 字段一一对齐，
作为 LangGraph ``StateGraph`` 的状态 schema。

设计要点（P0-4 修复后）
-----------------------
- 采用 ``TypedDict`` 而非直接复用 ``CaseState``（Pydantic 模型）：
  TypedDict 在 LangGraph 中序列化开销更低，且可在字段注解上挂载
  自定义 reducer 显式声明合并语义。
- 「键控合并」语义字段（plan / retrieval_queries / statutes / cases /
  evidence_requirements / conflicts / missing_facts）：
  节点返回的完整列表与旧列表按唯一键去重合并，避免 ``operator.add`` 与
  「节点返回完整结果」冲突导致状态不断复制。reducer 保留分数更高/更新版本。
- 「追加」语义字段（facts / disputed_facts / timeline / uploaded_documents）：
  节点返回的增量元素被拼接到既有列表末尾。这些字段的节点产出本身就是增量。
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


# ---------------------------------------------------------------------------
# 键控合并 reducer：节点返回完整列表时，按唯一键与新旧合并，避免状态翻倍
# ---------------------------------------------------------------------------
def _get_attr(item: object, name: str, default: object = None) -> object:
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def merge_plan(old: list[PlanStep], new: list[PlanStep]) -> list[PlanStep]:
    """PlanStep 按 step_id 去重，新值覆盖旧值（status/result_summary 更新）。"""
    merged: dict[object, PlanStep] = {}
    order: list[object] = []
    for step in [*old, *new]:
        key = _get_attr(step, "step_id", None) or id(step)
        if key not in merged:
            order.append(key)
        merged[key] = step
    return [merged[k] for k in order]


def merge_retrieval_queries(
    old: list[RetrievalQuery], new: list[RetrievalQuery]
) -> list[RetrievalQuery]:
    """RetrievalQuery 按 query_id 去重，新值覆盖旧值。"""
    merged: dict[object, RetrievalQuery] = {}
    order: list[object] = []
    for q in [*old, *new]:
        key = _get_attr(q, "query_id", None) or _get_attr(q, "query_text", None) or id(q)
        if key not in merged:
            order.append(key)
        merged[key] = q
    return [merged[k] for k in order]


def _authority_score(item: object) -> float:
    """取 Authority 三路分数的最大值（缺失视为 0）。"""
    scores = (
        float(_get_attr(item, "rerank_score", 0.0) or 0.0),
        float(_get_attr(item, "dense_score", 0.0) or 0.0),
        float(_get_attr(item, "lexical_score", 0.0) or 0.0),
    )
    return max(scores)


def merge_authorities(old: list[Authority], new: list[Authority]) -> list[Authority]:
    """Authority 按 (source_id, article_number) 去重，保留分数更高者。

    authority_resolver / retrieve_statutes 返回完整列表时，
    新值与旧值按键合并；同键保留分数更高者，避免重复检索导致状态膨胀。
    """
    merged: dict[tuple[object, object], Authority] = {}
    order: list[tuple[object, object]] = []
    for item in [*old, *new]:
        source_id = str(_get_attr(item, "source_id", "") or "")
        article_number = _get_attr(item, "article_number", None)
        article_key = article_number if article_number is not None else ""
        key = (source_id, article_key)
        if key not in merged:
            order.append(key)
            merged[key] = item
            continue
        existing = merged[key]
        if _authority_score(item) > _authority_score(existing):
            merged[key] = item
    return [merged[k] for k in order]


def merge_cases(old: list[CaseAuthority], new: list[CaseAuthority]) -> list[CaseAuthority]:
    """CaseAuthority 按 case_id 去重，保留 similarity_score 更高者。"""
    merged: dict[object, CaseAuthority] = {}
    order: list[object] = []
    for item in [*old, *new]:
        key = _get_attr(item, "case_id", None) or id(item)
        if key not in merged:
            order.append(key)
            merged[key] = item
            continue
        existing = merged[key]
        new_score = float(_get_attr(item, "similarity_score", 0.0) or 0.0)
        old_score = float(_get_attr(existing, "similarity_score", 0.0) or 0.0)
        if new_score > old_score:
            merged[key] = item
    return [merged[k] for k in order]


def merge_evidence_requirements(
    old: list[EvidenceRequirement], new: list[EvidenceRequirement]
) -> list[EvidenceRequirement]:
    """EvidenceRequirement 按 requirement_id 去重，新值覆盖旧值。"""
    merged: dict[object, EvidenceRequirement] = {}
    order: list[object] = []
    for item in [*old, *new]:
        key = _get_attr(item, "requirement_id", None) or id(item)
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[k] for k in order]


def merge_conflicts(
    old: list[AuthorityConflict], new: list[AuthorityConflict]
) -> list[AuthorityConflict]:
    """AuthorityConflict 按 conflict_id 去重，新值覆盖旧值。"""
    merged: dict[object, AuthorityConflict] = {}
    order: list[object] = []
    for item in [*old, *new]:
        key = _get_attr(item, "conflict_id", None) or id(item)
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[k] for k in order]


def merge_missing_facts(
    old: list[MissingFact], new: list[MissingFact]
) -> list[MissingFact]:
    """MissingFact 按 fact_key 去重，新值覆盖旧值。"""
    merged: dict[object, MissingFact] = {}
    order: list[object] = []
    for item in [*old, *new]:
        key = _get_attr(item, "fact_key", None) or id(item)
        if key not in merged:
            order.append(key)
        merged[key] = item
    return [merged[k] for k in order]


class GraphState(TypedDict):
    """LangGraph 跨节点流转状态，镜像 :class:`CaseState` 全部字段，并新增
    ``critic_report`` / ``critic_feedback`` 供 Critic 节点使用，
    ``pending_human_approval`` / ``output_iteration`` / ``output_retry_needed``
    供 output_guardrail 节点使用。

    合并语义字段：
      - 键控合并（去重，新值覆盖旧值）：plan / retrieval_queries / statutes /
        cases / evidence_requirements / conflicts / missing_facts
      - 追加（operator.add，节点返回增量）：facts / disputed_facts / timeline /
        uploaded_documents

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
    # P0 性能：附件按需检索后的紧凑上下文，替代「全文塞进 user_goal」。
    # 由 attachment_retriever 节点写入（覆盖语义）；LLM 节点优先读取它。
    relevant_attachment_context: str

    # --- 案件元信息（覆盖） ---
    jurisdiction: str | None
    case_type: str | None
    complexity: Literal["light", "deep", "document"]

    # --- 事实与文档（追加；节点返回增量） ---
    facts: Annotated[list[Fact], operator.add]
    disputed_facts: Annotated[list[Fact], operator.add]
    timeline: Annotated[list[TimelineEvent], operator.add]
    uploaded_documents: Annotated[list[DocumentRef], operator.add]

    # --- 计划与检索（键控合并；节点可返回完整列表） ---
    plan: Annotated[list[PlanStep], merge_plan]
    retrieval_queries: Annotated[list[RetrievalQuery], merge_retrieval_queries]

    # --- 权威与证据（键控合并；节点可返回完整列表） ---
    statutes: Annotated[list[Authority], merge_authorities]
    cases: Annotated[list[CaseAuthority], merge_cases]
    evidence_requirements: Annotated[list[EvidenceRequirement], merge_evidence_requirements]
    conflicts: Annotated[list[AuthorityConflict], merge_conflicts]
    missing_facts: Annotated[list[MissingFact], merge_missing_facts]

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
    # LegalAnswerV1 结构化输出（与 final_output 并行，供前端组件化渲染）
    legal_answer: dict | None

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
