"""Task 17.1: 回答评测脚本的 pytest 测试。

覆盖：
  1. AnswerEvalResult / AnswerEvalReport 数据模型字段完整性
  2. evaluate_answer 指标计算正确性（完美 / 虚构法条 / 案号虚构 / 过度确定）
  3. evaluate_answer 无 answer_golden 时仅计算法条/案号指标
  4. evaluate_answer_batch 聚合正确性
  5. 报告序列化
  6. 辅助函数 _is_covered / _extract_case_numbers
  7. run_regression 集成（默认通过 / degrade 失败 / 阈值覆盖）
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

# 路径引导：把 tests/evals 加入 sys.path，便于 import answer_eval / run_regression
_THIS_DIR = Path(__file__).resolve().parent  # tests/unit/
_EVALS_DIR = _THIS_DIR.parent / "evals"  # tests/evals/
if str(_EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALS_DIR))

from answer_eval import (  # noqa: E402
    AnswerEvalReport,
    AnswerEvalResult,
    TENDENCY_CN_TO_ENUM,
    _compute_coverage,
    _extract_case_numbers,
    _is_covered,
    _is_overconfident,
    evaluate_answer,
    evaluate_answer_batch,
)
from run_regression import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    RegressionReport,
    parse_thresholds,
    run_regression,
)


# ---------------------------------------------------------------------------
# 辅助：构造 mock state
# ---------------------------------------------------------------------------
def _make_statute(
    title: str = "中华人民共和国劳动合同法",
    article_number: str = "第四十七条",
    article_text: str = (
        "经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资。"
    ),
    status: str = "effective",
    source_id: str = "",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": title,
        "article_number": article_number,
        "article_text": article_text,
        "authority_level": "法律",
        "status": status,
        "jurisdiction": "中国大陆",
        "retrieved_at": datetime(2026, 7, 24, 12, 0, 0).isoformat(),
    }


def _make_reasoning_result(
    key_factors: list[str] | None = None,
    disputed_focus: list[str] | None = None,
    defendant_arguments: list[str] | None = None,
    judicial_tendency: str = "somewhat_favorable",
) -> dict[str, Any]:
    if key_factors is None:
        key_factors = [
            "依据《中华人民共和国劳动合同法》第四十七条，"
            "经济补偿按劳动者在本单位工作的年限计算"
        ]
    return {
        "legal_relationship": "劳动争议",
        "elements": ["劳动关系成立", "违法解除"],
        "disputed_focus": disputed_focus
        if disputed_focus is not None
        else ["是否构成违法解除", "经济补偿计算基数"],
        "plaintiff_arguments": ["原告主张违法解除赔偿"],
        "defendant_arguments": defendant_arguments
        if defendant_arguments is not None
        else ["原告严重违反规章制度"],
        "evidence_mapping": [],
        "judicial_tendency": judicial_tendency,
        "evidence_confidence": "medium",
        "key_factors": key_factors,
    }


def _make_state(
    reasoning_result: dict[str, Any] | None = None,
    statutes: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    evidence_requirements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "reasoning_result": reasoning_result or _make_reasoning_result(),
        "statutes": statutes if statutes is not None else [_make_statute()],
        "cases": cases or [],
        "evidence_requirements": evidence_requirements or [],
    }


def _make_answer_golden(
    disputed_issues: list[str] | None = None,
    evidence_gaps: list[str] | None = None,
    defendant_arguments: list[str] | None = None,
    ruling_tendency: str = "较有利",
) -> dict[str, Any]:
    return {
        "disputed_issues": disputed_issues
        or ["是否构成违法解除", "经济补偿计算基数"],
        "evidence_gaps": evidence_gaps or ["工资流水", "解除通知书"],
        "defendant_arguments": defendant_arguments
        or ["原告严重违反规章制度"],
        "ruling_tendency": ruling_tendency,
    }


def _make_evidence_requirements(
    gaps: list[str] | None = None,
) -> list[dict[str, Any]]:
    gaps = gaps or ["工资流水", "解除通知书"]
    return [
        {
            "requirement_id": f"req_{i}",
            "fact_to_prove": gap,
            "evidence_types": [gap],
            "current_status": "missing",
            "gap_description": gap,
        }
        for i, gap in enumerate(gaps)
    ]


# ---------------------------------------------------------------------------
# 1. 数据模型字段完整性
# ---------------------------------------------------------------------------
def test_answer_eval_result_has_all_metric_fields():
    """AnswerEvalResult 应含全部回答评测指标字段。"""
    result = AnswerEvalResult(query_id="test_001")
    required_fields = {
        "query_id",
        "total_citations",
        "valid_citations",
        "fabricated_citations",
        "statute_accuracy",
        "fabrication_rate",
        "total_case_numbers",
        "fabricated_case_numbers",
        "case_number_fabrication_rate",
        "disputed_issue_coverage",
        "evidence_gap_recall",
        "defendant_argument_coverage",
        "is_overconfident",
        "has_answer_golden",
    }
    actual_fields = set(AnswerEvalResult.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, f"AnswerEvalResult 缺少字段: {missing}"


def test_answer_eval_report_has_all_metric_fields():
    """AnswerEvalReport 应含全部聚合指标字段。"""
    report = AnswerEvalReport()
    required_fields = {
        "total_queries",
        "evaluated_queries",
        "avg_statute_accuracy",
        "avg_fabrication_rate",
        "avg_case_number_fabrication_rate",
        "avg_disputed_issue_coverage",
        "avg_evidence_gap_recall",
        "avg_defendant_argument_coverage",
        "overconfidence_ratio",
        "per_query",
    }
    actual_fields = set(AnswerEvalReport.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, f"AnswerEvalReport 缺少字段: {missing}"


# ---------------------------------------------------------------------------
# 2. evaluate_answer 指标计算正确性
# ---------------------------------------------------------------------------
def test_evaluate_answer_perfect():
    """高质量 mock state：所有指标应为 1.0，无虚构、无过度确定。"""
    state = _make_state(
        evidence_requirements=_make_evidence_requirements(),
    )
    golden = _make_answer_golden()
    result = evaluate_answer(
        state=state,
        answer_golden=golden,
        query_id="labor_001",
    )
    assert result.statute_accuracy == 1.0
    assert result.fabrication_rate == 0.0
    assert result.fabricated_citations == 0
    assert result.disputed_issue_coverage == 1.0
    assert result.evidence_gap_recall == 1.0
    assert result.defendant_argument_coverage == 1.0
    assert result.is_overconfident is False
    assert result.has_answer_golden is True


def test_evaluate_answer_fabricated_citation():
    """虚构法条：引用不存在的条文 → fabrication_rate > 0，accuracy < 1.0。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国劳动合同法》第四十七条，经济补偿按年限计算",
            "依据《中华人民共和国民法典》第九千九百九十九条虚构条款",
        ]
    )
    state = _make_state(reasoning_result=rr)
    result = evaluate_answer(state=state, answer_golden=None, query_id="test_fab")
    assert result.total_citations >= 2
    assert result.fabricated_citations >= 1
    assert result.fabrication_rate > 0.0
    assert result.statute_accuracy < 1.0


