"""Legal Reasoner 节点单元测试。

覆盖 Task 12 SubTask 12.1 / 12.2 验证标准：
1. 劳动争议场景（解除劳动合同经济补偿）：ReasoningResult 各字段非空、
   裁判倾向在合法枚举内、无数字概率。
2. 侵权场景：同上。
3. _assert_no_numeric_probability：含数字概率的非法 ReasoningResult 应被拦截。
4. _assert_no_numeric_probability：合法 ReasoningResult 应通过自检。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lvyan.nodes.legal_reasoner import (
    _assert_no_numeric_probability,
    legal_reasoner,
)
from lvyan.schemas import (
    Authority,
    CaseAuthority,
    CaseState,
    EvidenceRequirement,
    Fact,
    ReasoningResult,
)


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
    title: str,
    article_number: str,
    article_text: str,
    effective_date: date | None = None,
    authority_level: str = "法律",
) -> Authority:
    return Authority(
        source_id=f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level=authority_level,
        effective_date=effective_date,
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. 劳动争议场景（解除劳动合同经济补偿）
# ---------------------------------------------------------------------------
def _labor_state() -> CaseState:
    """构造劳动争议场景：用人单位违法解除劳动合同，劳动者主张经济补偿。"""
    return CaseState(
        run_id="run-labor-001",
        thread_id="thread-labor-001",
        current_date=date(2026, 7, 23),
        user_goal="公司辞退我，工作3年，月工资8000元，要求经济补偿",
        jurisdiction="中国大陆",
        case_type="劳动争议",
        complexity="deep",
        facts=[
            Fact(fact_id="f1", category="当事人", content="劳动者与用人单位存在劳动关系", source="user", confidence=0.9),
            Fact(fact_id="f2", category="行为", content="用人单位单方解除劳动合同", source="user", confidence=0.85),
            Fact(fact_id="f3", category="时间", content="工作年限3年", source="user", confidence=0.8),
            Fact(fact_id="f4", category="金额", content="月工资8000元", source="user", confidence=0.85),
            Fact(fact_id="f5", category="证据", content="劳动合同", source="user", confidence=0.9),
            Fact(fact_id="f6", category="证据", content="工资条", source="user", confidence=0.8),
        ],
        statutes=[
            _make_authority(
                title="中华人民共和国劳动合同法",
                article_number="第四十七条",
                article_text="经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资的标准向劳动者支付。",
                effective_date=date(2012, 12, 28),
            ),
            _make_authority(
                title="中华人民共和国劳动合同法",
                article_number="第四十六条",
                article_text="有下列情形之一的，用人单位应当向劳动者支付经济补偿。",
                effective_date=date(2012, 12, 28),
            ),
        ],
        cases=[
            CaseAuthority(
                case_id="case-labor-001",
                case_number="(2023)京01民终456号",
                court="北京市第一中级人民法院",
                case_type="劳动争议",
                brief_facts="用人单位违法解除劳动合同",
                ruling_summary="支持劳动者经济补偿请求",
                ruling_date=date(2023, 5, 10),
                similarity_score=0.85,
            ),
        ],
        evidence_requirements=[
            EvidenceRequirement(
                requirement_id="er1",
                fact_to_prove="劳动关系存在",
                evidence_types=["劳动合同", "考勤记录"],
                current_status="met",
                gap_description=None,
            ),
            EvidenceRequirement(
                requirement_id="er2",
                fact_to_prove="解除事实",
                evidence_types=["解除通知"],
                current_status="met",
                gap_description=None,
            ),
            EvidenceRequirement(
                requirement_id="er3",
                fact_to_prove="工资数额",
                evidence_types=["工资条", "银行流水"],
                current_status="met",
                gap_description=None,
            ),
        ],
    )


def test_legal_reasoner_labor_dispute_fields_non_empty():
    """劳动争议场景：ReasoningResult 各字段非空。"""
    state = _labor_state()
    result = legal_reasoner(state)

    assert "reasoning_result" in result
    rr = result["reasoning_result"]
    assert isinstance(rr, ReasoningResult)

    # 各字段非空
    assert rr.legal_relationship is not None and len(rr.legal_relationship) > 0
    assert len(rr.elements) > 0, "构成要件不应为空"
    assert len(rr.disputed_focus) > 0, "争议焦点不应为空"
    assert len(rr.plaintiff_arguments) > 0, "原告主张不应为空"
    assert len(rr.defendant_arguments) > 0, "被告主张不应为空"
    assert len(rr.evidence_mapping) > 0, "证据对应不应为空"
    assert len(rr.key_factors) > 0, "关键影响因素不应为空"

    # 构成要件应包含"已满足"或"未满足"标注
    for e in rr.elements:
        assert "已满足" in e or "未满足" in e, f"构成要件缺少满足状态标注：{e}"


def test_legal_reasoner_labor_dispute_tendency_in_legal_enum():
    """劳动争议场景：裁判倾向在合法枚举内。"""
    state = _labor_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]

    legal_tendencies = {
        "favorable",
        "somewhat_favorable",
        "even",
        "somewhat_unfavorable",
        "insufficient",
    }
    assert rr.judicial_tendency in legal_tendencies, (
        f"裁判倾向 {rr.judicial_tendency} 不在合法枚举内"
    )

    legal_confidence = {"high", "medium", "low"}
    assert rr.evidence_confidence in legal_confidence, (
        f"证据置信度 {rr.evidence_confidence} 不在合法枚举内"
    )


def test_legal_reasoner_labor_dispute_no_numeric_probability():
    """劳动争议场景：序列化结果中无数字概率/百分比。"""
    state = _labor_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]
    payload = rr.model_dump_json()

    # 不应包含百分比
    assert "%" not in payload, f"结果含百分比：{payload}"
    assert "％" not in payload, f"结果含全角百分号：{payload}"
    # 不应包含概率关键词
    for kw in ("胜诉率", "胜诉概率", "胜率", "概率"):
        assert kw not in payload, f"结果含概率关键词「{kw}」：{payload}"


def test_legal_reasoner_labor_dispute_confidence_aligned():
    """劳动争议场景：confidence 字段与 evidence_confidence 对齐。"""
    state = _labor_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]
    confidence = result.get("confidence")

    # statutes 非空时，confidence 应等于 evidence_confidence
    assert confidence == rr.evidence_confidence, (
        f"confidence({confidence}) 应与 evidence_confidence({rr.evidence_confidence}) 一致"
    )


# ---------------------------------------------------------------------------
# 2. 侵权场景
# ---------------------------------------------------------------------------
def _tort_state() -> CaseState:
    """构造侵权场景：交通事故致人损害。"""
    return CaseState(
        run_id="run-tort-001",
        thread_id="thread-tort-001",
        current_date=date(2026, 7, 23),
        user_goal="对方驾车撞伤我，导致医疗费5万元，要求赔偿",
        jurisdiction="中国大陆",
        case_type="侵权纠纷",
        complexity="deep",
        facts=[
            Fact(fact_id="f1", category="当事人", content="原告被撞伤", source="user", confidence=0.9),
            Fact(fact_id="f2", category="行为", content="被告驾车碰撞原告", source="user", confidence=0.85),
            Fact(fact_id="f3", category="金额", content="医疗费5万元", source="user", confidence=0.8),
            Fact(fact_id="f4", category="证据", content="医院诊断证明", source="user", confidence=0.85),
        ],
        statutes=[
            _make_authority(
                title="中华人民共和国民法典",
                article_number="第一千一百七十九条",
                article_text="侵害他人造成人身损害的，应当赔偿医疗费、护理费、交通费等为治疗和康复支出的合理费用。",
                effective_date=date(2021, 1, 1),
            ),
        ],
        cases=[],
        evidence_requirements=[
            EvidenceRequirement(
                requirement_id="er1",
                fact_to_prove="侵权行为",
                evidence_types=["事故认定书"],
                current_status="met",
                gap_description=None,
            ),
            EvidenceRequirement(
                requirement_id="er2",
                fact_to_prove="损害后果",
                evidence_types=["医疗费票据", "诊断证明"],
                current_status="met",
                gap_description=None,
            ),
            EvidenceRequirement(
                requirement_id="er3",
                fact_to_prove="因果关系",
                evidence_types=["鉴定意见"],
                current_status="missing",
                gap_description="缺失因果关系鉴定意见",
            ),
        ],
    )


def test_legal_reasoner_tort_dispute_fields_non_empty():
    """侵权场景：ReasoningResult 各字段非空。"""
    state = _tort_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]
    assert isinstance(rr, ReasoningResult)

    assert rr.legal_relationship is not None and len(rr.legal_relationship) > 0
    assert len(rr.elements) > 0, "构成要件不应为空"
    assert len(rr.disputed_focus) > 0, "争议焦点不应为空"
    assert len(rr.plaintiff_arguments) > 0, "原告主张不应为空"
    assert len(rr.defendant_arguments) > 0, "被告主张不应为空"
    assert len(rr.evidence_mapping) > 0, "证据对应不应为空"
    assert len(rr.key_factors) > 0, "关键影响因素不应为空"


def test_legal_reasoner_tort_dispute_tendency_in_legal_enum():
    """侵权场景：裁判倾向在合法枚举内。"""
    state = _tort_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]

    legal_tendencies = {
        "favorable",
        "somewhat_favorable",
        "even",
        "somewhat_unfavorable",
        "insufficient",
    }
    assert rr.judicial_tendency in legal_tendencies
    assert rr.evidence_confidence in {"high", "medium", "low"}


def test_legal_reasoner_tort_dispute_no_numeric_probability():
    """侵权场景：序列化结果中无数字概率/百分比。"""
    state = _tort_state()
    result = legal_reasoner(state)
    rr = result["reasoning_result"]
    payload = rr.model_dump_json()

    assert "%" not in payload
    assert "％" not in payload
    for kw in ("胜诉率", "胜诉概率", "胜率", "概率"):
        assert kw not in payload


# ---------------------------------------------------------------------------
# 3. _assert_no_numeric_probability 测试
# ---------------------------------------------------------------------------
def test_assert_no_numeric_probability_blocks_percentage():
    """含百分比的 ReasoningResult 应被拦截。"""
    bad_result = ReasoningResult(
        legal_relationship="测试",
        elements=["要件（已满足）"],
        disputed_focus=["争议"],
        plaintiff_arguments=["原告主张"],
        defendant_arguments=["被告主张"],
        evidence_mapping=["证据"],
        judicial_tendency="favorable",
        evidence_confidence="high",
        key_factors=["胜诉率 70%"],  # 含百分比
    )
    with pytest.raises(AssertionError, match="数字概率"):
        _assert_no_numeric_probability(bad_result)


def test_assert_no_numeric_probability_blocks_probability_keyword():
    """含概率关键词的 ReasoningResult 应被拦截。"""
    bad_result = ReasoningResult(
        legal_relationship="测试",
        elements=["要件（已满足）"],
        disputed_focus=["争议"],
        plaintiff_arguments=["原告主张"],
        defendant_arguments=["被告主张"],
        evidence_mapping=["证据"],
        judicial_tendency="favorable",
        evidence_confidence="high",
        key_factors=["胜诉概率较高"],  # 含概率关键词
    )
    with pytest.raises(AssertionError, match="数字概率"):
        _assert_no_numeric_probability(bad_result)


def test_assert_no_numeric_probability_blocks_range():
    """含概率区间的 ReasoningResult 应被拦截。"""
    bad_result = ReasoningResult(
        legal_relationship="测试",
        elements=["要件（已满足）"],
        disputed_focus=["争议"],
        plaintiff_arguments=["原告主张"],
        defendant_arguments=["被告主张"],
        evidence_mapping=["证据"],
        judicial_tendency="favorable",
        evidence_confidence="high",
        key_factors=["胜诉率 60%-80%"],  # 含概率区间
    )
    with pytest.raises(AssertionError, match="数字概率"):
        _assert_no_numeric_probability(bad_result)


def test_assert_no_numeric_probability_passes_valid_result():
    """合法 ReasoningResult（无数字概率）应通过自检。"""
    valid_result = ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）", "违约行为（未满足）"],
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["对方违约应赔偿"],
        defendant_arguments=["不可抗力免责"],
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency="somewhat_favorable",
        evidence_confidence="medium",
        key_factors=["违约行为待证实", "证据置信度中等"],
    )
    # 不应抛出异常
    _assert_no_numeric_probability(valid_result)


def test_assert_no_numeric_probability_allows_plain_amounts():
    """普通金额数字（如 10万元）不应被误拦截。"""
    valid_result = ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）"],
        disputed_focus=["欠款数额"],
        plaintiff_arguments=["主张款项数额：10万元"],
        defendant_arguments=["已还款"],
        evidence_mapping=["争议焦点1 → 借条"],
        judicial_tendency="favorable",
        evidence_confidence="high",
        key_factors=["本金10万元", "月工资8000元"],
    )
    # 不应抛出异常
    _assert_no_numeric_probability(valid_result)


# ---------------------------------------------------------------------------
# 4. 边界场景
# ---------------------------------------------------------------------------
def test_legal_reasoner_empty_statutes_returns_insufficient():
    """statutes 为空时，裁判倾向应为 insufficient。"""
    state = CaseState(
        run_id="run-empty",
        thread_id="thread-empty",
        current_date=date(2026, 7, 23),
        user_goal="测试空 statutes",
        case_type="合同纠纷",
    )
    result = legal_reasoner(state)
    rr = result["reasoning_result"]
    assert rr.judicial_tendency == "insufficient"
    assert result["confidence"] == "insufficient"


def test_legal_reasoner_critic_feedback_adjusts_tendency():
    """critic_feedback 含"过度推断"时，裁判倾向应下调。"""
    state = _labor_state()
    # 添加 critic_feedback
    state_dict = state.model_dump()
    state_dict["critic_feedback"] = ["过度推断：要件满足度不足却标注倾向原告"]
    result = legal_reasoner(state_dict)
    rr = result["reasoning_result"]
    # 下调后不应为 favorable
    assert rr.judicial_tendency != "favorable", (
        "critic_feedback 提示过度推断后，裁判倾向应被下调，不应为 favorable"
    )
