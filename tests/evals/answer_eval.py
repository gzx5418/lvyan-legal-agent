"""回答评测脚本（Task 17.1）。

对法律 Agent 的最终回答计算质量指标：

- **法条引用准确率**：输出中引用的法条有多少真实存在于法规库
  （用 ``validators/citation.py`` 的 ``validate_citations`` 校验）。
- **虚构法条率**：虚构法条数 / 总引用数（``not_found`` 引用占比）。
- **虚构案号率**：虚构案号数 / 总案号引用数（案号正则匹配后与 ``state.cases`` 比对）。
- **争议焦点覆盖率**：金标争议焦点被覆盖数 / 金标争议焦点总数。
- **证据缺口召回率**：识别出的真实证据缺口 / 金标证据缺口总数。
- **反方论点覆盖率**：``reasoning_result.defendant_arguments`` 覆盖的金标反方论点数 /
  金标反方论点总数。
- **过度确定性比例**：标注「有利/较有利」但金标应为「胶着/较不利」的比例。

公开接口
--------
    evaluate_answer(state, answer_golden) -> AnswerEvalResult
    evaluate_answer_batch(items) -> AnswerEvalReport

CLI 用法
--------
    python tests/evals/answer_eval.py --golden tests/evals/golden_set.json --limit 5
    python tests/evals/answer_eval.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径引导：确保以脚本形式运行时（python tests/evals/answer_eval.py）
# 也能从 AGENT/src 导入 lvyan 包。pyproject.toml 已配 pythonpath=["src"]，
# 故 pytest 运行不需要这段。
# ---------------------------------------------------------------------------
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]  # AGENT/
_SRC_DIR = _AGENT_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from lvyan.validators.citation import (  # noqa: E402
    CitationValidationReport,
    _char_bigrams,
    _jaccard,
    _reasoning_text,
    validate_citations,
)

# 默认金标集路径（相对 AGENT 工程根）
DEFAULT_GOLDEN_PATH = _AGENT_DIR / "tests" / "evals" / "golden_set.json"

# ---------------------------------------------------------------------------
# 裁判倾向：中文标签 ↔ ReasoningResult.judicial_tendency 枚举
# ---------------------------------------------------------------------------
TENDENCY_CN_TO_ENUM: dict[str, str] = {
    "有利": "favorable",
    "较有利": "somewhat_favorable",
    "胶着": "even",
    "较不利": "somewhat_unfavorable",
    "信息不足": "insufficient",
}

# 标注为「过度确定」的判定集合：实际倾向过于乐观
_OVERCONFIDENT_TENDENCIES = {"favorable", "somewhat_favorable"}
# 金标为「胶着/较不利」时才可能构成过度确定
_GOLDEN_PESSIMISTIC_TENDENCIES = {"even", "somewhat_unfavorable"}

# 案号正则：匹配 ``(年份)法院代号类型程序字第XXX号`` 等中国法院案号格式
# 示例：(2023)京01民初123号、(2022)沪民终456号、(2021)最高法民申789号
_CASE_NUMBER_RE = re.compile(
    r"\((?P<year>\d{4})\)"
    r"(?P<court>[\u4e00-\u9fffA-Za-z0-9]+?)"
    r"(?P<case_type>民|刑|行|执|商|知|破|辖|监|抗)?"
    r"(?P<procedure>初|终|再|抗|监|辖)?"
    r"字?第?(?P<serial>\d+)号"
)

# 覆盖匹配的 bigram Jaccard 阈值
_COVERAGE_JACCARD_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class AnswerEvalResult(BaseModel):
    """单条用例的回答评测结果。"""

    query_id: str
    query: str = ""
    category: str = ""
    # 法条引用
    total_citations: int = 0
    valid_citations: int = 0
    fabricated_citations: int = 0  # 虚构法条数（not_found）
    statute_accuracy: float = 0.0  # 法条引用准确率
    fabrication_rate: float = 0.0  # 虚构法条率
    # 案号
    total_case_numbers: int = 0
    fabricated_case_numbers: int = 0
    case_number_fabrication_rate: float = 0.0  # 虚构案号率
    # 覆盖率指标
    disputed_issue_coverage: float = 0.0  # 争议焦点覆盖率
    evidence_gap_recall: float = 0.0  # 证据缺口召回率
    defendant_argument_coverage: float = 0.0  # 反方论点覆盖率
    # 过度确定性
    is_overconfident: bool = False  # 本条是否过度确定
    has_answer_golden: bool = False  # 是否有 answer_golden

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AnswerEvalReport(BaseModel):
    """整批回答评测聚合报告。"""

    total_queries: int = 0
    evaluated_queries: int = 0  # 有 answer_golden 且参与评测的用例数
    avg_statute_accuracy: float = 0.0  # 法条引用准确率
    avg_fabrication_rate: float = 0.0  # 虚构法条率
    avg_case_number_fabrication_rate: float = 0.0  # 虚构案号率
    avg_disputed_issue_coverage: float = 0.0  # 争议焦点覆盖率
    avg_evidence_gap_recall: float = 0.0  # 证据缺口召回率
    avg_defendant_argument_coverage: float = 0.0  # 反方论点覆盖率
    overconfidence_ratio: float = 0.0  # 过度确定性比例
    per_query: list[AnswerEvalResult] = Field(default_factory=list)

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


def _is_covered(golden_phrase: str, actuals: list[str]) -> bool:
    """判断金标短语是否被实际短语列表覆盖。

    覆盖判定（任一满足即视为覆盖）：
    1. 双向子串匹配：金标是实际的子串，或实际是金标的子串。
    2. 字符 bigram Jaccard ≥ ``_COVERAGE_JACCARD_THRESHOLD``。
    """
    g = (golden_phrase or "").strip()
    if not g:
        return False
    g_grams = _char_bigrams(g)
    for a in actuals:
        a = (a or "").strip()
        if not a:
            continue
        if g in a or a in g:
            return True
        if g_grams and _jaccard(g_grams, _char_bigrams(a)) >= _COVERAGE_JACCARD_THRESHOLD:
            return True
    return False


def _extract_case_numbers(text: str) -> list[str]:
    """从文本中提取所有案号（去重，保持出现顺序）。"""
    seen: set[str] = set()
    results: list[str] = []
    for m in _CASE_NUMBER_RE.finditer(text):
        case_no = m.group(0)
        if case_no not in seen:
            seen.add(case_no)
            results.append(case_no)
    return results


def _get_case_numbers_from_state(state: Any) -> set[str]:
    """从 ``state.cases``（CaseAuthority 列表）中收集真实案号集合。"""
    cases = _get(state, "cases", []) or []
    numbers: set[str] = set()
    for case in cases:
        case_number = _get(case, "case_number", None)
        if case_number:
            numbers.add(str(case_number).strip())
    return numbers


def _get_evidence_gap_phrases(state: Any) -> list[str]:
    """从 ``state.evidence_requirements`` 中提取证据缺口描述短语。

    仅取 ``current_status`` 为 ``missing`` 或 ``partial`` 的项，
    优先用 ``gap_description``，回退到 ``fact_to_prove`` / ``evidence_types``。
    """
    reqs = _get(state, "evidence_requirements", []) or []
    phrases: list[str] = []
    for req in reqs:
        status = _get(req, "current_status", "")
        if status not in ("missing", "partial"):
            continue
        gap = _get(req, "gap_description", None)
        if gap:
            phrases.append(str(gap))
            continue
        fact = _get(req, "fact_to_prove", None)
        if fact:
            phrases.append(str(fact))
        evidence_types = _get(req, "evidence_types", []) or []
        phrases.extend(str(t) for t in evidence_types if t)
    return phrases


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def _compute_citation_metrics(
    citation_report: CitationValidationReport,
) -> tuple[int, int, int, float, float]:
    """从 CitationValidationReport 计算 法条引用指标。

    Returns:
        (total, valid, fabricated, accuracy, fabrication_rate)
    """
    total = citation_report.total_citations
    valid = citation_report.valid_citations
    fabricated = sum(
        1
        for issue in citation_report.issues
        if issue.issue_type == "not_found" and issue.severity == "error"
    )
    accuracy = (valid / total) if total > 0 else 1.0
    fab_rate = (fabricated / total) if total > 0 else 0.0
    return total, valid, fabricated, accuracy, fab_rate


def _compute_case_number_metrics(text: str, known_case_numbers: set[str]) -> tuple[int, int, float]:
    """计算案号虚构指标。

    Args:
        text: 推理结果文本（含案号引用）。
        known_case_numbers: ``state.cases`` 中的真实案号集合。

    Returns:
        (total_case_numbers, fabricated_case_numbers, fabrication_rate)
    """
    extracted = _extract_case_numbers(text)
    total = len(extracted)
    if total == 0:
        return 0, 0, 0.0
    fabricated = 0
    for case_no in extracted:
        # 案号在已知集合中存在（子串匹配，容忍细微差异）视为真实
        if not any(case_no in known or known in case_no for known in known_case_numbers):
            fabricated += 1
    fab_rate = fabricated / total
    return total, fabricated, fab_rate


def _compute_coverage(golden_items: list[str], actual_items: list[str]) -> float:
    """计算覆盖率：金标项被实际项覆盖的数量 / 金标总数。"""
    if not golden_items:
        return 1.0  # 无金标时不计入扣分
    covered = sum(1 for g in golden_items if _is_covered(g, actual_items))
    return covered / len(golden_items)


def _is_overconfident(actual_tendency: str, golden_tendency_cn: str) -> bool:
    """判断是否过度确定：实际乐观但金标应为胶着/较不利。"""
    golden_enum = TENDENCY_CN_TO_ENUM.get(golden_tendency_cn, "")
    if not golden_enum:
        return False
    if actual_tendency in _OVERCONFIDENT_TENDENCIES:
        return golden_enum in _GOLDEN_PESSIMISTIC_TENDENCIES
    return False


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def evaluate_answer(
    state: Any,
    answer_golden: dict[str, Any] | None,
    query_id: str = "",
    query: str = "",
    category: str = "",
    statutes: list[Any] | None = None,
) -> AnswerEvalResult:
    """对单条用例计算回答评测指标。

    Args:
        state: ``GraphState`` / ``CaseState`` 或 dict，含 ``reasoning_result``、
            ``cases``、``evidence_requirements`` 等字段。``statutes`` 优先从本参数
            读取，为 None 时从 ``state.statutes`` 读取。
        answer_golden: 金标回答字段 dict，含 ``disputed_issues`` /
            ``evidence_gaps`` / ``defendant_arguments`` / ``ruling_tendency``。
            为 None 或空 dict 时仅计算法条/案号指标。
        query_id / query / category: 用例元信息（用于报告展示）。
        statutes: 法规条目列表，为 None 时从 ``state.statutes`` 读取。

    Returns:
        AnswerEvalResult：含全部回答评测指标。
    """
    reasoning_result = _get(state, "reasoning_result", None)
    if statutes is None:
        statutes = _get(state, "statutes", []) or []

    # 法条引用指标
    citation_report = validate_citations(reasoning_result, statutes)
    total, valid, fabricated, accuracy, fab_rate = _compute_citation_metrics(citation_report)

    # 案号指标
    text = _reasoning_text(reasoning_result)
    known_case_numbers = _get_case_numbers_from_state(state)
    total_cn, fab_cn, cn_fab_rate = _compute_case_number_metrics(text, known_case_numbers)

    has_golden = bool(answer_golden)
    result = AnswerEvalResult(
        query_id=query_id,
        query=query,
        category=category,
        total_citations=total,
        valid_citations=valid,
        fabricated_citations=fabricated,
        statute_accuracy=round(accuracy, 4),
        fabrication_rate=round(fab_rate, 4),
        total_case_numbers=total_cn,
        fabricated_case_numbers=fab_cn,
        case_number_fabrication_rate=round(cn_fab_rate, 4),
        has_answer_golden=has_golden,
    )

    if not has_golden:
        return result

    # 争议焦点覆盖率
    golden_issues = answer_golden.get("disputed_issues", []) or []
    actual_issues: list[str] = [
        str(x) for x in (_get(reasoning_result, "disputed_focus", []) or [])
    ]
    result.disputed_issue_coverage = round(_compute_coverage(golden_issues, actual_issues), 4)

    # 证据缺口召回率
    golden_gaps = answer_golden.get("evidence_gaps", []) or []
    actual_gaps = _get_evidence_gap_phrases(state)
    result.evidence_gap_recall = round(_compute_coverage(golden_gaps, actual_gaps), 4)

    # 反方论点覆盖率
    golden_defendant = answer_golden.get("defendant_arguments", []) or []
    actual_defendant: list[str] = [
        str(x) for x in (_get(reasoning_result, "defendant_arguments", []) or [])
    ]
    result.defendant_argument_coverage = round(
        _compute_coverage(golden_defendant, actual_defendant), 4
    )

    # 过度确定性
    actual_tendency = str(_get(reasoning_result, "judicial_tendency", "") or "")
    golden_tendency_cn = str(answer_golden.get("ruling_tendency", "") or "")
    result.is_overconfident = _is_overconfident(actual_tendency, golden_tendency_cn)

    return result


def evaluate_answer_batch(
    items: list[dict[str, Any]],
) -> AnswerEvalReport:
    """对一批用例计算回答评测聚合报告。

    Args:
        items: 每项为 dict，至少含 ``state``（GraphState/CaseState/dict）与可选的
            ``answer_golden`` / ``query_id`` / ``query`` / ``category`` / ``statutes``。

    Returns:
        AnswerEvalReport：含全局聚合指标与 per_query 详情。
    """
    report = AnswerEvalReport(total_queries=len(items))

    evaluated = 0
    overconfident_count = 0
    # 用于加权平均的累计值
    sum_accuracy = 0.0
    sum_fab_rate = 0.0
    sum_cn_fab_rate = 0.0
    sum_issue_cov = 0.0
    sum_gap_recall = 0.0
    sum_defendant_cov = 0.0
    # 有对应内容的用例数（分母）
    n_citation = 0  # 有引用的用例
    n_case_number = 0  # 有案号的用例
    n_evaluated = 0  # 有 answer_golden 的用例

    for item in items:
        state = item.get("state")
        answer_golden = item.get("answer_golden")
        statutes = item.get("statutes")
        result = evaluate_answer(
            state=state,
            answer_golden=answer_golden,
            query_id=item.get("query_id", item.get("id", "")),
            query=item.get("query", ""),
            category=item.get("category", ""),
            statutes=statutes,
        )
        report.per_query.append(result)

        if result.has_answer_golden:
            n_evaluated += 1
            evaluated += 1
            sum_issue_cov += result.disputed_issue_coverage
            sum_gap_recall += result.evidence_gap_recall
            sum_defendant_cov += result.defendant_argument_coverage
            if result.is_overconfident:
                overconfident_count += 1

        # 法条引用指标：所有用例都参与（即使无 answer_golden）
        n_citation += 1
        sum_accuracy += result.statute_accuracy
        sum_fab_rate += result.fabrication_rate
        if result.total_case_numbers > 0:
            n_case_number += 1
            sum_cn_fab_rate += result.case_number_fabrication_rate

    report.evaluated_queries = evaluated

    if n_citation > 0:
        report.avg_statute_accuracy = round(sum_accuracy / n_citation, 4)
        report.avg_fabrication_rate = round(sum_fab_rate / n_citation, 4)
    if n_case_number > 0:
        report.avg_case_number_fabrication_rate = round(sum_cn_fab_rate / n_case_number, 4)
    if n_evaluated > 0:
        report.avg_disputed_issue_coverage = round(sum_issue_cov / n_evaluated, 4)
        report.avg_evidence_gap_recall = round(sum_gap_recall / n_evaluated, 4)
        report.avg_defendant_argument_coverage = round(sum_defendant_cov / n_evaluated, 4)
        report.overconfidence_ratio = round(overconfident_count / n_evaluated, 4)

    return report


# ---------------------------------------------------------------------------
# 报告格式化打印
# ---------------------------------------------------------------------------
def _format_report(report: AnswerEvalReport) -> str:
    """把 AnswerEvalReport 格式化成可读的多行字符串。"""
    lines: list[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(
        f"  回答评测报告  (total={report.total_queries}, evaluated={report.evaluated_queries})"
    )
    lines.append(sep)
    lines.append(f"  法条引用准确率       : {report.avg_statute_accuracy:.4f}")
    lines.append(f"  虚构法条率           : {report.avg_fabrication_rate:.4f}")
    lines.append(f"  虚构案号率           : {report.avg_case_number_fabrication_rate:.4f}")
    lines.append(f"  争议焦点覆盖率       : {report.avg_disputed_issue_coverage:.4f}")
    lines.append(f"  证据缺口召回率       : {report.avg_evidence_gap_recall:.4f}")
    lines.append(f"  反方论点覆盖率       : {report.avg_defendant_argument_coverage:.4f}")
    lines.append(f"  过度确定性比例       : {report.overconfidence_ratio:.4f}")
    lines.append("-" * 70)
    lines.append(
        f"  {'qid':<14} {'cite_acc':<9} {'fab_rate':<9} {'issue_cov':<10} "
        f"{'gap_rec':<9} {'def_cov':<9} {'overconf':<9}"
    )
    lines.append("-" * 70)
    for q in report.per_query:
        lines.append(
            f"  {q.query_id:<14} {q.statute_accuracy:<9.2f} {q.fabrication_rate:<9.2f} "
            f"{q.disputed_issue_coverage:<10.2f} {q.evidence_gap_recall:<9.2f} "
            f"{q.defendant_argument_coverage:<9.2f} {'Y' if q.is_overconfident else 'N':<9}"
        )
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="法律问答回答评测脚本（Task 17.1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--json",
        default=None,
        help="把评测报告以 JSON 写入指定文件（可选）",
    )
    args = parser.parse_args()

    # CLI 模式：仅加载金标集并提示需要 mock state
    # 实际端到端评测请用 run_regression.py（含 mock state 生成）
    print(f"[AnswerEval] 金标集: {args.golden}", file=sys.stderr)
    print(
        "[AnswerEval] 提示：本脚本需要对每条用例提供 mock state 才能计算指标。\n"
        "             请使用 run_regression.py 进行 CI 回归评测，或在代码中\n"
        "             调用 evaluate_answer_batch(items) 传入 state 列表。",
        file=sys.stderr,
    )

    # 加载金标集统计有 answer_golden 的用例数
    with open(args.golden, "r", encoding="utf-8") as f:
        golden = json.load(f)
    if args.limit:
        golden = golden[: args.limit]
    n_with_golden = sum(1 for g in golden if g.get("answer_golden"))
    print(
        f"[AnswerEval] 金标集共 {len(golden)} 条，其中 {n_with_golden} 条含 answer_golden。",
        file=sys.stderr,
    )

    if args.json:
        report = AnswerEvalReport(total_queries=len(golden), evaluated_queries=n_with_golden)
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[AnswerEval] 空报告已写入: {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
