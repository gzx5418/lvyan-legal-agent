"""LegalAnswerV1 校验器测试。"""

from __future__ import annotations

import pytest

from lvyan.schemas.legal_answer import (
    AnswerMeta,
    ExecutiveSummary,
    FactItem,
    LegalAnswerV1,
    LegalCitation,
    LegalIssue,
)
from lvyan.nodes.answer_validator import ValidationError as AVError, validate_legal_answer


def _make_answer(**overrides) -> LegalAnswerV1:
    base = dict(
        schema_version="legal_answer_v1",
        meta=AnswerMeta(
            title="t",
            jurisdiction="中国大陆",
            case_type="c",
            law_as_of_date="2026-08-02",
            risk_level="low",
            material_completeness="complete",
        ),
        executive_summary=ExecutiveSummary(conclusion="c", key_reasons=["r"], main_uncertainty="u"),
        disclaimer="本内容为法律信息分析，不是法院裁判。",
    )
    base.update(overrides)
    return LegalAnswerV1(**base)


def test_valid_answer_passes():
    answer = _make_answer()
    validate_legal_answer(answer)


def test_issue_referencing_nonexistent_fact_fails():
    answer = _make_answer(
        issues=[
            LegalIssue(
                issue_id="I1",
                question="q",
                conclusion="c",
                supporting_facts=["F999"],
            )
        ],
    )
    with pytest.raises(AVError, match="F999"):
        validate_legal_answer(answer)


def test_issue_referencing_nonexistent_citation_fails():
    answer = _make_answer(
        issues=[
            LegalIssue(
                issue_id="I1",
                question="q",
                conclusion="c",
                rules=["C999"],
            )
        ],
    )
    with pytest.raises(AVError, match="C999"):
        validate_legal_answer(answer)


def test_citation_missing_article_number_fails():
    answer = _make_answer(
        citations=[
            LegalCitation(
                citation_id="C1",
                full_name="某法",
                article_number="",
                level="law",
                status="effective",
            ),
        ],
    )
    with pytest.raises(AVError, match="条款序号"):
        validate_legal_answer(answer)


def test_guaranteed_win_language_fails():
    answer = _make_answer(
        executive_summary=ExecutiveSummary(
            conclusion="保证胜诉", key_reasons=[], main_uncertainty="u"
        ),
    )
    with pytest.raises(AVError, match="不当承诺"):
        validate_legal_answer(answer)


def test_repealed_citation_without_warning_fails():
    answer = _make_answer(
        citations=[
            LegalCitation(
                citation_id="C1",
                full_name="旧法",
                article_number="第一条",
                level="law",
                status="repealed",
            ),
        ],
    )
    with pytest.raises(AVError, match="已失效"):
        validate_legal_answer(answer)


def test_inferred_fact_without_uncertainty_passes():
    """存在 inferred 事实但无 uncertainties → 允许通过（仅警告级）。"""
    answer = _make_answer(
        facts=[FactItem(fact_id="F1", content="推断", status="inferred")],
        uncertainties=[],
    )
    validate_legal_answer(answer)
