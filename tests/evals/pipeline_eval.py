"""P1-4：Agent pipeline 端到端回归评测。

区别于 ``run_regression.py``（仅对 mock state 跑 evaluator），本脚本启动
**完整 LangGraph 图**（preflight → ... → output_guardrail），用真实检索 +
规则路径节点生成最终 state，再与金标集对比。

三层评测分层（按审查者建议）
---------------------------
- A. evaluator self-consistency：``run_regression.py``（保留，已存在）
- B. **pipeline regression**：本脚本（P1-4 新增）
- C. online 模型评测：需真实模型网关，留给生产侧手动执行

环境隔离
--------
- ``MODEL_GATEWAY_URL=""``：节点走规则降级路径（fact_extractor / planner /
  legal_reasoner 全部回退到正则/模板），不依赖外部 LLM。
- ``DATABASE_URL`` 仅占位：默认 ``MemorySaver``，不连 PostgreSQL。
- 官方法律库不存在时跳过检索类指标（仅验证 graph 端到端不崩溃）。

公开接口
--------
    run_pipeline_evaluation(golden_path, limit, complexity) -> PipelineReport

CLI 用法
--------
    python tests/evals/pipeline_eval.py --limit 3
    python tests/evals/pipeline_eval.py --json pipeline-report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 路径引导
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parents[2]
_SRC_DIR = _AGENT_DIR / "src"
_EVALS_DIR = _THIS_FILE.parent
for _p in (str(_SRC_DIR), str(_EVALS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lvyan.graph import build_graph  # noqa: E402
from lvyan.schemas import CaseState  # noqa: E402

from answer_eval import (  # noqa: E402
    AnswerEvalReport,
    evaluate_answer_batch,
)
from retrieval_eval import load_golden_set  # noqa: E402

DEFAULT_GOLDEN_PATH = _AGENT_DIR / "tests" / "evals" / "golden_set.json"


@dataclass
class PipelineQueryResult:
    """单条 query 的 pipeline 评测结果。"""

    query_id: str
    query: str
    category: str
    elapsed_seconds: float = 0.0
    final_output_chars: int = 0
    statutes_count: int = 0
    cases_count: int = 0
    facts_count: int = 0
    # 节点是否全部成功（无异常）
    pipeline_ok: bool = True
    error: str | None = None
    # evaluator 指标（与 answer_eval 一致）
    statute_accuracy: float = 0.0
    fabrication_rate: float = 0.0
    has_answer_golden: bool = False


@dataclass
class PipelineReport:
    """整批 pipeline 评测聚合报告。"""

    total_queries: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    avg_elapsed_seconds: float = 0.0
    avg_statute_accuracy: float = 0.0
    avg_fabrication_rate: float = 0.0
    pipeline_success_rate: float = 0.0
    per_query: list[PipelineQueryResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_initial_state(query: str, complexity: str) -> CaseState:
    """构造 pipeline 入口 CaseState。"""
    import datetime
    import uuid

    return CaseState(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        current_date=datetime.date.today(),
        user_goal=query,
        complexity=complexity,
    )


def run_pipeline_evaluation(
    golden_path: str | Path | None = None,
    limit: int | None = None,
    complexity: str = "light",
) -> PipelineReport:
    """对金标集运行完整 graph pipeline 并评测。

    Args:
        golden_path: 金标集路径；None 用默认。
        limit: 仅评测前 N 条（性能考量）。
        complexity: 输出复杂度档位；建议 ``light`` 加速。

    Returns:
        PipelineReport：含 pipeline 成功率与 answer 指标。
    """
    golden = load_golden_set(golden_path)
    if limit is not None and limit > 0:
        golden = golden[:limit]

    report = PipelineReport(total_queries=len(golden))
    graph = build_graph()  # 用 MemorySaver，每个 thread 独立

    items_for_answer_eval: list[dict[str, Any]] = []

    for item in golden:
        qid = item.get("id", "")
        query = item.get("query", "")
        category = item.get("category", "")
        answer_golden = item.get("answer_golden")

        qr = PipelineQueryResult(
            query_id=qid, query=query, category=category,
            has_answer_golden=bool(answer_golden),
        )

        t0 = time.monotonic()
        try:
            initial = _build_initial_state(query, complexity)
            config = {"configurable": {"thread_id": initial.thread_id}}
            final_state = graph.invoke(initial.model_dump(), config)
            if not isinstance(final_state, dict):
                final_state = {}
            qr.elapsed_seconds = time.monotonic() - t0
            qr.final_output_chars = len(str(final_state.get("final_output") or ""))
            qr.statutes_count = len(final_state.get("statutes") or [])
            qr.cases_count = len(final_state.get("cases") or [])
            qr.facts_count = len(final_state.get("facts") or [])
            qr.pipeline_ok = True

            items_for_answer_eval.append({
                "state": final_state,
                "answer_golden": answer_golden,
                "query_id": qid,
                "query": query,
                "category": category,
            })
        except Exception as exc:  # noqa: BLE001
            qr.elapsed_seconds = time.monotonic() - t0
            qr.pipeline_ok = False
            qr.error = str(exc)[:200]

        report.per_query.append(qr)

    # 聚合
    successful = [q for q in report.per_query if q.pipeline_ok]
    report.successful_runs = len(successful)
    report.failed_runs = len(report.per_query) - len(successful)
    if report.per_query:
        report.avg_elapsed_seconds = (
            sum(q.elapsed_seconds for q in report.per_query) / len(report.per_query)
        )
        report.pipeline_success_rate = len(successful) / len(report.per_query)

    # 对成功的 run 跑 answer_eval
    if successful and items_for_answer_eval:
        answer_report: AnswerEvalReport = evaluate_answer_batch(items_for_answer_eval)
        report.avg_statute_accuracy = answer_report.avg_statute_accuracy
        report.avg_fabrication_rate = answer_report.avg_fabrication_rate
        # 把指标回填到 per_query
        for qr, ar in zip(report.per_query, answer_report.per_query):
            if qr.query_id == ar.query_id:
                qr.statute_accuracy = ar.statute_accuracy
                qr.fabrication_rate = ar.fabrication_rate

    return report


def _format_report(report: PipelineReport) -> str:
    lines: list[str] = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(
        f"  Pipeline 端到端评测报告  (total={report.total_queries}, "
        f"ok={report.successful_runs}, failed={report.failed_runs})"
    )
    lines.append(sep)
    lines.append(f"  pipeline 成功率     : {report.pipeline_success_rate:.4f}")
    lines.append(f"  平均耗时（秒）       : {report.avg_elapsed_seconds:.2f}")
    lines.append(f"  法条引用准确率       : {report.avg_statute_accuracy:.4f}")
    lines.append(f"  虚构法条率           : {report.avg_fabrication_rate:.4f}")
    lines.append("-" * 70)
    lines.append(
        f"  {'qid':<14} {'ok':<4} {'secs':<7} {'statutes':<9} {'acc':<6} {'fab':<6}"
    )
    lines.append("-" * 70)
    for q in report.per_query:
        lines.append(
            f"  {q.query_id:<14} {'Y' if q.pipeline_ok else 'N':<4} "
            f"{q.elapsed_seconds:<7.2f} {q.statutes_count:<9} "
            f"{q.statute_accuracy:<6.2f} {q.fabrication_rate:<6.2f}"
        )
    lines.append(sep)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent pipeline 端到端回归评测（P1-4）",
    )
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN_PATH))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--complexity", default="light",
                        choices=["light", "deep", "document"])
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    print(f"[Pipeline] golden={args.golden} limit={args.limit}", file=sys.stderr)
    report = run_pipeline_evaluation(
        golden_path=args.golden,
        limit=args.limit,
        complexity=args.complexity,
    )
    print(_format_report(report))

    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[Pipeline] JSON 报告已写入: {args.json}", file=sys.stderr)

    # pipeline 成功率 < 0.7 视为 CI 失败
    return 0 if report.pipeline_success_rate >= 0.7 else 1


if __name__ == "__main__":
    sys.exit(main())
