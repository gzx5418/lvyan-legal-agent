"""LegalAnswerV1 构建器：从 CaseState 构建结构化法律输出。

将 composer 已有的结构化中间态（facts / statutes / cases / reasoning_result /
evidence_requirements / missing_facts）映射为 LegalAnswerV1，使最终输出具备
固定字段与关联关系，便于前端组件化渲染与 DOCX/PDF 导出。
"""
from __future__ import annotations

from typing import Any

from lvyan.schemas.authority import Authority
from lvyan.schemas.case import CaseState, Fact
from lvyan.schemas.evidence import EvidenceRequirement
from lvyan.schemas.legal_answer import (
    ActionItem,
    AnswerMeta,
    EvidenceItem,
    ExecutiveSummary,
    FactItem,
    LegalAnswerV1,
    LegalCitation,
    LegalIssue,
    RiskItem,
    UncertaintyItem,
)
from lvyan.schemas.output import ReasoningResult

_DISCLAIMER = (
    "本内容为基于用户陈述和已上传材料生成的法律信息分析，"
    "不是法院裁判、律师正式法律意见或结果保证。"
)

_AUTHORITY_LEVEL_MAP: dict[str, str] = {
    "宪法": "law",
    "法律": "law",
    "行政法规": "regulation",
    "司法解释": "judicial_interpretation",
    "监察法规": "regulation",
    "地方性法规": "regulation",
    "部门规章": "normative",
}

_FACT_STATUS_MAP: dict[str, str] = {
    "document": "confirmed",
    "extracted": "confirmed",
    "user": "claimed",
    "llm": "inferred",
}


