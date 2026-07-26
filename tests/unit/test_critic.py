"""Critic 节点单元测试。

覆盖 Task 12 SubTask 12.3 验证标准：
1. critic 通过测试：构造良好的 state，验证 critic_report.passed = True。
2. critic 遗漏反方论点测试：defendant_arguments 为空，验证不通过。
3. critic 过度推断测试：3/5 要件未满足但 ruling_tendency="favorable"，验证不通过。
4. critic 法规冲突测试：statutes 有多版本但 conflicts 为空，验证不通过。
5. critic 回退测试：验证 iteration 计数累加，超过 MAX_LEGAL_REASONER_ITERATIONS 后强制通过。
6. route_after_critic 路由测试。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lvyan.graph.routing import route_after_critic
from lvyan.nodes.critic import (
    MAX_LEGAL_REASONER_ITERATIONS,
    CriticReport,
    critic,
)
from lvyan.schemas import (
    Authority,
    CaseState,
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
    source_id: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level=authority_level,
        effective_date=effective_date,
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_reasoning_result(
    elements: list[str] | None = None,
    defendant_arguments: list[str] | None = None,
    judicial_tendency: str = "somewhat_favorable",
    evidence_confidence: str = "medium",
) -> ReasoningResult:
    """构造 ReasoningResult，参数可定制。"""
    if elements is None:
        elements = [
            "要件1（已满足）",
            "要件2（已满足）",
            "要件3（已满足）",
        ]
    if defendant_arguments is None:
        defendant_arguments = ["被告主张1：不存在违约行为"]
    return ReasoningResult(
        legal_relationship="合同纠纷",
        elements=elements,
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["原告主张1：对方违约应赔偿"],
        defendant_arguments=defendant_arguments,
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency=judicial_tendency,  # type: ignore[arg-type]
        evidence_confidence=evidence_confidence,  # type: ignore[arg-type]
        key_factors=["违约行为待证实"],
    )


def _make_state(
    reasoning_result: ReasoningResult | None = None,
    statutes: list[Authority] | None = None,
    conflicts: list | None = None,
    iteration: int = 0,
    critic_feedback: list[str] | None = None,
) -> dict:
    """构造测试用 state dict。"""
    if statutes is None:
        statutes = [
            _make_authority(
                title="中华人民共和国民法典",
                article_number="第五百七十七条",
                article_text="当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
                effective_date=date(2021, 1, 1),
            ),
        ]
    return {
        "run_id": "run-critic-test",
        "thread_id": "thread-critic-test",
        "current_date": date(2026, 7, 23),
        "user_goal": "测试 critic",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "deep",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": statutes,
        "cases": [],
        "evidence_requirements": [],
        "conflicts": conflicts or [],
        "reasoning_result": reasoning_result,
        "citation_audit": None,
        "critic_report": None,
        "critic_feedback": critic_feedback or [],
        "risk_level": "low",
        "confidence": "medium",
        "iteration": iteration,
        "final_output": None,
    }


# ---------------------------------------------------------------------------
# 1. critic 通过测试
# ---------------------------------------------------------------------------
def test_critic_passes_good_state():
    """构造良好的 state（有被告主张、要件满足度合理、无冲突），应通过。"""
    rr = _make_reasoning_result(
        elements=["要件1（已满足）", "要件2（已满足）", "要件3（已满足）"],
        defendant_arguments=["被告主张1：不可抗力免责"],
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr)
    result = critic(state)

    assert "critic_report" in result
    report = result["critic_report"]
    assert isinstance(report, dict)
    assert report["passed"] is True
    assert len(report["issues"]) == 0
    assert report["forced_pass"] is False


def test_critic_passes_no_statutes():
    """statutes 为空时（无法律依据），不检查反方论点，应通过。"""
    rr = _make_reasoning_result(
        elements=[],  # 无构成要件，匹配 insufficient 倾向，不触发过度保守检查
        defendant_arguments=[],
        judicial_tendency="insufficient",
    )
    state = _make_state(reasoning_result=rr, statutes=[])
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# 2. critic 遗漏反方论点测试
# ---------------------------------------------------------------------------
def test_critic_fails_missing_defendant_arguments():
    """defendant_arguments 为空但 statutes 非空时，应不通过。"""
    rr = _make_reasoning_result(
        defendant_arguments=[],  # 空被告主张
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr)
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("遗漏反方论点" in issue for issue in report["issues"])


# ---------------------------------------------------------------------------
# 3. critic 过度推断测试
# ---------------------------------------------------------------------------
def test_critic_fails_over_inference():
    """3/5 要件未满足但 ruling_tendency=favorable，应不通过。"""
    rr = _make_reasoning_result(
        elements=[
            "要件1（已满足）",
            "要件2（未满足）",
            "要件3（未满足）",
            "要件4（未满足）",
            "要件5（已满足）",
        ],
        defendant_arguments=["被告主张1"],
        judicial_tendency="favorable",  # 过度推断
    )
    state = _make_state(reasoning_result=rr)
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("过度推断" in issue for issue in report["issues"])


def test_critic_fails_over_conservative():
    """全部要件满足却标注 somewhat_unfavorable，应不通过（过度保守）。"""
    rr = _make_reasoning_result(
        elements=["要件1（已满足）", "要件2（已满足）"],
        defendant_arguments=["被告主张1"],
        judicial_tendency="somewhat_unfavorable",
    )
    state = _make_state(reasoning_result=rr)
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("过度保守" in issue for issue in report["issues"])


def test_critic_passes_reasonable_tendency():
    """3/5 要件满足 + even 裁判倾向，应通过（匹配合理）。"""
    rr = _make_reasoning_result(
        elements=[
            "要件1（已满足）",
            "要件2（已满足）",
            "要件3（未满足）",
            "要件4（未满足）",
            "要件5（已满足）",
        ],
        defendant_arguments=["被告主张1"],
        judicial_tendency="even",  # 合理
    )
    state = _make_state(reasoning_result=rr)
    result = critic(state)
    report = result["critic_report"]
    # 过度推断检查应通过（40% 满足 + even 不触发）
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# 4. critic 法规冲突测试
# ---------------------------------------------------------------------------
def test_critic_fails_unhandled_statute_version_conflict():
    """statutes 中同一 title 有多个 effective_date 版本但 conflicts 为空，应不通过。"""
    statutes = [
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款（2021版）",
            effective_date=date(2021, 1, 1),
            source_id="src-civil-v2",
        ),
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款（旧版）",
            effective_date=date(2010, 1, 1),
            source_id="src-civil-v1",
        ),
    ]
    rr = _make_reasoning_result(
        defendant_arguments=["被告主张1"],
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr, statutes=statutes, conflicts=[])
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("法规冲突未处理" in issue for issue in report["issues"])


def test_critic_fails_unhandled_statute_hierarchy_conflict():
    """statutes 中同一 title 有多个 authority_level 但 conflicts 为空，应不通过。"""
    statutes = [
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款",
            effective_date=date(2021, 1, 1),
            authority_level="法律",
            source_id="src-civil-law",
        ),
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款（司法解释）",
            effective_date=date(2021, 1, 1),
            authority_level="司法解释",
            source_id="src-civil-interpretation",
        ),
    ]
    rr = _make_reasoning_result(
        defendant_arguments=["被告主张1"],
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr, statutes=statutes, conflicts=[])
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("法规冲突未处理" in issue for issue in report["issues"])


def test_critic_passes_when_conflicts_already_populated():
    """statutes 有多版本但 conflicts 已填充时，应通过（冲突已处理）。"""
    from lvyan.schemas import AuthorityConflict

    statutes = [
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款（2021版）",
            effective_date=date(2021, 1, 1),
            source_id="src-civil-v2",
        ),
        _make_authority(
            title="中华人民共和国民法典",
            article_number="第五百七十七条",
            article_text="违约责任条款（旧版）",
            effective_date=date(2010, 1, 1),
            source_id="src-civil-v1",
        ),
    ]
    conflicts = [
        AuthorityConflict(
            conflict_id="c1",
            authority_ids=["src-civil-v2", "src-civil-v1"],
            conflict_type="version",
            description="民法典存在多个版本",
            resolution="优先适用最新版本",
        ),
    ]
    rr = _make_reasoning_result(
        defendant_arguments=["被告主张1"],
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr, statutes=statutes, conflicts=conflicts)
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# 5. critic 回退与迭代测试
# ---------------------------------------------------------------------------
def test_critic_iteration_increments_on_failure():
    """不通过且 iteration < MAX 时，iteration 应 +1。"""
    rr = _make_reasoning_result(
        defendant_arguments=[],  # 触发失败
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(reasoning_result=rr, iteration=0)
    result = critic(state)

    assert result["iteration"] == 1
    assert result["critic_report"]["passed"] is False
    assert len(result["critic_feedback"]) > 0


def test_critic_feedback_accumulates():
    """多次回退时 critic_feedback 应累积（去重）。"""
    rr = _make_reasoning_result(
        defendant_arguments=[],  # 触发失败
        judicial_tendency="somewhat_favorable",
    )
    # 第一次：已有 1 条 feedback
    state = _make_state(
        reasoning_result=rr,
        iteration=1,
        critic_feedback=["之前的反馈"],
    )
    result = critic(state)

    assert result["iteration"] == 2
    assert "之前的反馈" in result["critic_feedback"]
    # 新 issue 也应被追加
    assert any("遗漏反方论点" in fb for fb in result["critic_feedback"])


def test_critic_forces_pass_at_max_iterations():
    """iteration >= MAX_LEGAL_REASONER_ITERATIONS 时应强制通过并标记高风险。"""
    rr = _make_reasoning_result(
        defendant_arguments=[],  # 触发失败
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(
        reasoning_result=rr,
        iteration=MAX_LEGAL_REASONER_ITERATIONS,
    )
    result = critic(state)

    assert result["critic_report"]["passed"] is True  # 强制通过
    assert result["critic_report"]["forced_pass"] is True
    assert result["critic_report"]["warning"] == "自动 critic 未通过，需人工复核"
    assert result["risk_level"] == "high"
    # 仍应有 issues 记录
    assert len(result["critic_report"]["issues"]) > 0


def test_critic_forces_pass_beyond_max_iterations():
    """iteration 超过 MAX_LEGAL_REASONER_ITERATIONS 时也应强制通过。"""
    rr = _make_reasoning_result(
        defendant_arguments=[],
        judicial_tendency="somewhat_favorable",
    )
    state = _make_state(
        reasoning_result=rr,
        iteration=MAX_LEGAL_REASONER_ITERATIONS + 5,
    )
    result = critic(state)
    assert result["critic_report"]["passed"] is True
    assert result["critic_report"]["forced_pass"] is True
    assert result["risk_level"] == "high"


def test_critic_max_iterations_is_positive():
    """MAX_LEGAL_REASONER_ITERATIONS 应为正整数（默认 2）。"""
    assert isinstance(MAX_LEGAL_REASONER_ITERATIONS, int)
    assert MAX_LEGAL_REASONER_ITERATIONS > 0


# ---------------------------------------------------------------------------
# 6. route_after_critic 路由测试
# ---------------------------------------------------------------------------
def test_route_after_critic_passed_returns_citation_verifier():
    """critic_report.passed=True → composer（P1-9b：先组装初稿）。"""
    state = _make_state()
    state["critic_report"] = {"passed": True, "issues": [], "forced_pass": False}
    assert route_after_critic(state) == "composer"


def test_route_after_critic_failed_returns_legal_reasoner():
    """critic_report.passed=False → legal_reasoner。"""
    state = _make_state()
    state["critic_report"] = {
        "passed": False,
        "issues": ["测试问题"],
        "forced_pass": False,
    }
    assert route_after_critic(state) == "legal_reasoner"


def test_route_after_critic_no_report_returns_citation_verifier():
    """critic_report=None → composer（P1-9b：默认通过，先组装初稿）。"""
    state = _make_state()
    state["critic_report"] = None
    assert route_after_critic(state) == "composer"


def test_route_after_critic_forced_pass_returns_citation_verifier():
    """critic_report.passed=True（强制通过）→ composer（P1-9b）。"""
    state = _make_state()
    state["critic_report"] = {
        "passed": True,
        "issues": ["仍有问题"],
        "forced_pass": True,
        "warning": "自动 critic 未通过，需人工复核",
    }
    assert route_after_critic(state) == "composer"


# ---------------------------------------------------------------------------
# 7. CriticReport 模型测试
# ---------------------------------------------------------------------------
def test_critic_report_model_defaults():
    """CriticReport 默认值正确。"""
    report = CriticReport(passed=True)
    assert report.passed is True
    assert report.issues == []
    assert report.suggestions == []
    assert report.forced_pass is False
    assert report.warning is None


def test_critic_report_model_serialization():
    """CriticReport 可序列化为 dict 并还原。"""
    report = CriticReport(
        passed=False,
        issues=["问题1"],
        suggestions=["建议1"],
        forced_pass=False,
    )
    data = report.model_dump()
    assert data["passed"] is False
    assert data["issues"] == ["问题1"]
    assert data["suggestions"] == ["建议1"]
    assert data["forced_pass"] is False

    restored = CriticReport.model_validate(data)
    assert restored.passed is False
    assert restored.issues == ["问题1"]


# ---------------------------------------------------------------------------
# 8. 无 reasoning_result 边界场景
# ---------------------------------------------------------------------------
def test_critic_fails_when_no_reasoning_result():
    """reasoning_result 为空时，应不通过。"""
    state = _make_state(reasoning_result=None, iteration=0)
    result = critic(state)
    report = result["critic_report"]
    assert report["passed"] is False
    assert any("reasoning_result" in issue for issue in report["issues"])
    assert result["iteration"] == 1  # iteration +1