def test_evaluate_answer_case_number_fabrication():
    """虚构案号：引用案号不在 state.cases 中 → fabrication_rate=1.0。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国劳动合同法》第四十七条计算经济补偿，"
            "参照(2023)京01民初123号案判决"
        ]
    )
    state = _make_state(reasoning_result=rr, cases=[])  # cases 为空
    result = evaluate_answer(state=state, answer_golden=None, query_id="test_cn")
    assert result.total_case_numbers >= 1
    assert result.fabricated_case_numbers >= 1
    assert result.case_number_fabrication_rate == 1.0


def test_evaluate_answer_case_number_real():
    """真实案号：引用案号在 state.cases 中 → fabrication_rate=0.0。"""
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国劳动合同法》第四十七条计算经济补偿，"
            "参照(2023)京01民初123号案判决"
        ]
    )
    state = _make_state(
        reasoning_result=rr,
        cases=[{"case_id": "c1", "case_number": "(2023)京01民初123号"}],
    )
    result = evaluate_answer(state=state, answer_golden=None, query_id="test_cn_real")
    assert result.total_case_numbers >= 1
    assert result.fabricated_case_numbers == 0
    assert result.case_number_fabrication_rate == 0.0


def test_evaluate_answer_overconfident():
    """过度确定：标注 favorable 但金标为胶着 → is_overconfident=True。"""
    rr = _make_reasoning_result(judicial_tendency="favorable")
    state = _make_state(reasoning_result=rr)
    golden = _make_answer_golden(ruling_tendency="胶着")
    result = evaluate_answer(state=state, answer_golden=golden, query_id="test_oc")
    assert result.is_overconfident is True


def test_evaluate_answer_not_overconfident():
    """非过度确定：标注与金标一致 → is_overconfident=False。"""
    rr = _make_reasoning_result(judicial_tendency="somewhat_favorable")
    state = _make_state(reasoning_result=rr)
    golden = _make_answer_golden(ruling_tendency="较有利")
    result = evaluate_answer(state=state, answer_golden=golden, query_id="test_noc")
    assert result.is_overconfident is False


def test_evaluate_answer_no_golden():
    """无 answer_golden：仅计算法条/案号指标，覆盖率为默认值。"""
    state = _make_state()
    result = evaluate_answer(state=state, answer_golden=None, query_id="test_ng")
    assert result.has_answer_golden is False
    # 覆盖率指标应为默认 0.0
    assert result.disputed_issue_coverage == 0.0
    assert result.evidence_gap_recall == 0.0
    assert result.defendant_argument_coverage == 0.0
    # 法条指标仍应计算
    assert result.statute_accuracy == 1.0


def test_evaluate_answer_partial_coverage():
    """部分覆盖：金标 2 条争议焦点，实际只覆盖 1 条 → coverage=0.5。"""
    rr = _make_reasoning_result(disputed_focus=["是否构成违法解除"])  # 仅 1 条
    state = _make_state(reasoning_result=rr)
    golden = _make_answer_golden(
        disputed_issues=["是否构成违法解除", "经济补偿计算基数"]
    )
    result = evaluate_answer(state=state, answer_golden=golden, query_id="test_pc")
    assert result.disputed_issue_coverage == 0.5


# ---------------------------------------------------------------------------
# 3. evaluate_answer_batch 聚合
# ---------------------------------------------------------------------------
def test_evaluate_answer_batch_aggregation():
    """批量评测聚合：混合完美与过度确定用例。"""
    # 用例 1：完美
    state1 = _make_state(evidence_requirements=_make_evidence_requirements())
    golden1 = _make_answer_golden(ruling_tendency="较有利")
    # 用例 2：过度确定
    state2 = _make_state(
        reasoning_result=_make_reasoning_result(judicial_tendency="favorable"),
        evidence_requirements=_make_evidence_requirements(),
    )
    golden2 = _make_answer_golden(ruling_tendency="胶着")

    items = [
        {"state": state1, "answer_golden": golden1, "query_id": "q1"},
        {"state": state2, "answer_golden": golden2, "query_id": "q2"},
    ]
    report = evaluate_answer_batch(items)
    assert report.total_queries == 2
    assert report.evaluated_queries == 2
    assert len(report.per_query) == 2
    # 过度确定比例 = 1/2 = 0.5
    assert report.overconfidence_ratio == 0.5
    # 法条准确率应为 1.0（两条都引用真实法条）
    assert report.avg_statute_accuracy == 1.0
    # 争议焦点覆盖率应为 1.0（两条都完整覆盖）
    assert report.avg_disputed_issue_coverage == 1.0


def test_evaluate_answer_batch_empty():
    """空批量：返回空报告，所有指标为 0。"""
    report = evaluate_answer_batch([])
    assert report.total_queries == 0
    assert report.evaluated_queries == 0
    assert len(report.per_query) == 0


# ---------------------------------------------------------------------------
# 4. 报告序列化
# ---------------------------------------------------------------------------
def test_answer_eval_result_serialization():
    """AnswerEvalResult.to_dict() 应返回含全部指标的 dict。"""
    result = evaluate_answer(
        state=_make_state(),
        answer_golden=_make_answer_golden(),
        query_id="ser_001",
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["query_id"] == "ser_001"
    assert "statute_accuracy" in d
    assert "fabrication_rate" in d
    assert "overconfidence" not in d  # 单条用 is_overconfident
    assert "is_overconfident" in d


def test_answer_eval_report_serialization():
    """AnswerEvalReport.to_dict() 应可 JSON 序列化。"""
    import json

    report = evaluate_answer_batch(
        [
            {
                "state": _make_state(),
                "answer_golden": _make_answer_golden(),
                "query_id": "q1",
            }
        ]
    )
    d = report.to_dict()
    assert isinstance(d, dict)
    # 应可 JSON 序列化
    json_str = json.dumps(d, ensure_ascii=False)
    assert isinstance(json_str, str)
    restored = json.loads(json_str)
    assert restored["total_queries"] == 1


# ---------------------------------------------------------------------------
# 5. 辅助函数
# ---------------------------------------------------------------------------
def test_is_covered_substring():
    """子串匹配：金标是实际的子串 → 覆盖。"""
    assert _is_covered("经济补偿", ["是否构成经济补偿违法解除"]) is True
    assert _is_covered("经济补偿", ["完全不同内容"]) is False


def test_is_covered_empty():
    """空金标或空实际 → 不覆盖。"""
    assert _is_covered("", ["abc"]) is False
    assert _is_covered("abc", []) is False
    assert _is_covered("abc", [""]) is False


def test_extract_case_numbers():
    """案号提取：应匹配标准中国法院案号格式。"""
    text = "参照(2023)京01民初123号判决及(2022)沪民终456号裁定"
    numbers = _extract_case_numbers(text)
    assert len(numbers) == 2
    assert "(2023)京01民初123号" in numbers
    assert "(2022)沪民终456号" in numbers


def test_extract_case_numbers_empty():
    """无案号文本 → 返回空列表。"""
    assert _extract_case_numbers("没有案号的文本") == []


def test_compute_coverage_empty_golden():
    """金标为空 → 覆盖率 1.0（不计入扣分）。"""
    assert _compute_coverage([], ["实际"]) == 1.0


def test_is_overconfident_logic():
    """过度确定判定逻辑。"""
    # favorable + 胶着 → 过度确定
    assert _is_overconfident("favorable", "胶着") is True
    # somewhat_favorable + 较不利 → 过度确定
    assert _is_overconfident("somewhat_favorable", "较不利") is True
    # favorable + 较有利 → 非过度确定（金标也乐观）
    assert _is_overconfident("favorable", "较有利") is False
    # even + 胶着 → 非过度确定（实际不乐观）
    assert _is_overconfident("even", "胶着") is False
    # insufficient + 任何 → 非过度确定
    assert _is_overconfident("insufficient", "胶着") is False


def test_tendency_mapping():
    """中文倾向标签应正确映射到枚举。"""
    assert TENDENCY_CN_TO_ENUM["有利"] == "favorable"
    assert TENDENCY_CN_TO_ENUM["较有利"] == "somewhat_favorable"
    assert TENDENCY_CN_TO_ENUM["胶着"] == "even"
    assert TENDENCY_CN_TO_ENUM["较不利"] == "somewhat_unfavorable"
    assert TENDENCY_CN_TO_ENUM["信息不足"] == "insufficient"


# ---------------------------------------------------------------------------
# 6. run_regression 集成测试
# ---------------------------------------------------------------------------
def test_run_regression_default_passes():
    """默认高质量 mock 应通过 CI 阈值。"""
    report = run_regression(limit=5)
    assert isinstance(report, RegressionReport)
    assert report.passed is True
    assert report.threshold_violations == []
    assert report.evaluated_queries > 0
    assert report.metrics["avg_statute_accuracy"] >= DEFAULT_THRESHOLDS["statute_accuracy"]
    assert report.metrics["avg_fabrication_rate"] <= DEFAULT_THRESHOLDS["fabrication_rate"]
    assert report.metrics["overconfidence_ratio"] <= DEFAULT_THRESHOLDS["overconfidence"]


def test_run_regression_degrade_fails():
    """degrade 模式生成低质量 mock，应触发阈值失败。"""
    report = run_regression(limit=5, degrade=True)
    assert report.passed is False
    assert len(report.threshold_violations) > 0
    # 虚构法条率应超过阈值
    assert report.metrics["avg_fabrication_rate"] > DEFAULT_THRESHOLDS["fabrication_rate"]


def test_run_regression_threshold_override():
    """--threshold 参数覆盖默认阈值：提高准确率阈值应导致失败。"""
    # 默认 mock accuracy=1.0，提高阈值到 1.01 仍应通过（1.0 < 1.01 失败）
    thresholds = {"statute_accuracy": 1.01, "fabrication_rate": 0.05, "overconfidence": 0.1}
    report = run_regression(limit=5, thresholds=thresholds)
    assert report.passed is False
    assert any("statute_accuracy" in v for v in report.threshold_violations)


def test_parse_thresholds():
    """阈值解析：key=value 格式。"""
    # 默认
    t = parse_thresholds(None)
    assert t == DEFAULT_THRESHOLDS
    # 覆盖
    t = parse_thresholds("statute_accuracy=0.95,fabrication_rate=0.03")
    assert t["statute_accuracy"] == 0.95
    assert t["fabrication_rate"] == 0.03
    assert t["overconfidence"] == 0.1  # 未覆盖的保留默认


def test_parse_thresholds_invalid():
    """非法阈值项应抛 ValueError。"""
    with pytest.raises(ValueError, match="未知阈值项"):
        parse_thresholds("unknown_key=0.5")
    with pytest.raises(ValueError, match="阈值格式错误"):
        parse_thresholds("no_equals_sign")


def test_regression_report_serialization():
    """RegressionReport.to_dict() 应含 passed / metrics / threshold_violations。"""
    report = run_regression(limit=5)
    d = report.to_dict()
    assert isinstance(d, dict)
    assert "passed" in d
    assert "metrics" in d
    assert "threshold_violations" in d
    assert "thresholds" in d
    assert d["passed"] is True