def _fmt_date(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _classify_fact(fact: Fact) -> FactItem:
    status = _FACT_STATUS_MAP.get(fact.source, "claimed")
    return FactItem(
        fact_id=fact.fact_id,
        content=fact.content,
        status=status,  # type: ignore[arg-type]
        source_ref=fact.source_ref,
        detail=fact.category,
    )


def _authority_to_citation(auth: Authority) -> LegalCitation:
    level = _AUTHORITY_LEVEL_MAP.get(auth.authority_level, "normative")
    return LegalCitation(
        citation_id=auth.source_id,
        full_name=auth.title,
        article_number=auth.article_number or "",
        article_text=auth.article_text,
        level=level,  # type: ignore[arg-type]
        status=auth.status,
        effective_date=_fmt_date(auth.effective_date),
        official_source=auth.official_source,
    )


def _build_facts(state: CaseState) -> list[FactItem]:
    items = [_classify_fact(f) for f in state.facts]
    for mf in state.missing_facts:
        items.append(
            FactItem(
                fact_id=mf.fact_key,
                content=mf.question,
                status="missing",
                detail=mf.reason,
            )
        )
    return items


def _build_issues(reasoning: ReasoningResult | None) -> list[LegalIssue]:
    if reasoning is None:
        return []
    issues: list[LegalIssue] = []
    for idx, focus in enumerate(reasoning.disputed_focus):
        issues.append(
            LegalIssue(
                issue_id=f"I{idx + 1}",
                question=focus,
                conclusion=reasoning.legal_relationship or "",
                rules=[],
                supporting_facts=[],
                analysis="",
                counterarguments=reasoning.defendant_arguments if idx == 0 else [],
            )
        )
    return issues


def _build_evidence(reqs: list[EvidenceRequirement]) -> list[EvidenceItem]:
    status_map = {"met": "provided", "partial": "partial", "missing": "missing"}
    force_map = {"met": "strong", "partial": "medium", "missing": "key"}
    items: list[EvidenceItem] = []
    for req in reqs:
        items.append(
            EvidenceItem(
                evidence_id=req.requirement_id,
                name=req.fact_to_prove,
                purpose=req.fact_to_prove,
                status=status_map.get(req.current_status, "missing"),  # type: ignore[arg-type]
                probative_force=force_map.get(req.current_status, "medium"),  # type: ignore[arg-type]
                next_step=req.gap_description,
            )
        )
    return items


def _build_risks(state: CaseState) -> list[RiskItem]:
    risks: list[RiskItem] = []
    rating_map = {"low": "low", "medium": "medium", "high": "high"}
    risks.append(
        RiskItem(
            dimension="综合风险",
            rating=rating_map.get(state.risk_level, "medium"),  # type: ignore[arg-type]
            detail=f"风险等级：{state.risk_level}",
        )
    )
    conf_detail = {
        "high": "证据较充分",
        "medium": "证据部分充分",
        "low": "证据不足",
        "insufficient": "证据严重不足，难以得出确定结论",
    }
    conf_rating = (
        "high"
        if state.confidence == "high"
        else ("medium" if state.confidence == "medium" else "low")
    )
    risks.append(
        RiskItem(
            dimension="证据置信度",
            rating=conf_rating,  # type: ignore[arg-type]
            detail=conf_detail.get(state.confidence, "未知"),
        )
    )
    if state.reasoning_result is not None:
        tend = state.reasoning_result.judicial_tendency
        tend_map = {
            "favorable": ("low", "整体趋势有利"),
            "somewhat_favorable": ("low", "整体趋势较有利"),
            "even": ("medium", "双方势均力敌"),
            "somewhat_unfavorable": ("high", "存在不利趋势"),
            "insufficient": ("high", "信息不足以判断趋势"),
        }
        rating, detail = tend_map.get(tend, ("medium", "趋势未知"))
        risks.append(RiskItem(dimension="司法裁判趋势", rating=rating, detail=detail))  # type: ignore[arg-type]
    return risks


def _build_action_plan(state: CaseState) -> list[ActionItem]:
    plan: list[ActionItem] = []
    for mf in state.missing_facts:
        if mf.is_blocking:
            plan.append(
                ActionItem(
                    phase="immediate",
                    description=f"补充材料：{mf.question}",
                    target="补齐关键证据缺口",
                    required_materials=[mf.question],
                    risk=mf.reason,
                )
            )
    if not plan:
        plan.append(
            ActionItem(
                phase="immediate",
                description="整理现有证据材料并妥善保存原始文件",
                target="固定证据",
            )
        )
    plan.append(
        ActionItem(
            phase="short_term",
            description="发送书面催告，要求对方说明具体扣款项目与依据",
            target="尝试协商解决",
        )
    )
    plan.append(
        ActionItem(
            phase="contingency",
            description="协商不成时，整理证据目录与起诉材料，选择适当程序",
            target="依法维权",
        )
    )
    return plan


def _build_summary(state: CaseState) -> ExecutiveSummary:
    reasoning = state.reasoning_result
    if reasoning is not None:
        conclusion = reasoning.legal_relationship or "基于现有材料的初步分析"
        key_reasons = reasoning.key_factors[:3] if reasoning.key_factors else []
    else:
        conclusion = f"关于「{state.user_goal}」的初步分析"
        key_reasons = []
    uncertainty = (
        state.missing_facts[0].question if state.missing_facts else "暂无显著不确定点"
    )
    return ExecutiveSummary(
        conclusion=conclusion,
        key_reasons=key_reasons,
        main_uncertainty=uncertainty,
    )


def build_legal_answer(state: CaseState) -> LegalAnswerV1:
    """从 CaseState 构建 LegalAnswerV1 结构化输出。"""
    meta = AnswerMeta(
        title=state.user_goal or "法律分析意见",
        jurisdiction=state.jurisdiction or "中国大陆",
        case_type=state.case_type or "未分类",
        law_as_of_date=_fmt_date(state.law_as_of_date or state.current_date),
        risk_level=state.risk_level,
        material_completeness=(
            "complete"
            if not state.missing_facts
            else ("partial" if len(state.missing_facts) <= 2 else "insufficient")
        ),
        analysis_mode=state.complexity,
    )
    return LegalAnswerV1(
        meta=meta,
        executive_summary=_build_summary(state),
        facts=_build_facts(state),
        issues=_build_issues(state.reasoning_result),
        evidence=_build_evidence(state.evidence_requirements),
        risks=_build_risks(state),
        action_plan=_build_action_plan(state),
        citations=[_authority_to_citation(a) for a in state.statutes],
        uncertainties=[
            UncertaintyItem(
                description=mf.question,
                impact=mf.reason,
                resolution="补充相应材料后可进一步判断" if mf.is_blocking else None,
            )
            for mf in state.missing_facts
        ],
        disclaimer=_DISCLAIMER,
    )


__all__ = ["build_legal_answer"]
