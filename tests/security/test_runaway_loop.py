"""SubTask 18.5：无限制循环测试（安全评测视角）。

验证 ``policies.py`` 的最大检索次数、成本预算与循环检测生效，以及
``citation_verifier`` / ``critic`` 节点的迭代上限守卫。

覆盖场景：
1. ``detect_loop``：同一查询出现 >= 3 次 → True；< 3 次 → False
2. ``check_retrieval_budget``：iteration 达上限 → False
3. ``check_cost_budget``：成本超预算 → False
4. ``enforce_policies``：循环失控 → PolicyViolationError(kind="loop")
5. ``enforce_policies``：检索预算耗尽 → PolicyViolationError(kind="retrieval_budget")
6. ``enforce_policies``：成本超预算 → PolicyViolationError(kind="cost_budget")
7. ``enforce_policies``：全部通过 → 不抛异常
8. ``enforce_policies``：检查顺序（loop 优先于 retrieval_budget / cost_budget）
9. citation_verifier 始终不通过 → reretrieval_count 不超过 2
10. critic 始终不通过 → iteration 不超过 MAX_LEGAL_REASONER_ITERATIONS
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lvyan.config import settings
from lvyan.graph.policies import (
    PolicyViolationError,
    check_cost_budget,
    check_retrieval_budget,
    detect_loop,
    enforce_policies,
)
from lvyan.nodes.citation_verifier import citation_verifier
from lvyan.nodes.critic import critic
from lvyan.schemas import Authority, ReasoningResult, RetrievalQuery


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------
def _query(query_text: str, qid: str | None = None) -> RetrievalQuery:
    return RetrievalQuery(
        query_id=qid or f"rq-{query_text[:8]}",
        query_text=query_text,
        route="hybrid",
        result_count=0,
    )


def _state_for_policies(
    iteration: int = 0,
    retrieval_queries: list[RetrievalQuery] | None = None,
) -> dict[str, Any]:
    """构造 policies 测试用 state dict。"""
    return {
        "iteration": iteration,
        "retrieval_queries": retrieval_queries or [],
    }


def _citation_state(
    reasoning_result: ReasoningResult | None,
    statutes: list[Authority],
    iteration: int = 0,
) -> dict[str, Any]:
    """构造 citation_verifier 节点测试用 state dict。"""
    return {
        "run_id": "run-loop-sec",
        "thread_id": "thread-loop-sec",
        "current_date": date(2026, 7, 23),
        "user_goal": "测试循环失控",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "light",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [
            _query("合同违约责任 法条", "rq-1"),
        ],
        "statutes": statutes,
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": reasoning_result,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "medium",
        "iteration": iteration,
        "final_output": "",
        "output_iteration": 0,
        "output_retry_needed": False,
        "pending_human_approval": None,
    }


def _critic_state(
    reasoning_result: ReasoningResult | None,
    iteration: int = 0,
    critic_feedback: list[str] | None = None,
) -> dict[str, Any]:
    """构造 critic 节点测试用 state dict。"""
    return {
        "run_id": "run-critic-loop",
        "thread_id": "thread-critic-loop",
        "current_date": date(2026, 7, 23),
        "user_goal": "测试 critic 循环",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "light",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": [],
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": reasoning_result,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "medium",
        "iteration": iteration,
        "critic_feedback": critic_feedback or [],
        "final_output": "",
        "output_iteration": 0,
        "output_retry_needed": False,
        "pending_human_approval": None,
    }


def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
) -> Authority:
    from datetime import datetime, timezone

    return Authority(
        source_id=f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=(
            "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
            "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
        ),
        authority_level="法律",
        effective_date=date(2021, 1, 1),
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_reasoning_result_fabricated() -> ReasoningResult:
    """构造始终含虚构法条的 reasoning_result（citation_verifier 必然不通过）。"""
    return ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）"],
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["原告主张对方违约"],
        defendant_arguments=["被告主张不可抗力免责"],
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency="somewhat_favorable",
        evidence_confidence="medium",
        key_factors=["依据《中华人民共和国虚构法》第一条判定甲方胜诉"],
    )


# ---------------------------------------------------------------------------
# 1. detect_loop
# ---------------------------------------------------------------------------
def test_detect_loop_triggered_at_three_occurrences():
    """同一 query_text 出现 3 次 → detect_loop 返回 True。"""
    state = _state_for_policies(
        retrieval_queries=[
            _query("相同查询", "q1"),
            _query("相同查询", "q2"),
            _query("相同查询", "q3"),
        ]
    )
    assert detect_loop(state) is True


def test_detect_loop_not_triggered_below_three():
    """同一 query_text 出现 2 次 → detect_loop 返回 False。"""
    state = _state_for_policies(
        retrieval_queries=[
            _query("相同查询", "q1"),
            _query("相同查询", "q2"),
            _query("不同查询", "q3"),
        ]
    )
    assert detect_loop(state) is False


def test_detect_loop_empty_queries():
    """无 retrieval_queries → detect_loop 返回 False。"""
    assert detect_loop(_state_for_policies()) is False


def test_detect_loop_multiple_distinct_queries():
    """多条不同查询（各 1 次）→ detect_loop 返回 False。"""
    state = _state_for_policies(
        retrieval_queries=[
            _query("查询一", "q1"),
            _query("查询二", "q2"),
            _query("查询三", "q3"),
        ]
    )
    assert detect_loop(state) is False


# ---------------------------------------------------------------------------
# 2. check_retrieval_budget
# ---------------------------------------------------------------------------
def test_check_retrieval_budget_within_limit(monkeypatch: pytest.MonkeyPatch):
    """iteration < max_retrieval_iterations → True（仍有预算）。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 3)
    assert check_retrieval_budget(_state_for_policies(iteration=2)) is True


