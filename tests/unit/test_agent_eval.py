"""Task 17.2: Agent 评测脚本的 pytest 测试。

覆盖：
  1. AgentEvalResult / AgentEvalReport 数据模型字段完整性
  2. 工具选择正确率（LCS 匹配）
  3. 无效工具调用数
  4. 循环失控检测（iteration / reretrieval_count）
  5. 中断恢复成功率
  6. 应提问而未提问 / 不必要追问
  7. 平均成本和延迟
  8. 批量评测聚合
  9. 报告序列化
  10. LCS 辅助函数
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


# 路径引导：把 tests/evals 加入 sys.path，便于 import agent_eval
_THIS_DIR = Path(__file__).resolve().parent  # tests/unit/
_EVALS_DIR = _THIS_DIR.parent / "evals"  # tests/evals/
if str(_EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALS_DIR))

from agent_eval import (  # noqa: E402
    KNOWN_TOOLS,
    AgentEvalReport,
    AgentEvalResult,
    AgentRunRecord,
    ToolCallRecord,
    _compute_tool_selection_accuracy,
    _count_invalid_tool_calls,
    _is_runaway,
    _lcs_length,
    evaluate_agent_batch,
    evaluate_agent_run,
)


# ---------------------------------------------------------------------------
# 辅助：构造 mock state / record
# ---------------------------------------------------------------------------
def _make_state(
    iteration: int = 0,
    reretrieval_count: int = 0,
    missing_facts: list[dict[str, Any]] | None = None,
    has_final_output: bool = True,
) -> dict[str, Any]:
    citation_audit = None
    if reretrieval_count > 0:
        citation_audit = {
            "passed": True,
            "total_citations": 1,
            "verified": 1,
            "fabricated": 0,
            "repealed_cited": 0,
            "unsupported": 0,
            "details": [],
            "reretrieval_count": reretrieval_count,
        }
    return {
        "iteration": iteration,
        "citation_audit": citation_audit,
        "missing_facts": missing_facts or [],
        "final_output": "分析结果" if has_final_output else None,
    }


def _make_record(
    query_id: str = "test_001",
    state: dict[str, Any] | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
    expected_tool_sequence: list[str] | None = None,
    interrupted: bool = False,
    recovered: bool = False,
    asked_missing_facts: bool = False,
    model_call_count: int = 0,
    token_count: int = 0,
    elapsed_seconds: float = 0.0,
) -> AgentRunRecord:
    return AgentRunRecord(
        query_id=query_id,
        state=state or _make_state(),
        tool_calls=tool_calls or [],
        expected_tool_sequence=expected_tool_sequence or [],
        interrupted=interrupted,
        recovered=recovered,
        asked_missing_facts=asked_missing_facts,
        model_call_count=model_call_count,
        token_count=token_count,
        elapsed_seconds=elapsed_seconds,
    )


# ---------------------------------------------------------------------------
# 1. 数据模型字段完整性
# ---------------------------------------------------------------------------
def test_agent_eval_result_has_all_metric_fields():
    """AgentEvalResult 应含全部 Agent 评测指标字段。"""
    AgentEvalResult(query_id="test_001")
    required_fields = {
        "query_id",
        "tool_selection_accuracy",
        "invalid_tool_calls",
        "has_expected_sequence",
        "is_runaway",
        "iteration",
        "reretrieval_count",
        "interrupted",
        "recovered",
        "should_ask",
        "asked",
        "missed_asking",
        "unnecessary_asking",
        "model_call_count",
        "token_count",
        "elapsed_seconds",
    }
    actual_fields = set(AgentEvalResult.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, f"AgentEvalResult 缺少字段: {missing}"


def test_agent_eval_report_has_all_metric_fields():
    """AgentEvalReport 应含全部聚合指标字段。"""
    AgentEvalReport()
    required_fields = {
        "total_runs",
        "avg_tool_selection_accuracy",
        "total_invalid_tool_calls",
        "avg_invalid_tool_calls",
        "runaway_rate",
        "recovery_success_rate",
        "missed_asking_rate",
        "unnecessary_asking_rate",
        "avg_model_call_count",
        "avg_token_count",
        "avg_elapsed_seconds",
        "per_run",
    }
    actual_fields = set(AgentEvalReport.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, f"AgentEvalReport 缺少字段: {missing}"


def test_tool_call_record_fields():
    """ToolCallRecord 应含 tool_name / success / error_message 字段。"""
    tc = ToolCallRecord(tool_name="search_statutes")
    assert tc.tool_name == "search_statutes"
    assert tc.success is True
    assert tc.error_message is None


def test_agent_run_record_fields():
    """AgentRunRecord 应含运行记录全部字段。"""
    AgentRunRecord(query_id="q1")
    required_fields = {
        "query_id",
        "state",
        "tool_calls",
        "expected_tool_sequence",
        "interrupted",
        "recovered",
        "asked_missing_facts",
        "model_call_count",
        "token_count",
        "elapsed_seconds",
    }
    actual_fields = set(AgentRunRecord.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, f"AgentRunRecord 缺少字段: {missing}"


# ---------------------------------------------------------------------------
# 2. 工具选择正确率（LCS）
# ---------------------------------------------------------------------------
def test_tool_selection_accuracy_perfect():
    """实际工具序列与金标完全一致 → accuracy=1.0。"""
    accuracy = _compute_tool_selection_accuracy(
        actual=["search_statutes", "get_statute_article"],
        expected=["search_statutes", "get_statute_article"],
    )
    assert accuracy == 1.0


def test_tool_selection_accuracy_partial():
    """部分匹配：金标 3 个，实际匹配 2 个 → accuracy=2/3。"""
    accuracy = _compute_tool_selection_accuracy(
        actual=["search_statutes", "get_statute_article"],
        expected=["search_statutes", "get_statute_article", "verify_statute_status"],
    )
    assert abs(accuracy - 2 / 3) < 1e-6


def test_tool_selection_accuracy_no_expected():
    """无金标序列 → accuracy=1.0（无约束）。"""
    accuracy = _compute_tool_selection_accuracy(actual=["search_statutes"], expected=[])
    assert accuracy == 1.0


def test_tool_selection_accuracy_order_matters():
    """顺序不同 → LCS 较短 → accuracy 较低。"""
    accuracy = _compute_tool_selection_accuracy(
        actual=["get_statute_article", "search_statutes"],
        expected=["search_statutes", "get_statute_article"],
    )
    # LCS=1（任一单独匹配），accuracy=0.5
    assert accuracy == 0.5


def test_evaluate_agent_run_tool_selection():
    """evaluate_agent_run 应正确计算工具选择正确率。"""
    record = _make_record(
        tool_calls=[
            ToolCallRecord(tool_name="search_statutes"),
            ToolCallRecord(tool_name="get_statute_article"),
        ],
        expected_tool_sequence=["search_statutes", "get_statute_article"],
    )
    result = evaluate_agent_run(record)
    assert result.tool_selection_accuracy == 1.0
    assert result.has_expected_sequence is True


# ---------------------------------------------------------------------------
# 3. 无效工具调用数
# ---------------------------------------------------------------------------
def test_count_invalid_tool_calls_unknown_tool():
    """调用不存在的工具 → 计为无效。"""
    calls = [
        ToolCallRecord(tool_name="search_statutes"),  # 有效
        ToolCallRecord(tool_name="nonexistent_tool"),  # 无效
    ]
    assert _count_invalid_tool_calls(calls) == 1


def test_count_invalid_tool_calls_failed():
    """参数错误（success=False）→ 计为无效。"""
    calls = [
        ToolCallRecord(tool_name="search_statutes", success=False, error_message="参数错误"),
    ]
    assert _count_invalid_tool_calls(calls) == 1


def test_count_invalid_tool_calls_all_valid():
    """全部有效且成功 → 0 无效。"""
    calls = [
        ToolCallRecord(tool_name="search_statutes"),
        ToolCallRecord(tool_name="get_statute_article"),
    ]
    assert _count_invalid_tool_calls(calls) == 0


def test_known_tools_set():
    """KNOWN_TOOLS 应包含 Task 15 的标准工具。"""
    assert "search_statutes" in KNOWN_TOOLS
    assert "get_statute_article" in KNOWN_TOOLS
    assert "search_cases" in KNOWN_TOOLS
    assert "render_docx" in KNOWN_TOOLS
    assert "calculate_legal_deadline" in KNOWN_TOOLS


# ---------------------------------------------------------------------------
# 4. 循环失控检测
# ---------------------------------------------------------------------------
def test_is_runaway_iteration_exceeded():
    """iteration 超过 MAX_LEGAL_REASONER_ITERATIONS → 失控。"""
    state = _make_state(iteration=99)
    is_runaway, iteration, reretrieval = _is_runaway(state)
    assert is_runaway is True
    assert iteration == 99


def test_is_runaway_reretrieval_exceeded():
    """reretrieval_count >= 2 → 失控。"""
    state = _make_state(reretrieval_count=2)
    is_runaway, _, reretrieval = _is_runaway(state)
    assert is_runaway is True
    assert reretrieval == 2


def test_is_runaway_normal():
    """正常迭代 → 不失控。"""
    state = _make_state(iteration=1, reretrieval_count=0)
    is_runaway, _, _ = _is_runaway(state)
    assert is_runaway is False


def test_is_runaway_none_citation_audit():
    """citation_audit 为 None → reretrieval_count=0，不失控。"""
    state = _make_state(iteration=0, reretrieval_count=0)
    is_runaway, _, _ = _is_runaway(state)
    assert is_runaway is False


# ---------------------------------------------------------------------------
# 5. 中断恢复成功率
# ---------------------------------------------------------------------------
def test_evaluate_agent_run_recovered():
    """中断后恢复成功 → recovered=True。"""
    record = _make_record(interrupted=True, recovered=True)
    result = evaluate_agent_run(record)
    assert result.interrupted is True
    assert result.recovered is True


def test_evaluate_agent_run_not_recovered():
    """中断后未恢复 → recovered=False。"""
    record = _make_record(interrupted=True, recovered=False)
    result = evaluate_agent_run(record)
    assert result.interrupted is True
    assert result.recovered is False


def test_recovery_success_rate_batch():
    """批量：2 次中断，1 次恢复 → recovery_success_rate=0.5。"""
    records = [
        _make_record(query_id="q1", interrupted=True, recovered=True),
        _make_record(query_id="q2", interrupted=True, recovered=False),
        _make_record(query_id="q3", interrupted=False),  # 未中断不计入
    ]
    report = evaluate_agent_batch(records)
    assert report.recovery_success_rate == 0.5


def test_recovery_success_rate_no_interrupts():
    """无中断运行 → recovery_success_rate=0.0（无数据）。"""
    records = [_make_record(query_id="q1", interrupted=False)]
    report = evaluate_agent_batch(records)
    assert report.recovery_success_rate == 0.0


# ---------------------------------------------------------------------------
# 6. 应提问而未提问 / 不必要追问
# ---------------------------------------------------------------------------
def test_missed_asking():
    """missing_facts 非空但未提问 → missed_asking=True。"""
    state = _make_state(missing_facts=[{"fact_key": "k1", "question": "q?", "reason": "r"}])
    record = _make_record(state=state, asked_missing_facts=False)
    result = evaluate_agent_run(record)
    assert result.should_ask is True
    assert result.asked is False
    assert result.missed_asking is True
    assert result.unnecessary_asking is False


def test_unnecessary_asking():
    """missing_facts 为空但提问了 → unnecessary_asking=True。"""
    state = _make_state(missing_facts=[])
    record = _make_record(state=state, asked_missing_facts=True)
    result = evaluate_agent_run(record)
    assert result.should_ask is False
    assert result.asked is True
    assert result.missed_asking is False
    assert result.unnecessary_asking is True


def test_correct_asking():
    """missing_facts 非空且提问了 → 正确，无 missed/unnecessary。"""
    state = _make_state(missing_facts=[{"fact_key": "k1", "question": "q?", "reason": "r"}])
    record = _make_record(state=state, asked_missing_facts=True)
    result = evaluate_agent_run(record)
    assert result.missed_asking is False
    assert result.unnecessary_asking is False


def test_missed_asking_rate_batch():
    """批量：2 条应提问，1 条未提问 → missed_asking_rate=0.5。"""
    state_with_missing = _make_state(
        missing_facts=[{"fact_key": "k1", "question": "q?", "reason": "r"}]
    )
    records = [
        _make_record(query_id="q1", state=state_with_missing, asked_missing_facts=True),
        _make_record(query_id="q2", state=state_with_missing, asked_missing_facts=False),
    ]
    report = evaluate_agent_batch(records)
    assert report.missed_asking_rate == 0.5


# ---------------------------------------------------------------------------
# 7. 平均成本和延迟
# ---------------------------------------------------------------------------
def test_cost_and_latency():
    """evaluate_agent_run 应记录 model_call_count / token_count / elapsed_seconds。"""
    record = _make_record(
        model_call_count=5,
        token_count=1200,
        elapsed_seconds=3.5,
    )
    result = evaluate_agent_run(record)
    assert result.model_call_count == 5
    assert result.token_count == 1200
    assert result.elapsed_seconds == 3.5


def test_avg_cost_and_latency_batch():
    """批量：平均成本和延迟应正确计算。"""
    records = [
        _make_record(query_id="q1", model_call_count=4, token_count=1000, elapsed_seconds=2.0),
        _make_record(query_id="q2", model_call_count=6, token_count=2000, elapsed_seconds=4.0),
    ]
    report = evaluate_agent_batch(records)
    assert report.avg_model_call_count == 5.0
    assert report.avg_token_count == 1500.0
    assert report.avg_elapsed_seconds == 3.0


# ---------------------------------------------------------------------------
# 8. 批量评测聚合
# ---------------------------------------------------------------------------
def test_evaluate_agent_batch_aggregation():
    """批量评测聚合：混合正常与异常用例。"""
    records = [
        # 正常用例
        _make_record(
            query_id="q1",
            tool_calls=[ToolCallRecord(tool_name="search_statutes")],
            expected_tool_sequence=["search_statutes"],
            model_call_count=3,
            token_count=500,
            elapsed_seconds=1.0,
        ),
        # 循环失控用例
        _make_record(
            query_id="q2",
            state=_make_state(iteration=99),
            tool_calls=[ToolCallRecord(tool_name="nonexistent_tool")],
            model_call_count=10,
            token_count=2000,
            elapsed_seconds=5.0,
        ),
    ]
    report = evaluate_agent_batch(records)
    assert report.total_runs == 2
    assert len(report.per_run) == 2
    # 循环失控率 = 1/2
    assert report.runaway_rate == 0.5
    # 总无效工具调用 = 1
    assert report.total_invalid_tool_calls == 1
    # 平均无效 = 0.5
    assert report.avg_invalid_tool_calls == 0.5
    # 工具选择正确率仅 q1 有 expected → q1=1.0
    assert report.avg_tool_selection_accuracy == 1.0


def test_evaluate_agent_batch_empty():
    """空批量：返回空报告。"""
    report = evaluate_agent_batch([])
    assert report.total_runs == 0
    assert len(report.per_run) == 0
    assert report.runaway_rate == 0.0


# ---------------------------------------------------------------------------
# 9. 报告序列化
# ---------------------------------------------------------------------------
def test_agent_eval_result_serialization():
    """AgentEvalResult.to_dict() 应返回含全部指标的 dict。"""
    record = _make_record(query_id="ser_001")
    result = evaluate_agent_run(record)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["query_id"] == "ser_001"
    assert "tool_selection_accuracy" in d
    assert "invalid_tool_calls" in d
    assert "is_runaway" in d
    assert "missed_asking" in d


def test_agent_eval_report_serialization():
    """AgentEvalReport.to_dict() 应可 JSON 序列化。"""
    import json

    records = [_make_record(query_id="q1")]
    report = evaluate_agent_batch(records)
    d = report.to_dict()
    assert isinstance(d, dict)
    json_str = json.dumps(d, ensure_ascii=False)
    assert isinstance(json_str, str)
    restored = json.loads(json_str)
    assert restored["total_runs"] == 1


# ---------------------------------------------------------------------------
# 10. LCS 辅助函数
# ---------------------------------------------------------------------------
def test_lcs_length_identical():
    """相同序列 → LCS = 长度。"""
    assert _lcs_length(["a", "b", "c"], ["a", "b", "c"]) == 3


def test_lcs_length_disjoint():
    """完全不同 → LCS = 0。"""
    assert _lcs_length(["a", "b"], ["c", "d"]) == 0


def test_lcs_length_subsequence():
    """子序列 → LCS = 子序列长度。"""
    # ["a", "c"] 是 ["a", "b", "c"] 的子序列
    assert _lcs_length(["a", "c"], ["a", "b", "c"]) == 2


def test_lcs_length_empty():
    """空序列 → LCS = 0。"""
    assert _lcs_length([], ["a"]) == 0
    assert _lcs_length(["a"], []) == 0
    assert _lcs_length([], []) == 0
