"""真模型管线评测(U-14):金标全集 × 真实网关,输出与离线基线的漂移。

与 :mod:`pipeline_eval`(离线降级路径)的区别:
- ``--require-real``:网关不可用直接退出码 **2**(区分于评测失败=1),
  供夜间 workflow 明确判定"今天没测成";
- 指标额外覆盖:弃答率(U-12/13)、每 run 成本与延迟分布;
- 报告落 ``outputs/evals/live-<date>.json``,供 U-15 漂移对比。

用法::

    python tests/evals/live_eval.py [--limit N] [--json path] [--require-real]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_eval import load_golden_set  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "outputs" / "evals"


def _run_single_query(
    query: str, *, complexity: str = "light", as_of: Any = None
) -> dict[str, Any]:
    """对单条查询跑完整 graph,返回 final_state 关键字段(含耗时)。"""
    import datetime
    import time
    import uuid

    from lvyan.graph.builder import build_graph
    from lvyan.schemas import CaseState

    initial = CaseState(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        current_date=datetime.date.today(),
        user_goal=query,
        complexity=complexity,
        law_as_of_date=as_of,
    )
    graph = build_graph()
    config = {"configurable": {"thread_id": initial.thread_id}}
    t0 = time.monotonic()
    final_state = graph.invoke(initial.model_dump(), config)
    elapsed = time.monotonic() - t0
    if not isinstance(final_state, dict):
        final_state = {}
    final_state["_elapsed_seconds"] = elapsed
    return final_state


def _real_backend_available() -> tuple[bool, str]:
    """探测真实 LLM 网关是否可用:配置齐备 **且一次真实 chat ping 成功**。

    ``llm_available`` 只验证配置形态(有 URL 即 True),key 失效(401)
    不会暴露——真模型评测必须用真实调用验证,否则"真模型"报告实为降级路径。
    """
    from lvyan.config import settings
    from lvyan.llm import llm_available

    if not settings.model_gateway_url:
        return False, "model_gateway_url 未配置"
    if not llm_available():
        return False, "llm_available=False"
    try:
        from lvyan.llm import chat_json

        chat_json(
            messages=[{"role": "user", "content": "ping"}],
            temperature=0.0,
            max_tokens=5,
        )
    except Exception as exc:  # noqa: BLE001 boundary-exception: 探测即判定
        return False, f"chat ping 失败:{type(exc).__name__}"
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真模型管线评测(U-14)")
    parser.add_argument(
        "--golden", default=None, help="金标集路径(默认 tests/evals/golden_set.json)"
    )
    parser.add_argument("--limit", type=int, default=None, help="仅评测前 N 条")
    parser.add_argument("--json", dest="json_path", default=None, help="报告输出路径")
    parser.add_argument(
        "--require-real",
        action="store_true",
        help="网关不可用时退出码 2(夜间 workflow 用以区分'没测成'与'测失败')",
    )
    parser.add_argument(
        "--json-summary",
        dest="json_summary",
        default=None,
        help="同时写出 Markdown 摘要(供 GitHub step summary)",
    )
    args = parser.parse_args(argv)

    real_ok, real_reason = _real_backend_available()
    if not real_ok:
        print(f"[LiveEval] 真实网关不可用:{real_reason}", file=sys.stderr)
        if args.require_real:
            return 2
        print("[LiveEval] 继续(结果将是降级路径,仅供对照)", file=sys.stderr)

    golden = load_golden_set(args.golden)
    if args.limit and args.limit > 0:
        golden = golden[: args.limit]

    results: list[dict[str, Any]] = []
    for item in golden:
        qid = item.get("id", "")
        query = item.get("query", "")
        as_of = item.get("as_of")
        entry: dict[str, Any] = {"qid": qid, "query": query, "as_of": as_of}
        try:
            outcome = _run_single_query(
                query, complexity=str(item.get("complexity") or "light"), as_of=as_of
            )
            final_output = str(outcome.get("final_output") or "")
            audit = outcome.get("citation_audit") or {}
            legal_answer = outcome.get("legal_answer")
            entry.update(
                ok=True,
                elapsed_seconds=round(float(outcome.get("_elapsed_seconds", 0.0)), 2),
                statute_count=len(outcome.get("statutes") or []),
                case_count=len(outcome.get("cases") or []),
                abstained=bool(
                    (audit.get("abstain_recommended") if isinstance(audit, dict) else False)
                    or (
                        isinstance(legal_answer, dict)
                        and (legal_answer.get("meta") or {}).get("abstained", False)
                    )
                ),
                audit_passed=bool(audit.get("passed", False)) if isinstance(audit, dict) else None,
                output_chars=len(final_output),
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        results.append(entry)
        status = "OK" if entry.get("ok") else "FAIL"
        print(f"[LiveEval] {status} {qid}", flush=True)

    ok_items = [r for r in results if r.get("ok")]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "real_backend": real_ok,
        "real_backend_reason": real_reason,
        "total": len(results),
        "ok": len(ok_items),
        "failed": len(results) - len(ok_items),
        "abstain_rate": (
            sum(1 for r in ok_items if r.get("abstained")) / len(ok_items) if ok_items else 0.0
        ),
        "audit_pass_rate": (
            sum(1 for r in ok_items if r.get("audit_passed")) / len(ok_items) if ok_items else 0.0
        ),
        "avg_output_chars": (
            statistics.mean(r["output_chars"] for r in ok_items) if ok_items else 0.0
        ),
        "avg_elapsed_seconds": (
            round(
                statistics.mean(r["elapsed_seconds"] for r in ok_items if "elapsed_seconds" in r), 2
            )
            if ok_items
            else 0.0
        ),
        "results": results,
    }

    print(
        f"\n[LiveEval] total={summary['total']} ok={summary['ok']} "
        f"abstain_rate={summary['abstain_rate']:.2f} audit_pass={summary['audit_pass_rate']:.2f}",
        flush=True,
    )

    if args.json_summary:
        summary_path = Path(args.json_summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Live Eval 摘要",
            "",
            f"- 生成时间:{summary['generated_at']}",
            f"- 真实网关:{'可用' if summary['real_backend'] else '不可用'}"
            f"({summary['real_backend_reason']})",
            f"- 用例:{summary['ok']}/{summary['total']} 成功,失败 {summary['failed']}",
            f"- 弃答率:{summary['abstain_rate']:.2%}",
            f"- 引用审计通过率:{summary['audit_pass_rate']:.2%}",
            f"- 平均耗时:{summary.get('avg_elapsed_seconds', 0.0)}s",
            "",
        ]
        for r in results:
            mark = "OK" if r.get("ok") else "FAIL"
            lines.append(
                f"- [{mark}] {r['qid']}" + (f" — {r.get('error', '')}" if not r.get("ok") else "")
            )
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.json_path:
        out_path = Path(args.json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[LiveEval] 报告已写入: {out_path}", file=sys.stderr)
    else:
        default_path = DEFAULT_OUTPUT_DIR / f"live-{datetime.now().date().isoformat()}.json"
        default_path.parent.mkdir(parents=True, exist_ok=True)
        default_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[LiveEval] 报告已写入: {default_path}", file=sys.stderr)

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
