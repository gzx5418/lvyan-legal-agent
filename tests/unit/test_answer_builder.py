"""build_legal_answer 构建器测试。"""

from __future__ import annotations

from datetime import date, datetime

from lvyan.schemas.case import CaseState, Fact, MissingFact
from lvyan.schemas.authority import Authority
from lvyan.schemas.evidence import EvidenceRequirement
from lvyan.schemas.output import ReasoningResult

from lvyan.nodes.answer_builder import build_legal_answer


def _make_state(**overrides) -> CaseState:
    """构造测试用 CaseState。"""
    defaults = dict(
        run_id="run-1",
        thread_id="thread-1",
        current_date=date(2026, 8, 2),
        user_goal="追回押金",
        jurisdiction="中国大陆",
        case_type="房屋租赁合同纠纷",
        complexity="deep",
        risk_level="medium",
        confidence="medium",
        law_as_of_date=date(2026, 8, 2),
    )
    defaults.update(overrides)
    return CaseState(**defaults)


def test_build_minimal_state():
    """空状态也能构建合法 LegalAnswerV1（带免责声明）。"""
    state = _make_state()
    answer = build_legal_answer(state)
    assert answer.schema_version == "legal_answer_v1"
    assert answer.meta.case_type == "房屋租赁合同纠纷"
    assert answer.meta.risk_level == "medium"
    assert "法律信息分析" in answer.disclaimer


def test_facts_classified_by_source():
    """Fact 按 source 映射到四态标签。"""
    state = _make_state(
        facts=[
            Fact(fact_id="F1", category="金额", content="押金3000元", source="document"),
            Fact(fact_id="F2", category="行为", content="房东拒退", source="user"),
            Fact(fact_id="F3", category="其他", content="推测无损坏", source="llm"),
        ],
        missing_facts=[
            MissingFact(fact_key="M1", question="有无交接记录?", reason="影响判定"),
        ],
    )
    answer = build_legal_answer(state)
    by_id = {f.fact_id: f for f in answer.facts}
    assert by_id["F1"].status == "confirmed"
    assert by_id["F2"].status == "claimed"
    assert by_id["F3"].status == "inferred"
    assert any(f.status == "missing" for f in answer.facts)


def test_statutes_become_citations():
    """Authority 转换为 LegalCitation，保留 full_name/article/status。"""
    state = _make_state(
        statutes=[
            Authority(
                source_id="S1",
                title="中华人民共和国民法典",
                article_number="第七百零三条",
                article_text="租赁合同是出租人...",
                authority_level="法律",
                status="effective",
                retrieved_at=datetime(2026, 8, 2),
            ),
        ],
    )
    answer = build_legal_answer(state)
    assert len(answer.citations) == 1
    c = answer.citations[0]
    assert c.full_name == "中华人民共和国民法典"
    assert c.article_number == "第七百零三条"
    assert c.level == "law"
    assert c.status == "effective"


def test_reasoning_result_maps_to_issues():
    """disputed_focus 映射为 LegalIssue 列表。"""
    state = _make_state(
        reasoning_result=ReasoningResult(
            judicial_tendency="somewhat_favorable",
            evidence_confidence="medium",
            disputed_focus=["房东能否扣除全部押金", "损坏举证责任归属"],
            key_factors=["押金支付事实", "缺少交接记录"],
        ),
    )
    answer = build_legal_answer(state)
    assert len(answer.issues) == 2
    assert answer.issues[0].question == "房东能否扣除全部押金"


def test_evidence_requirements_map_to_matrix():
    """EvidenceRequirement 映射为证据矩阵行。"""
    state = _make_state(
        evidence_requirements=[
            EvidenceRequirement(
                requirement_id="E1",
                fact_to_prove="已支付押金",
                evidence_types=["转账记录"],
                current_status="met",
            ),
            EvidenceRequirement(
                requirement_id="E2",
                fact_to_prove="房屋交接状态",
                evidence_types=["交接单", "照片"],
                current_status="missing",
                gap_description="未提供",
            ),
        ],
    )
    answer = build_legal_answer(state)
    assert len(answer.evidence) == 2
    by_id = {e.evidence_id: e for e in answer.evidence}
    assert by_id["E1"].status == "provided"
    assert by_id["E2"].status == "missing"


def test_contract_action_plan_does_not_use_universal_deduction_template():
    answer = build_legal_answer(_make_state(case_type="合同纠纷"))
    descriptions = " ".join(item.description for item in answer.action_plan)
    assert "具体扣款项目" not in descriptions
    assert "合同依据" in descriptions


def test_insufficient_judicial_tendency_is_neutral_not_high_risk():
    state = _make_state(
        reasoning_result=ReasoningResult(
            judicial_tendency="insufficient",
            evidence_confidence="low",
        ),
    )
    answer = build_legal_answer(state)
    tendency = next(item for item in answer.risks if item.dimension == "司法裁判趋势")
    assert tendency.rating == "medium"


def test_risk_level_maps_to_risk_matrix():
    """risk_level 映射为综合风险维度 + 多维度评估。"""
    state = _make_state(
        risk_level="high",
        confidence="low",
    )
    answer = build_legal_answer(state)
    assert len(answer.risks) >= 1
    assert any(r.dimension == "综合风险" and r.rating == "high" for r in answer.risks)


def test_action_plan_generated_from_missing_facts():
    """缺失事实生成 immediate 行动建议。"""
    state = _make_state(
        missing_facts=[
            MissingFact(
                fact_key="M1", question="补充租赁合同", reason="关键证据", is_blocking=True
            ),
        ],
    )
    answer = build_legal_answer(state)
    assert any(a.phase == "immediate" for a in answer.action_plan)


def test_work_injury_action_plan_uses_recognition_deadlines():
    """工伤认定场景应给出事故责任证据与30日/1年申请期限。"""
    answer = build_legal_answer(
        _make_state(case_type="工伤认定", user_goal="上班途中发生交通事故是否工伤")
    )
    descriptions = " ".join(item.description for item in answer.action_plan)
    assert "道路交通事故责任认定书" in descriptions
    assert "30日" in descriptions
    assert "1年" in descriptions
    assert "扣款项目" not in descriptions


def test_composer_writes_legal_answer_to_state():
    """composer 节点应在 final_output 之外同时写入 legal_answer。"""
    from lvyan.nodes.composer import composer

    state = _make_state()
    result = composer(state)
    assert "final_output" in result
    assert "legal_answer" in result
    assert result["legal_answer"]["schema_version"] == "legal_answer_v1"
