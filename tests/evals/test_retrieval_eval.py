"""Task 16: 检索评测脚本与金标集的 pytest 测试。

覆盖：
  1. 金标集可加载且 >= 20 条
  2. 每条金标有 expected_statutes 字段且非空
  3. evaluate_retrieval 能跑通（@pytest.mark.slow，限制前 5 条）
  4. EvalReport 含全部指标字段（Recall@k / MRR / nDCG / 正确法条命中率 /
     正确版本命中率 / 废止法规误召回率）
  5. 废止法规误召回率字段存在且类型正确
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 路径引导：确保 tests/evals 目录在 sys.path，便于 import retrieval_eval
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from retrieval_eval import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    EvalReport,
    QueryResult,
    evaluate_retrieval,
    load_golden_set,
)

from lvyan.config import LAWTEXT_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 金标集加载与条数
# ---------------------------------------------------------------------------
def test_golden_set_loads_and_has_min_20():
    """金标集应能加载且 >= 20 条。"""
    golden = load_golden_set(DEFAULT_GOLDEN_PATH)
    assert isinstance(golden, list)
    assert len(golden) >= 20, f"金标集应 >= 20 条，实际 {len(golden)}"


def test_golden_set_each_has_expected_statutes():
    """每条金标应有非空 expected_statutes 字段。"""
    golden = load_golden_set(DEFAULT_GOLDEN_PATH)
    assert golden, "金标集不应为空"
    for item in golden:
        assert "id" in item, f"缺少 id 字段: {item}"
        assert "query" in item, f"缺少 query 字段: {item.get('id')}"
        assert "category" in item, f"缺少 category 字段: {item.get('id')}"
        assert "expected_statutes" in item, f"缺少 expected_statutes 字段: {item.get('id')}"
        expected = item["expected_statutes"]
        assert isinstance(expected, list) and expected, (
            f"expected_statutes 应为非空 list: {item.get('id')}"
        )
        for exp in expected:
            assert "title" in exp, f"expected_statutes 项缺少 title: {item.get('id')}"
            assert "article_keywords" in exp, (
                f"expected_statutes 项缺少 article_keywords: {item.get('id')}"
            )


def test_golden_set_covers_all_categories():
    """金标集应覆盖任务要求的 7 个类别。"""
    golden = load_golden_set(DEFAULT_GOLDEN_PATH)
    categories = {item["category"] for item in golden}
    required = {
        "劳动争议", "合同纠纷", "侵权纠纷", "婚姻家庭",
        "租赁纠纷", "消费者权益", "个人信息保护",
    }
    missing = required - categories
    assert not missing, f"金标集缺少类别: {missing}"


# ---------------------------------------------------------------------------
# 2. EvalReport 数据模型字段完整性
# ---------------------------------------------------------------------------
def test_eval_report_has_all_metric_fields():
    """EvalReport 应含全部指标字段（含废止法规误召回率）。"""
    report = EvalReport()
    required_fields = {
        "total_queries", "avg_recall_at_k", "avg_mrr", "avg_ndcg_at_k",
        "statute_hit_rate", "version_hit_rate", "repealed_recall_rate",
        "per_query", "top_k", "label",
    }
    actual_fields = set(report.__dict__.keys())
    missing = required_fields - actual_fields
    assert not missing, f"EvalReport 缺少字段: {missing}"


def test_eval_report_repealed_recall_rate_field_exists():
    """废止法规误召回率字段应存在且为 float。"""
    report = EvalReport()
    assert hasattr(report, "repealed_recall_rate"), "EvalReport 应有 repealed_recall_rate 字段"
    assert isinstance(report.repealed_recall_rate, float)


def test_query_result_has_expected_fields():
    """QueryResult 应含 spec 要求的字段。"""
    qr = QueryResult(
        query_id="test_001", query="测试", category="测试",
        hit=True, hit_rank=1,
    )
    required_fields = {
        "query_id", "query", "hit", "hit_rank",
        "recalled_titles", "expected_titles", "repealed_in_results",
        "recalled_statuses", "matched_expected", "recall_at_k",
        "reciprocal_rank", "ndcg_at_k", "category",
    }
    actual_fields = set(qr.__dict__.keys())
    missing = required_fields - actual_fields
    assert not missing, f"QueryResult 缺少字段: {missing}"


# ---------------------------------------------------------------------------
# 3. evaluate_retrieval 端到端跑通（slow，仅前 5 条）
# ---------------------------------------------------------------------------
# 官方法律库不存在时跳过端到端评测（与 test_version_aware.py 一致）
_DB_SKIP = pytest.mark.skipif(
    not LAWTEXT_DIR.is_dir(),
    reason=f"官方法律库目录不存在：{LAWTEXT_DIR}",
)


@pytest.mark.slow
@_DB_SKIP
def test_evaluate_retrieval_runs_with_limit_5():
    """evaluate_retrieval 应能跑通（限制前 5 条以加速）。

    标记为 slow，可用 ``pytest -m 'not slow'`` 跳过。
    """
    report = evaluate_retrieval(golden_path=DEFAULT_GOLDEN_PATH, top_k=10, limit=5)
    assert isinstance(report, EvalReport)
    assert report.total_queries == 5
    assert len(report.per_query) == 5

    # 全局指标应为合法数值
    assert 0.0 <= report.avg_recall_at_k <= 1.0
    assert 0.0 <= report.avg_mrr <= 1.0
    assert 0.0 <= report.avg_ndcg_at_k <= 1.0
    assert 0.0 <= report.statute_hit_rate <= 1.0
    assert 0.0 <= report.version_hit_rate <= 1.0
    assert 0.0 <= report.repealed_recall_rate <= 1.0

    # per_query 每条字段完整
    for qr in report.per_query:
        assert qr.query_id
        assert qr.query
        assert isinstance(qr.hit, bool)
        assert isinstance(qr.recalled_titles, list)
        assert isinstance(qr.expected_titles, list)
        assert isinstance(qr.repealed_in_results, list)


@pytest.mark.slow
@_DB_SKIP
def test_evaluate_retrieval_recall_gap_regression():
    """召回漏洞回归：隐私 query (privacy_001) 至少应命中一条 expected statute。

    与 ``test_version_aware.py::test_recall_gap_regression_personal_info`` 对齐：
    验证 BM25 取消标题预筛后，含「个人信息 / 隐私」的条文仍能被召回。
    privacy_001 的 expected_statutes 含个人信息保护法与民法典（隐私权条款），
    命中任一即视为召回漏洞已修复。
    """
    report = evaluate_retrieval(golden_path=DEFAULT_GOLDEN_PATH, top_k=10, limit=20)
    privacy_q = next(
        (q for q in report.per_query if q.query_id == "privacy_001"), None
    )
    if privacy_q is None:
        pytest.skip("金标集中无 privacy_001")
    # 至少命中一条 expected statute（个人信息保护法 或 民法典隐私权条款）
    assert privacy_q.hit, (
        f"召回漏洞回归失败：privacy_001 应至少命中一条 expected statute，"
        f"recalled_titles={privacy_q.recalled_titles[:5]}"
    )


# ---------------------------------------------------------------------------
# 4. 报告可序列化为 dict
# ---------------------------------------------------------------------------
def test_eval_report_to_dict():
    """EvalReport.to_dict() 应返回含全部指标的 dict。"""
    report = EvalReport(total_queries=3, avg_recall_at_k=0.5, avg_mrr=0.3)
    d = report.to_dict()
    assert isinstance(d, dict)
    assert d["total_queries"] == 3
    assert "repealed_recall_rate" in d
    assert "version_hit_rate" in d
    assert "statute_hit_rate" in d
    assert "avg_ndcg_at_k" in d