def test_check_retrieval_budget_at_limit(monkeypatch: pytest.MonkeyPatch):
    """iteration >= max_retrieval_iterations → False（预算耗尽）。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 3)
    assert check_retrieval_budget(_state_for_policies(iteration=3)) is False
    assert check_retrieval_budget(_state_for_policies(iteration=10)) is False


# ---------------------------------------------------------------------------
# 3. check_cost_budget
# ---------------------------------------------------------------------------
def test_check_cost_budget_within_limit(monkeypatch: pytest.MonkeyPatch):
    """iteration * 0.5 <= max_cost_budget → True。"""
    monkeypatch.setattr(settings, "max_cost_budget_usd", 2.0)
    # iteration=4 → cost=2.0 <= 2.0
    assert check_cost_budget(_state_for_policies(iteration=4)) is True


def test_check_cost_budget_exceeded(monkeypatch: pytest.MonkeyPatch):
    """iteration * 0.5 > max_cost_budget → False（超支）。"""
    monkeypatch.setattr(settings, "max_cost_budget_usd", 1.0)
    # iteration=3 → cost=1.5 > 1.0
    assert check_cost_budget(_state_for_policies(iteration=3)) is False


# ---------------------------------------------------------------------------
# 4-7. enforce_policies
# ---------------------------------------------------------------------------
def test_enforce_policies_loop_violation(monkeypatch: pytest.MonkeyPatch):
    """循环失控 → PolicyViolationError(kind='loop')。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 10)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 100.0)
    state = _state_for_policies(
        iteration=1,
        retrieval_queries=[
            _query("重复查询", "q1"),
            _query("重复查询", "q2"),
            _query("重复查询", "q3"),
        ],
    )
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    assert exc_info.value.kind == "loop"


