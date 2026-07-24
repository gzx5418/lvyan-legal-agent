"""律言统一 CLI 入口。

复用 SKILL.md 触发约定，支持以下调用形式::

    python -m lvyan "用户查询"                       # 默认 light 模式
    python -m lvyan "用户查询" --mode deep           # deep 模式
    python -m lvyan "用户查询" --mode document --case-type 起诉状
    python -m lvyan --interactive                     # 交互式多轮对话
    python -m lvyan "用户查询" --thread-id <id>       # 续接历史会话
    python -m lvyan "用户查询" --verbose              # 输出节点执行进度
    python -m lvyan "用户查询" --json                 # 输出 CaseState JSON

输出格式默认 Markdown；``--json`` 输出 :class:`lvyan.main.AgentResult` 的 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from lvyan.main import AgentResult, run_agent_with_state, stream_agent

__all__ = ["build_parser", "run_cli", "main"]


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="lvyan",
        description="律言法律智能体 — 命令行运行入口",
    )
    parser.add_argument(
        "query", nargs="?", default=None, help="法律问题描述或合同文件文本"
    )
    parser.add_argument(
        "--mode",
        choices=["light", "deep", "document"],
        default="light",
        help="输出复杂度档位（默认 light）",
    )
    parser.add_argument(
        "--case-type",
        dest="case_type",
        default=None,
        help="文书生成模式下的文书类型（如 起诉状 / 律师函 / 合同审查报告）",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="进入交互式多轮对话模式",
    )
    parser.add_argument(
        "--thread-id",
        dest="thread_id",
        default=None,
        help="续接历史会话的线程 ID",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出节点执行进度到标准错误",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 输出 AgentResult（含完整 CaseState）",
    )
    return parser


def _print_event(event: dict[str, Any]) -> None:
    """将节点事件以简短形式打印到标准错误。"""
    kind = event.get("event")
    if kind == "node_end":
        sys.stderr.write(f"▸ 节点完成: {event.get('node')}\n")
    elif kind == "final_output":
        sys.stderr.write("▸ 最终输出已生成\n")
    elif kind == "error":
        sys.stderr.write(f"▸ 错误: {event.get('message')}\n")
    sys.stderr.flush()


def _run_verbose(args: argparse.Namespace) -> int:
    """--verbose 模式：流式输出节点事件，最终输出到 stdout。"""
    final_output = ""
    for event in stream_agent(
        args.query,
        thread_id=args.thread_id,
        complexity=args.mode,
        case_type=args.case_type,
    ):
        _print_event(event)
        if event.get("event") == "final_output":
            final_output = event.get("output", "")
        elif event.get("event") == "error":
            # 错误事件已打印到 stderr；输出友好信息到 stdout
            print("[lvyan] 运行过程中发生错误，详见标准错误。")
            return 1
    print(final_output or "[lvyan] 未产生 final_output。")
    return 0


def _run_default(args: argparse.Namespace) -> int:
    """默认 / --json 模式：调用 run_agent_with_state。"""
    try:
        result = run_agent_with_state(
            args.query,
            thread_id=args.thread_id,
            complexity=args.mode,
            case_type=args.case_type,
        )
    except Exception as exc:  # noqa: BLE001 入口层需宽口径捕获
        if args.as_json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[lvyan] Agent 运行出错：{exc}")
        return 1

    if args.as_json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, default=str))
    else:
        print(result.final_output or "[lvyan] 未产生 final_output。")
    return 0


def _run_interactive(args: argparse.Namespace) -> int:
    """交互式多轮对话：逐行读取查询，共用 thread_id 续接。"""
    thread_id = args.thread_id
    print("律言交互模式（输入空行或 Ctrl+C 退出）", file=sys.stderr)
    try:
        for line in sys.stdin:
            query = line.strip()
            if not query:
                break
            output = run_agent_with_state(
                query,
                thread_id=thread_id,
                complexity=args.mode,
                case_type=args.case_type,
            )
            thread_id = output.thread_id
            print(output.final_output or "[lvyan] 未产生 final_output。")
    except (KeyboardInterrupt, EOFError):
        print("\n再见。", file=sys.stderr)
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口，返回退出码。"""
    args = build_parser().parse_args(argv)

    if args.interactive:
        return _run_interactive(args)

    if not args.query:
        sys.stderr.write("错误：请提供查询文本，或使用 --interactive 进入交互模式。\n")
        return 2

    if args.verbose:
        return _run_verbose(args)

    return _run_default(args)


def main() -> None:
    """``python -m lvyan`` 入口。"""
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
