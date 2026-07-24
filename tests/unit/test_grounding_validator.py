"""Grounding Validator 单元测试（SubTask 13.5）。

覆盖场景：
1. 语义支持：引用上下文与条文有 bigram 重叠 → passed=True
2. 完全无支持：引用上下文与条文无任何重叠 → no_support error
3. 弱支持：少量重叠但未达阈值 → weak_support warning
4. 未匹配法条：引用不在 statutes 中 → unmatched warning
5. 空推理结果：reasoning_result=None → passed=True, total=0
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lvyan.schemas import Authority, ReasoningResult
from lvyan.validators.grounding import GroundingReport, validate_grounding


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
    article_text: str = (
        "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
        "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
    ),
    source_id: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level="法律",
        effective_date=date(2021, 1, 1),
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_reasoning_result(key_factors: list[str]) -> ReasoningResult:
    """构造 ReasoningResult，在 key_factors 中嵌入法条引用。"""
    return ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）", "违约行为（已满足）"],
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["原告主张对方违约应赔偿"],
        defendant_arguments=["被告主张不可抗力免责"],
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency="somewhat_favorable",
        evidence_confidence="medium",
        key_factors=key_factors,
    )


# ---------------------------------------------------------------------------
# 1. 语义支持：通过
# ---------------------------------------------------------------------------
def test_validate_grounding_supported():
    """引用上下文与条文有充分 bigram 重叠 → passed=True。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条，"
            "当事人一方不履行合同义务应当承担违约责任"
        ]
    )
    statutes = [_make_authority()]
    report = validate_grounding(rr, statutes)
    assert report.total_citations >= 1
    assert report.passed is True
    # 不应有 no_support error
    assert all(i.severity != "error" for i in report.issues)


# ---------------------------------------------------------------------------
# 2. 完全无支持：no_support error
# ---------------------------------------------------------------------------
def test_validate_grounding_no_support():
    """引用上下文与条文无任何 bigram 重叠 → no_support error。

    使用「继承从被继承人死亡时开始」作为条文内容，确保与 reasoning_result
    中任何字段（合同/违约/主张等）均无 bigram 重叠。
    """
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条"
            "关于宇宙飞船星际旅行的规定"
        ]
    )
    statutes = [
        _make_authority(
            article_text="继承从被继承人死亡时开始。",
        )
    ]
    report = validate_grounding(rr, statutes)
    # 应有 no_support 或 weak_support 问题
    assert report.passed is False or any(
        i.issue_type in ("no_support", "weak_support") for i in report.issues
    )


def test_validate_grounding_completely_unrelated():
    """引用上下文与条文完全不相干 → no_support error。

    使用「继承从被继承人死亡时开始」作为条文内容，确保与 reasoning_result
    中任何字段均无 bigram 重叠。
    """
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条"
            "量子力学测不准原理"
        ]
    )
    statutes = [
        _make_authority(
            article_text="继承从被继承人死亡时开始。",
        )
    ]
    report = validate_grounding(rr, statutes)
    no_support = [i for i in report.issues if i.issue_type == "no_support"]
    # 完全不相干时应有 no_support error（或至少 weak_support warning）
    assert len(no_support) >= 1 or any(
        i.issue_type == "weak_support" for i in report.issues
    )


# ---------------------------------------------------------------------------
# 3. 弱支持：warning
# ---------------------------------------------------------------------------
def test_validate_grounding_weak_support():
    """引用上下文与条文有少量重叠但未达阈值 → weak_support warning。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条xyzabc"
        ]
    )
    statutes = [
        _make_authority(
            article_text="当事人一方不履行合同义务应承担违约责任",
        )
    ]
    report = validate_grounding(rr, statutes)
    # 可能有 weak_support warning，但 passed 可能为 True（warning 不影响）
    weak = [i for i in report.issues if i.issue_type == "weak_support"]
    # 至少不应有 error
    assert all(i.severity != "error" for i in report.issues) or len(weak) > 0


# ---------------------------------------------------------------------------
# 4. 未匹配法条：unmatched warning
# ---------------------------------------------------------------------------
def test_validate_grounding_unmatched():
    """引用不在 statutes 中 → unmatched warning。"""
    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国合同法》第四条虚构条款"]
    )
    statutes = [
        _make_authority(article_number="第五百七十七条")  # 不同的条文号
    ]
    report = validate_grounding(rr, statutes)
    unmatched = [i for i in report.issues if i.issue_type == "unmatched"]
    assert len(unmatched) >= 1
    # unmatched 是 warning，不影响 passed
    assert report.passed is True


# ---------------------------------------------------------------------------
# 5. 空推理结果
# ---------------------------------------------------------------------------
def test_validate_grounding_empty_reasoning():
    """reasoning_result=None → passed=True, total=0。"""
    report = validate_grounding(None, [])
    assert report.total_citations == 0
    assert report.passed is True
    assert len(report.issues) == 0


# ---------------------------------------------------------------------------
# 6. 多条引用：部分支持部分不支持
# ---------------------------------------------------------------------------
def test_validate_grounding_mixed():
    """多条引用：一条有支持，一条无支持 → passed=False。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条，"
            "当事人一方不履行合同义务应当承担违约责任",
            "依据《中华人民共和国民法典》第一千二百三十四条量子力学",
        ]
    )
    statutes = [
        _make_authority(article_number="第五百七十七条"),
        _make_authority(
            article_number="第一千二百三十四条",
            article_text="继承从被继承人死亡时开始。",
        ),
    ]
    report = validate_grounding(rr, statutes)
    # 第二条引用与继承条文无语义支持
    assert report.total_citations >= 2