def test_enforce_policies_retrieval_budget_violation(monkeypatch: pytest.MonkeyPatch):
    """检索预算耗尽 → PolicyViolationError(kind='retrieval_budget')。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 2)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 100.0)
    state = _state_for_policies(iteration=2)  # 无循环
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    assert exc_info.value.kind == "retrieval_budget"


def test_enforce_policies_cost_budget_violation(monkeypatch: pytest.MonkeyPatch):
    """成本超预算 → PolicyViolationError(kind='cost_budget')。"""
    # 设置较大的检索预算，使 cost 先超
    monkeypatch.setattr(settings, "max_retrieval_iterations", 100)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 1.0)
    state = _state_for_policies(iteration=3)  # cost=1.5 > 1.0，无循环，retrieval 未达上限
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    assert exc_info.value.kind == "cost_budget"


def test_enforce_policies_all_pass(monkeypatch: pytest.MonkeyPatch):
    """全部策略通过 → 不抛异常。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 5)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 10.0)
    state = _state_for_policies(
        iteration=1,
        retrieval_queries=[_query("查询一", "q1"), _query("查询二", "q2")],
    )
    # 不应抛异常
    enforce_policies(state)


# ---------------------------------------------------------------------------
# 8. enforce_policies 检查顺序（loop 优先）
# ---------------------------------------------------------------------------
def test_enforce_policies_loop_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    """loop 优先于 retrieval_budget / cost_budget：同时违反时抛 loop。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 1)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 0.1)
    state = _state_for_policies(
        iteration=5,  # retrieval_budget 与 cost_budget 均违反
        retrieval_queries=[
            _query("重复", "q1"),
            _query("重复", "q2"),
            _query("重复", "q3"),
        ],
    )
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    # loop 优先检查
    assert exc_info.value.kind == "loop"


def test_enforce_policies_retrieval_before_cost(monkeypatch: pytest.MonkeyPatch):
    """retrieval_budget 优先于 cost_budget：同时违反时抛 retrieval_budget。"""
    monkeypatch.setattr(settings, "max_retrieval_iterations", 2)
    monkeypatch.setattr(settings, "max_cost_budget_usd", 0.1)
    state = _state_for_policies(iteration=5)  # 无循环
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    assert exc_info.value.kind == "retrieval_budget"


# ---------------------------------------------------------------------------
# 9. citation_verifier 始终不通过 → reretrieval_count 不超过 2
# ---------------------------------------------------------------------------
def test_citation_verifier_reretrieval_count_capped(
    monkeypatch: pytest.MonkeyPatch,
    make_authority,
    mock_statute_status_effective,
):
    """citation_verifier 始终不通过：reretrieval_count 不超过 min(max_iter, 2)=2。

    即使 settings.max_retrieval_iterations 设置为 10，citation_verifier 内部
    仍以 min(max, 2)=2 为上限，防止无限制重检索。
    """
    monkeypatch.setattr(settings, "max_retrieval_iterations", 10)
    rr = _make_reasoning_result_fabricated()
    statutes = [make_authority(article_number="第五百七十七条")]

    # 模拟连续多次调用：iteration 0 → 1 → 2 → 强制通过
    for start_iter in [0, 1]:
        state = _citation_state(rr, statutes, iteration=start_iter)
        result = citation_verifier(state)
        assert result["citation_audit"]["reretrieval_count"] == start_iter + 1
        assert result["iteration"] == start_iter + 1
        assert result["citation_audit"]["passed"] is False

    # iteration=2 → 强制通过，reretrieval_count 不再增加
    state = _citation_state(rr, statutes, iteration=2)
    result = citation_verifier(state)
    assert result["citation_audit"]["reretrieval_count"] == 2
    assert "iteration" not in result  # 不再重检索
    assert result.get("risk_level") == "high"
    assert result.get("confidence") == "insufficient"


def test_citation_verifier_never_exceeds_two_reretrievals(
    monkeypatch: pytest.MonkeyPatch,
    make_authority,
    mock_statute_status_effective,
):
    """即使 max_retrieval_iterations=100，连续流程中 reretrieval_count 始终 <= 2。

    模拟从 iteration=0 开始的连续重检索流程：0→1→2→强制通过。
    citation_verifier 内部以 min(max_iter, 2)=2 为上限，iteration 在 2 处封顶，
    不会再增长（强制通过路径不返回 iteration）。
    """
    monkeypatch.setattr(settings, "max_retrieval_iterations", 100)
    rr = _make_reasoning_result_fabricated()
    statutes = [make_authority(article_number="第五百七十七条")]

    # 连续流程：iteration 从 0 开始，每次用上一步返回的 iteration 继续调用
    current_iter = 0
    max_reretrieval = 0
    for _ in range(5):  # 最多模拟 5 步，应在 iteration=2 处终止
        state = _citation_state(rr, statutes, iteration=current_iter)
        result = citation_verifier(state)
        max_reretrieval = max(max_reretrieval, result["citation_audit"]["reretrieval_count"])
        # 强制通过路径不返回 iteration → 流程终止
        if "iteration" not in result:
            # 已强制通过，risk_level=high
            assert result.get("risk_level") == "high"
            break
        current_iter = result["iteration"]

    assert max_reretrieval <= 2
    assert current_iter <= 2  # iteration 不超过 2


# ---------------------------------------------------------------------------
# 10. critic 始终不通过 → iteration 不超过 MAX_LEGAL_REASONER_ITERATIONS
# ---------------------------------------------------------------------------
def test_critic_iteration_capped(monkeypatch: pytest.MonkeyPatch):
    """critic 始终不通过：iteration 不超过 MAX_LEGAL_REASONER_ITERATIONS。"""
    # 保留测试意图：允许 2 次回退（默认值已降为 1 以减少 LLM 调用）
    monkeypatch.setattr("lvyan.nodes.critic.MAX_LEGAL_REASONER_ITERATIONS", 2)
    from lvyan.nodes.critic import MAX_LEGAL_REASONER_ITERATIONS as _max_iter
    assert _max_iter == 2

    # reasoning_result=None → critic 必然不通过
    state_0 = _critic_state(reasoning_result=None, iteration=0)
    result_0 = critic(state_0)
    assert result_0["critic_report"]["passed"] is False
    assert result_0["iteration"] == 1

    state_1 = _critic_state(reasoning_result=None, iteration=1)
    result_1 = critic(state_1)
    assert result_1["critic_report"]["passed"] is False
    assert result_1["iteration"] == 2

    # iteration=2 >= MAX → 强制通过，不再 +1
    state_2 = _critic_state(reasoning_result=None, iteration=2)
    result_2 = critic(state_2)
    assert result_2["critic_report"]["passed"] is True  # 强制通过
    assert result_2["critic_report"]["forced_pass"] is True
    assert "iteration" not in result_2  # 不再回退
    assert result_2.get("risk_level") == "high"


def test_critic_iteration_never_exceeds_max(monkeypatch: pytest.MonkeyPatch):
    """连续模拟多次 critic，iteration 始终 <= MAX_LEGAL_REASONER_ITERATIONS。"""
    monkeypatch.setattr("lvyan.nodes.critic.MAX_LEGAL_REASONER_ITERATIONS", 2)
    from lvyan.nodes.critic import MAX_LEGAL_REASONER_ITERATIONS as _max_iter

    max_iteration = 0
    for start_iter in range(5):
        state = _critic_state(reasoning_result=None, iteration=start_iter)
        result = critic(state)
        if "iteration" in result:
            max_iteration = max(max_iteration, result["iteration"])
        # 强制通过时不返回 iteration，说明已达上限
    assert max_iteration <= _max_iter


def test_critic_passes_when_reasoning_present(monkeypatch: pytest.MonkeyPatch, make_reasoning_result):
    """critic 对正常 reasoning_result（无遗漏/过度推断/冲突）→ 通过。"""
    monkeypatch.setattr("lvyan.nodes.critic.MAX_LEGAL_REASONER_ITERATIONS", 2)
    rr = make_reasoning_result()
    state = _critic_state(reasoning_result=rr, iteration=0)
    result = critic(state)
    # 正常推理结果应通过（无 issues）
    assert result["critic_report"]["passed"] is True
    assert "iteration" not in result
