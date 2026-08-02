"""LegalAnswerV1 数据协议模型测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lvyan.schemas.legal_answer import (
    LegalAnswerV1,
    AnswerMeta,
    ExecutiveSummary,
    FactItem,
    LegalIssue,
    EvidenceItem,
    RiskItem,
    ActionItem,
    LegalCitation,
    UncertaintyItem,
)


def test_legal_answer_minimal_valid():
    """最小合法 LegalAnswerV1 只需 meta + executive_summary + disclaimer。"""
    answer = LegalAnswerV1(
        schema_version="legal_answer_v1",
        meta=AnswerMeta(
            title="测试分析",
            jurisdiction="中国大陆",
            case_type="租赁合同纠纷",
            law_as_of_date="2026-08-02",
            risk_level="medium",
            material_completeness="partial",
        ),
        executive_summary=ExecutiveSummary(
            conclusion="测试结论",
            key_reasons=["理由1"],
            main_uncertainty="不确定点",
        ),
        disclaimer="本内容为法律信息分析，不是法院裁判。",
    )
    assert answer.schema_version == "legal_answer_v1"


def test_fact_item_status_enum():
    """FactItem.status 必须是四态之一。"""
    for status in ("confirmed", "claimed", "inferred", "missing"):
        item = FactItem(fact_id="F1", content="x", status=status)
        assert item.status == status
    with pytest.raises(ValidationError):
        FactItem(fact_id="F1", content="x", status="invalid")


def test_legal_issue_requires_rules_or_facts():
    """LegalIssue 至少应关联规则或事实（验证默认值结构）。"""
    issue = LegalIssue(
        issue_id="I1",
        question="争点",
        conclusion="结论",
        rules=["C1"],
        supporting_facts=["F1"],
        analysis="分析",
    )
    assert issue.counterarguments == []


def test_citation_authority_level_order():
    """LegalCitation.level 必须是固定权威层级。"""
    for level in ("law", "regulation", "judicial_interpretation", "guiding_case", "reference_case", "normative"):
        c = LegalCitation(
            citation_id="C1",
            full_name="法",
            level=level,
            article_number="第一条",
            status="effective",
        )
        assert c.level == level


def test_risk_item_no_numeric_probability_required():
    """RiskItem 不强制要求数字概率（避免虚假精确）。"""
    r = RiskItem(
        dimension="证据充分程度",
        rating="medium",
        detail="缺少交接材料",
    )
    assert r.score is None


def test_action_item_phase_enum():
    """ActionItem.phase 必须是固定时序枚举。"""
    for phase in ("immediate", "short_term", "contingency"):
        a = ActionItem(phase=phase, description="行动", target="目标")
        assert a.phase == phase
    with pytest.raises(ValidationError):
        ActionItem(phase="never", description="x", target="y")


def test_schema_version_immutable_literal():
    """schema_version 只接受 legal_answer_v1。"""
    with pytest.raises(ValidationError):
        LegalAnswerV1(
            schema_version="v2",
            meta=AnswerMeta(
                title="t", jurisdiction="中国大陆", case_type="c",
                law_as_of_date="2026-08-02", risk_level="low",
                material_completeness="complete",
            ),
            executive_summary=ExecutiveSummary(
                conclusion="c", key_reasons=[], main_uncertainty="u"
            ),
            disclaimer="d",
        )
