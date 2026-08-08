"""CI 金标集回归评测入口（Task 17.3）。

读取 ``tests/evals/golden_set.json``，对每条含 ``answer_golden`` 的用例构造 mock
state 并运行回答评测，输出 JSON 报告 + 控制台汇总，并按阈值判定 CI 通过/失败。

默认阈值（SubTask 17.3）：
- 法条引用准确率 >= 0.9
- 虚构法条率 <= 0.05
- 过度确定性比例 <= 0.1

低于任一阈值时退出码非 0（CI 失败）。

CLI 用法
--------
    python tests/evals/run_regression.py
    python tests/evals/run_regression.py --limit 5
    python tests/evals/run_regression.py --json report.json
    python tests/evals/run_regression.py --threshold statute_accuracy=0.95,fabrication_rate=0.03
    python tests/evals/run_regression.py --degrade  # 生成低质量 mock 验证阈值触发
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径引导
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]  # AGENT/
_SRC_DIR = _AGENT_DIR / "src"
_EVALS_DIR = _THIS_FILE.parent  # tests/evals/
for _p in (str(_SRC_DIR), str(_EVALS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from answer_eval import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    TENDENCY_CN_TO_ENUM,
    AnswerEvalReport,
    evaluate_answer_batch,
)

# ---------------------------------------------------------------------------
# 默认阈值
# ---------------------------------------------------------------------------
# 「越高越好」的指标：metric >= threshold
_MIN_METRICS: set[str] = {"statute_accuracy"}
# 「越低越好」的指标：metric <= threshold
_MAX_METRICS: set[str] = {"fabrication_rate", "overconfidence"}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "statute_accuracy": 0.9,
    "fabrication_rate": 0.05,
    "overconfidence": 0.1,
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class RegressionReport(BaseModel):
    """CI 回归评测报告。"""

    passed: bool = False
    total_queries: int = 0
    evaluated_queries: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    threshold_violations: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------------------------------------------------------------------
# mock state 生成
# ---------------------------------------------------------------------------
def _build_mock_state(
    golden_item: dict[str, Any],
    degrade: bool = False,
) -> dict[str, Any]:
    """基于金标用例构造 mock state（用于 CI 回归）。

    Args:
        golden_item: 金标集单条用例 dict。
        degrade: 为 True 时生成低质量 mock（虚构法条 + 过度确定），
            用于验证阈值能正确触发失败。

    Returns:
        dict 形式的 mock state，含 ``reasoning_result`` / ``statutes`` /
        ``cases`` / ``evidence_requirements``。
    """
    expected_statutes = golden_item.get("expected_statutes", []) or []
    answer_golden = golden_item.get("answer_golden", {}) or {}
    category = golden_item.get("category", "")

    # --- 构造 statutes（source_id="" 避免查库，status=effective） ---
    statutes: list[dict[str, Any]] = []
    for exp in expected_statutes:
        title = exp.get("title", "")
        keywords = exp.get("article_keywords", []) or []
        article_text = "、".join(keywords) + "等条款规定内容，适用于本案情形。"
        statutes.append(
            {
                "source_id": "",  # 空字符串避免触发 verify_statute_status 查库
                "title": title,
                "article_number": "第四十七条",
                "article_text": article_text,
                "authority_level": "法律",
                "status": "effective",
                "jurisdiction": "中国大陆",
                "retrieved_at": datetime(2026, 7, 24, 12, 0, 0).isoformat(),
            }
        )

    # --- 构造 reasoning_result ---
    tendency_cn = answer_golden.get("ruling_tendency", "较有利")
    tendency_enum = TENDENCY_CN_TO_ENUM.get(tendency_cn, "somewhat_favorable")

    disputed_issues = answer_golden.get("disputed_issues", []) or []
    defendant_args = answer_golden.get("defendant_arguments", []) or []

    # 引用第一条 expected statute（确保法条真实存在）
    key_factors: list[str] = []
    if statutes:
        first = statutes[0]
        title = first["title"]
        article_text = first["article_text"]
        key_factors.append(f"依据《{title}》第四十七条关于{article_text}的规定")

    if degrade:
        # 低质量 mock：虚构法条 + 过度确定
        key_factors.append("依据《中华人民共和国民法典》第九千九百九十九条虚构条款")
        tendency_enum = "favorable"  # 过度乐观
        # 争议焦点/反方论点留空，降低覆盖率
        disputed_issues_mock: list[str] = []
        defendant_args_mock: list[str] = []
    else:
        disputed_issues_mock = list(disputed_issues)
        defendant_args_mock = list(defendant_args)

    reasoning_result: dict[str, Any] = {
        "legal_relationship": category,
        "elements": ["要件一", "要件二"],
        "disputed_focus": disputed_issues_mock,
        "plaintiff_arguments": ["原告主张其合法权益受到侵害"],
        "defendant_arguments": defendant_args_mock,
        "evidence_mapping": [],
        "judicial_tendency": tendency_enum,
        "evidence_confidence": "medium",
        "key_factors": key_factors,
    }

    # --- 构造 evidence_requirements ---
    evidence_gaps = answer_golden.get("evidence_gaps", []) or []
    evidence_requirements: list[dict[str, Any]] = []
    for i, gap in enumerate(evidence_gaps):
        evidence_requirements.append(
            {
                "requirement_id": f"req_{i}",
                "fact_to_prove": gap,
                "evidence_types": [gap],
                "current_status": "missing" if not degrade else "met",
                "gap_description": gap if not degrade else None,
            }
        )

    # --- 构造 cases（空，避免案号虚构） ---
    cases: list[dict[str, Any]] = []

    return {
        "reasoning_result": reasoning_result,
        "statutes": statutes,
        "cases": cases,
        "evidence_requirements": evidence_requirements,
        "iteration": 0,
        "citation_audit": None,
        "missing_facts": [],
    }


# ---------------------------------------------------------------------------
# 阈值解析与判定
# ---------------------------------------------------------------------------
def parse_thresholds(threshold_str: str | None) -> dict[str, float]:
    """解析 ``--threshold`` 参数为阈值 dict。

    格式：``statute_accuracy=0.95,fabrication_rate=0.03,overconfidence=0.05``
    未指定的 key 保留默认值。
    """
    thresholds = dict(DEFAULT_THRESHOLDS)
    if not threshold_str:
        return thresholds
    for pair in threshold_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"阈值格式错误（应为 key=value）: {pair}")
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k not in DEFAULT_THRESHOLDS:
            raise ValueError(f"未知阈值项 '{k}'，支持的阈值: {sorted(DEFAULT_THRESHOLDS.keys())}")
        thresholds[k] = float(v)
    return thresholds


def _check_thresholds(report: AnswerEvalReport, thresholds: dict[str, float]) -> list[str]:
    """检查回答评测报告是否满足阈值，返回违反项描述列表。"""
    violations: list[str] = []
    metric_map = {
        "statute_accuracy": report.avg_statute_accuracy,
        "fabrication_rate": report.avg_fabrication_rate,
        "overconfidence": report.overconfidence_ratio,
    }
    for key, threshold in thresholds.items():
        value = metric_map.get(key)
        if value is None:
            continue
        if key in _MIN_METRICS:
            if value < threshold:
                violations.append(f"{key}={value:.4f} 低于阈值 {threshold}（要求 >= {threshold}）")
        elif key in _MAX_METRICS:
            if value > threshold:
                violations.append(f"{key}={value:.4f} 超过阈值 {threshold}（要求 <= {threshold}）")
    return violations


# ---------------------------------------------------------------------------
# 回归评测主入口
# ---------------------------------------------------------------------------
def run_regression(
    golden_path: str | Path | None = None,
    limit: int | None = None,
    thresholds: dict[str, float] | None = None,
    degrade: bool = False,
) -> RegressionReport:
    """运行金标集回归评测。

    Args:
        golden_path: 金标集路径；None 用默认。
        limit: 仅评测前 N 条用例；None 评测全量。
        thresholds: 阈值 dict；None 用默认。
        degrade: 为 True 时生成低质量 mock，验证阈值触发失败。

    Returns:
        RegressionReport：含 passed / metrics / threshold_violations。
    """
    path = Path(golden_path) if golden_path else DEFAULT_GOLDEN_PATH
    with open(path, "r", encoding="utf-8") as f:
        golden = json.load(f)
    if limit is not None and limit > 0:
        golden = golden[:limit]

    if thresholds is None:
        thresholds = dict(DEFAULT_THRESHOLDS)

    # 仅对有 answer_golden 的用例构造 mock state 并评测
    items: list[dict[str, Any]] = []
    for item in golden:
        state = _build_mock_state(item, degrade=degrade)
        items.append(
            {
                "state": state,
                "answer_golden": item.get("answer_golden"),
                "query_id": item.get("id", ""),
                "query": item.get("query", ""),
                "category": item.get("category", ""),
            }
        )

    answer_report = evaluate_answer_batch(items)
    violations = _check_thresholds(answer_report, thresholds)

    regression = RegressionReport(
        passed=len(violations) == 0,
        total_queries=len(golden),
        evaluated_queries=answer_report.evaluated_queries,
        thresholds=dict(thresholds),
        metrics=answer_report.to_dict(),
        threshold_violations=violations,
    )
    return regression


# ---------------------------------------------------------------------------
# 控制台汇总打印
# ---------------------------------------------------------------------------
def _format_regression_report(report: RegressionReport) -> str:
    """格式化回归报告为可读多行字符串。"""
    lines: list[str] = []
    sep = "=" * 70
    status = "PASS" if report.passed else "FAIL"
    lines.append(sep)
    lines.append(
        f"  CI 回归评测报告  [{status}]  "
        f"(total={report.total_queries}, evaluated={report.evaluated_queries})"
    )
    lines.append(sep)
    metrics = report.metrics
    lines.append(
        f"  法条引用准确率       : {metrics.get('avg_statute_accuracy', 0):.4f}"
        f"  (阈值 >= {report.thresholds.get('statute_accuracy', 0.9)})"
    )
    lines.append(
        f"  虚构法条率           : {metrics.get('avg_fabrication_rate', 0):.4f}"
        f"  (阈值 <= {report.thresholds.get('fabrication_rate', 0.05)})"
    )
    lines.append(
        f"  虚构案号率           : {metrics.get('avg_case_number_fabrication_rate', 0):.4f}"
    )
    lines.append(f"  争议焦点覆盖率       : {metrics.get('avg_disputed_issue_coverage', 0):.4f}")
    lines.append(f"  证据缺口召回率       : {metrics.get('avg_evidence_gap_recall', 0):.4f}")
    lines.append(
        f"  反方论点覆盖率       : {metrics.get('avg_defendant_argument_coverage', 0):.4f}"
    )
    lines.append(
        f"  过度确定性比例       : {metrics.get('overconfidence_ratio', 0):.4f}"
        f"  (阈值 <= {report.thresholds.get('overconfidence', 0.1)})"
    )
    if report.threshold_violations:
        lines.append("-" * 70)
        lines.append("  阈值违反项：")
        for v in report.threshold_violations:
            lines.append(f"    - {v}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="法律问答金标集 CI 回归评测（Task 17.3）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tests/evals/run_regression.py\n"
            "  python tests/evals/run_regression.py --limit 5\n"
            "  python tests/evals/run_regression.py --json report.json\n"
            "  python tests/evals/run_regression.py "
            "--threshold statute_accuracy=0.95,fabrication_rate=0.03\n"
            "  python tests/evals/run_regression.py --degrade  # 验证阈值触发\n"
        ),
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN_PATH),
        help=f"金标集路径（默认 {DEFAULT_GOLDEN_PATH}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅评测前 N 条用例",
    )
    parser.add_argument(
        "--threshold",
        default=None,
        help=(
            "覆盖默认阈值，格式 key=value,key=value。"
            "支持的 key: statute_accuracy, fabrication_rate, overconfidence"
        ),
    )
    parser.add_argument(
        "--json",
        default=None,
        help="把回归报告以 JSON 写入指定文件",
    )
    parser.add_argument(
        "--degrade",
        action="store_true",
        help="生成低质量 mock state，验证阈值能正确触发失败（测试用）",
    )
    args = parser.parse_args()

    try:
        thresholds = parse_thresholds(args.threshold)
    except ValueError as exc:
        print(f"[Regression] 阈值解析失败: {exc}", file=sys.stderr)
        return 2

    print(f"[Regression] 金标集: {args.golden}", file=sys.stderr)
    print(f"[Regression] 阈值: {thresholds}", file=sys.stderr)
    print(f"[Regression] degrade={args.degrade}", file=sys.stderr)

    report = run_regression(
        golden_path=args.golden,
        limit=args.limit,
        thresholds=thresholds,
        degrade=args.degrade,
    )

    print(_format_regression_report(report))

    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[Regression] JSON 报告已写入: {args.json}", file=sys.stderr)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
