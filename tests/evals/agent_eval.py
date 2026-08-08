"""Agent 评测脚本（Task 17.2）。

对法律 Agent 的运行过程计算行为指标：

- **工具选择正确率**：金标工具调用序列匹配率（LCS / 金标长度）。
- **无效工具调用数**：调用不存在工具或参数错误（``success=False``）的次数。
- **循环失控率**：``iteration > MAX_LEGAL_REASONER_ITERATIONS`` 或
  ``citation_audit.reretrieval_count >= 2`` 的运行比例。
- **中断恢复成功率**：中断后从 checkpoint 恢复并成功完成的运行比例。
- **应提问而未提问比例**：``missing_facts`` 非空但未触发提问的比例。
- **不必要追问比例**：已有充分事实（``missing_facts`` 为空）仍提问的比例。
- **平均成本和延迟**：token 数 + 运行时间（mock 模型时记录调用次数与耗时）。

公开接口
--------
    evaluate_agent_run(record) -> AgentEvalResult
    evaluate_agent_batch(records) -> AgentEvalReport

CLI 用法
--------
    python tests/evals/agent_eval.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径引导：确保以脚本形式运行时也能从 AGENT/src 导入 lvyan 包。
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]  # AGENT/
_SRC_DIR = _AGENT_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from lvyan.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# 已知合法工具集（来自 Task 15 标准工具集）
# ---------------------------------------------------------------------------
KNOWN_TOOLS: set[str] = {
    # statutes
    "search_statutes",
    "get_statute_article",
    "verify_statute_status",
    # cases
    "search_cases",
    "get_case_detail",
    # documents
    "extract_document",
    "analyze_contract_clause",
    # calculators
    "calculate_legal_deadline",
    "calculate_claim_amount",
    "generate_evidence_checklist",
    "build_case_timeline",
    # export
    "render_docx",
}

# 默认阈值常量（可被 settings 覆盖）
_MAX_LEGAL_REASONER_ITERATIONS = settings.max_legal_reasoner_iterations
_RERETRIEVAL_LOOP_THRESHOLD = 2  # citation_verifier 最多重检索 2 次，达到即视为失控


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class ToolCallRecord(BaseModel):
    """单次工具调用记录。"""

    tool_name: str
    success: bool = True
    error_message: str | None = None  # 参数错误等失败原因


class AgentRunRecord(BaseModel):
    """单次 Agent 运行记录（供 Agent 评测）。

    封装 GraphState 之外、仅运行时可知的元数据（中断/恢复、工具调用序列、
    成本与延迟等）。``state`` 字段持有 GraphState / CaseState 或 dict。
    """

    query_id: str
    state: Any = None  # GraphState / CaseState / dict
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    expected_tool_sequence: list[str] = Field(default_factory=list)
    # 运行时元数据
    interrupted: bool = False  # 是否发生过中断
    recovered: bool = False  # 中断后是否成功恢复
    asked_missing_facts: bool = False  # 是否触发了向用户提问
    model_call_count: int = 0
    token_count: int = 0
    elapsed_seconds: float = 0.0


class AgentEvalResult(BaseModel):
    """单条用例的 Agent 评测结果。"""

    query_id: str
    # 工具选择
    tool_selection_accuracy: float = 0.0  # 工具选择正确率
    invalid_tool_calls: int = 0  # 无效工具调用数
    has_expected_sequence: bool = False
    # 循环失控
    is_runaway: bool = False  # 本条是否循环失控
    iteration: int = 0
    reretrieval_count: int = 0
    # 中断恢复
    interrupted: bool = False
    recovered: bool = False
    # 应提问而未提问
    should_ask: bool = False  # missing_facts 非空
    asked: bool = False  # 实际是否提问
    missed_asking: bool = False  # 应提问而未提问
    # 不必要追问
    unnecessary_asking: bool = False  # 已有充分事实仍提问
    # 成本与延迟
    model_call_count: int = 0
    token_count: int = 0
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AgentEvalReport(BaseModel):
    """整批 Agent 评测聚合报告。"""

    total_runs: int = 0
    avg_tool_selection_accuracy: float = 0.0
    total_invalid_tool_calls: int = 0
    avg_invalid_tool_calls: float = 0.0
    runaway_rate: float = 0.0  # 循环失控率
    recovery_success_rate: float = 0.0  # 中断恢复成功率
    missed_asking_rate: float = 0.0  # 应提问而未提问比例
    unnecessary_asking_rate: float = 0.0  # 不必要追问比例
    avg_model_call_count: float = 0.0
    avg_token_count: float = 0.0
    avg_elapsed_seconds: float = 0.0
    per_run: list[AgentEvalResult] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _lcs_length(a: list[str], b: list[str]) -> int:
    """计算两个字符串序列的最长公共子序列长度。"""
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0
    # 滚动数组优化空间
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, prev
    return prev[n]


def _compute_tool_selection_accuracy(actual: list[str], expected: list[str]) -> float:
    """计算工具选择正确率：LCS(actual, expected) / len(expected)。

    ``expected`` 为空时不计入（返回 1.0，表示无约束）。
    """
    if not expected:
        return 1.0
    lcs = _lcs_length(actual, expected)
    return lcs / len(expected)


def _count_invalid_tool_calls(tool_calls: list[ToolCallRecord]) -> int:
    """统计无效工具调用数：工具名不在 KNOWN_TOOLS 中或 success=False。"""
    count = 0
    for tc in tool_calls:
        if tc.tool_name not in KNOWN_TOOLS:
            count += 1
            continue
        if not tc.success:
            count += 1
    return count


def _is_runaway(state: Any) -> tuple[bool, int, int]:
    """判断是否循环失控。

    Returns:
        (is_runaway, iteration, reretrieval_count)
    """
    iteration = int(_get(state, "iteration", 0) or 0)
    citation_audit = _get(state, "citation_audit", None)
    reretrieval_count = int(_get(citation_audit, "reretrieval_count", 0) or 0)

    is_runaway = (
        iteration > _MAX_LEGAL_REASONER_ITERATIONS
        or reretrieval_count >= _RERETRIEVAL_LOOP_THRESHOLD
    )
    return is_runaway, iteration, reretrieval_count


def _check_missing_facts(state: Any) -> list[Any]:
    """读取 state.missing_facts 列表。"""
    return _get(state, "missing_facts", []) or []


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def evaluate_agent_run(record: AgentRunRecord) -> AgentEvalResult:
    """对单次 Agent 运行计算评测指标。

    Args:
        record: ``AgentRunRecord``，含 state 与运行时元数据。

    Returns:
        AgentEvalResult：含全部 Agent 评测指标。
    """
    state = record.state
    actual_tools = [tc.tool_name for tc in record.tool_calls]
    expected_tools = record.expected_tool_sequence

    accuracy = _compute_tool_selection_accuracy(actual_tools, expected_tools)
    invalid_count = _count_invalid_tool_calls(record.tool_calls)

    is_runaway, iteration, reretrieval = _is_runaway(state)

    missing_facts = _check_missing_facts(state)
    should_ask = len(missing_facts) > 0
    asked = record.asked_missing_facts
    missed_asking = should_ask and not asked
    unnecessary_asking = (not should_ask) and asked

    result = AgentEvalResult(
        query_id=record.query_id,
        tool_selection_accuracy=round(accuracy, 4),
        invalid_tool_calls=invalid_count,
        has_expected_sequence=bool(expected_tools),
        is_runaway=is_runaway,
        iteration=iteration,
        reretrieval_count=reretrieval,
        interrupted=record.interrupted,
        recovered=record.recovered,
        should_ask=should_ask,
        asked=asked,
        missed_asking=missed_asking,
        unnecessary_asking=unnecessary_asking,
        model_call_count=record.model_call_count,
        token_count=record.token_count,
        elapsed_seconds=record.elapsed_seconds,
    )
    return result


def evaluate_agent_batch(records: list[AgentRunRecord]) -> AgentEvalReport:
    """对一批 Agent 运行计算聚合评测报告。

    Args:
        records: ``AgentRunRecord`` 列表。

    Returns:
        AgentEvalReport：含全局聚合指标与 per_run 详情。
    """
    report = AgentEvalReport(total_runs=len(records))
    if not records:
        return report

    n = len(records)
    sum_accuracy = 0.0
    n_with_expected = 0
    total_invalid = 0
    runaway_count = 0
    interrupted_count = 0
    recovered_count = 0
    missed_asking_count = 0
    should_ask_count = 0
    unnecessary_asking_count = 0
    sum_model_calls = 0
    sum_tokens = 0
    sum_elapsed = 0.0

    for record in records:
        result = evaluate_agent_run(record)
        report.per_run.append(result)

        if result.has_expected_sequence:
            n_with_expected += 1
            sum_accuracy += result.tool_selection_accuracy
        total_invalid += result.invalid_tool_calls
        if result.is_runaway:
            runaway_count += 1
        if result.interrupted:
            interrupted_count += 1
            if result.recovered:
                recovered_count += 1
        if result.should_ask:
            should_ask_count += 1
            if result.missed_asking:
                missed_asking_count += 1
        if result.unnecessary_asking:
            unnecessary_asking_count += 1
        sum_model_calls += result.model_call_count
        sum_tokens += result.token_count
        sum_elapsed += result.elapsed_seconds

    report.total_invalid_tool_calls = total_invalid
    report.avg_invalid_tool_calls = round(total_invalid / n, 4)
    report.runaway_rate = round(runaway_count / n, 4)
    if interrupted_count > 0:
        report.recovery_success_rate = round(recovered_count / interrupted_count, 4)
    if should_ask_count > 0:
        report.missed_asking_rate = round(missed_asking_count / should_ask_count, 4)
    report.unnecessary_asking_rate = round(unnecessary_asking_count / n, 4)
    if n_with_expected > 0:
        report.avg_tool_selection_accuracy = round(sum_accuracy / n_with_expected, 4)
    report.avg_model_call_count = round(sum_model_calls / n, 4)
    report.avg_token_count = round(sum_tokens / n, 4)
    report.avg_elapsed_seconds = round(sum_elapsed / n, 4)

    return report


# ---------------------------------------------------------------------------
# 报告格式化打印
# ---------------------------------------------------------------------------
def _format_report(report: AgentEvalReport) -> str:
    """把 AgentEvalReport 格式化成可读的多行字符串。"""
    lines: list[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(f"  Agent 评测报告  (runs={report.total_runs})")
    lines.append(sep)
    lines.append(f"  工具选择正确率       : {report.avg_tool_selection_accuracy:.4f}")
    lines.append(f"  无效工具调用数（总） : {report.total_invalid_tool_calls}")
    lines.append(f"  无效工具调用数（均） : {report.avg_invalid_tool_calls:.4f}")
    lines.append(f"  循环失控率           : {report.runaway_rate:.4f}")
    lines.append(f"  中断恢复成功率       : {report.recovery_success_rate:.4f}")
    lines.append(f"  应提问而未提问比例   : {report.missed_asking_rate:.4f}")
    lines.append(f"  不必要追问比例       : {report.unnecessary_asking_rate:.4f}")
    lines.append(f"  平均模型调用次数     : {report.avg_model_call_count:.2f}")
    lines.append(f"  平均 token 数        : {report.avg_token_count:.2f}")
    lines.append(f"  平均延迟（秒）       : {report.avg_elapsed_seconds:.4f}")
    lines.append("-" * 70)
    lines.append(
        f"  {'qid':<14} {'tool_acc':<9} {'invalid':<8} {'runaway':<8} "
        f"{'missed':<8} {'unnec':<8} {'tokens':<8}"
    )
    lines.append("-" * 70)
    for r in report.per_run:
        lines.append(
            f"  {r.query_id:<14} {r.tool_selection_accuracy:<9.2f} "
            f"{r.invalid_tool_calls:<8} {'Y' if r.is_runaway else 'N':<8} "
            f"{'Y' if r.missed_asking else 'N':<8} "
            f"{'Y' if r.unnecessary_asking else 'N':<8} {r.token_count:<8}"
        )
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="法律 Agent 行为评测脚本（Task 17.2）",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="把评测报告以 JSON 写入指定文件（可选）",
    )
    args = parser.parse_args()

    print(
        "[AgentEval] 提示：本脚本需要对每次运行提供 AgentRunRecord 才能计算指标。\n"
        "             请在代码中调用 evaluate_agent_batch(records) 传入运行记录列表。",
        file=sys.stderr,
    )

    if args.json:
        report = AgentEvalReport()
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[AgentEval] 空报告已写入: {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
