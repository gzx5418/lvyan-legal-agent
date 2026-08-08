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
    "本内容为基于用户陈述和已上传材料生成的法律信息分析，不是法院裁判、律师正式法律意见或结果保证。"
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
            "insufficient": ("medium", "信息不足，暂无法判断趋势"),
        }
        rating, detail = tend_map.get(tend, ("medium", "趋势未知"))
        risks.append(RiskItem(dimension="司法裁判趋势", rating=rating, detail=detail))  # type: ignore[arg-type]
    return risks


def _build_action_plan(state: CaseState) -> list[ActionItem]:
    if state.case_type == "工伤认定":
        return [
            ActionItem(
                phase="immediate",
                description="尽快取得道路交通事故责任认定书，并保存现场照片、视频、报警记录、诊断证明和病历原件",
                target="固定事故责任、伤害事实和医疗证据",
                required_materials=["道路交通事故责任认定书", "诊断证明/病历", "现场或报警记录"],
                risk="事故责任认定是判断“非本人主要责任”的关键证据。",
            ),
            ActionItem(
                phase="short_term",
                description="书面告知用人单位并要求其在事故伤害发生之日起30日内申请工伤认定；同时整理个人申请材料",
                target="及时启动工伤认定程序",
                required_materials=["劳动关系证据", "医疗诊断证明", "事故责任认定书"],
                deadline="事故伤害发生之日起30日内（用人单位申请）",
            ),
            ActionItem(
                phase="contingency",
                description="用人单位未申请的，劳动者、近亲属或工会组织可在事故伤害发生之日起1年内向统筹地区社会保险行政部门申请；对不予认定决定依法寻求救济",
                target="保全工伤认定申请期限与救济权利",
                deadline="事故伤害发生之日起1年内（个人、近亲属或工会组织申请）",
            ),
        ]

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
    guidance = {
        "劳动争议": (
            "向用人单位发送书面请求，明确工资、解除理由或补偿事项，并保留送达记录",
            "在仲裁时效内向有管辖权的劳动人事争议仲裁委员会申请仲裁",
            "劳动争议申请仲裁的时效通常为1年，具体起算时间需结合请求类型判断",
        ),
        "合同纠纷": (
            "向合同相对方发送书面催告，列明具体请求、合同依据、金额和合理履行期限",
            "逾期未履行的，按争议解决条款选择调解、仲裁或向有管辖权的法院起诉",
            "普通合同请求权的诉讼时效通常为3年，需结合履行期限和知悉损害时间核算",
        ),
        "侵权纠纷": (
            "向责任方提出书面赔偿请求，列明损害项目并附证据和费用凭证",
            "协商不成时，根据证据情况申请鉴定、调解或向有管辖权的法院起诉",
            "侵权损害赔偿请求通常适用3年诉讼时效，身体伤害等证据应尽早固定",
        ),
        "婚姻家庭": (
            "在确保人身安全的前提下，提出子女、财产和债务处理的书面方案",
            "无法达成协议的，整理身份、婚姻、子女和财产材料后依法申请调解或起诉",
            "财产和损害赔偿等具体请求可能有期限，不宜因持续协商延误",
        ),
        "知识产权": (
            "完成权属和侵权证据保全后，向对方或平台发送明确的停止侵害通知",
            "根据侵权规模选择平台投诉、行政处理或民事诉讼，并主张合理维权费用",
            "民事请求通常适用3年诉讼时效，网络证据可能灭失，应优先保全",
        ),
    }
    short_term, contingency, deadline = guidance.get(
        state.case_type,
        (
            "向对方发送可留痕的书面通知，明确事实、请求和合理处理期限",
            "对方逾期未处理的，根据法律关系选择调解、仲裁、行政程序或诉讼",
            "具体期限需依据请求权性质和事实发生时间单独核算",
        ),
    )
    plan.append(
        ActionItem(
            phase="short_term",
            description=short_term,
            target="尝试协商解决",
            deadline=deadline,
        )
    )
    plan.append(
        ActionItem(
            phase="contingency",
            description=contingency,
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
    uncertainty = state.missing_facts[0].question if state.missing_facts else "暂无显著不确定点"
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
