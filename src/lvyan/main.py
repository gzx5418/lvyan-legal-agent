"""律言 Agent Runtime 运行入口。

提供 CLI 与 Python API 两种调用方式：构建 CaseState 初始状态并交由
``lvyan.graph`` 下的 LangGraph 状态图编排执行。

入口分布
--------
- ``run_agent`` —— 向后兼容的 Python API，返回最终 Markdown 字符串。
- ``run_agent_with_state`` —— 返回 :class:`AgentResult`（含完整最终状态），供
  CLI ``--json`` 与 API 层使用。
- ``stream_agent`` —— 同步生成器，流式 yield 节点事件，供 CLI ``--verbose`` 使用。
- ``lvyan.cli.run_cli`` —— 统一 CLI 入口（``--mode`` / ``--json`` / ``--verbose`` 等）。
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from typing import Any, Iterator

from pydantic import BaseModel, Field

from lvyan.runtime import get_shared_graph
from lvyan.schemas import CaseState

__all__ = ["run_agent", "run_agent_with_state", "stream_agent", "AgentResult", "main"]


class AgentResult(BaseModel):
    """单次 Agent 运行的结构化结果。"""

    final_output: str = Field(default="")
    thread_id: str
    state: dict[str, Any] = Field(default_factory=dict)


def _build_initial_state(
    run_id: str,
    thread_id: str,
    query: str,
    complexity: str,
    case_type: str | None,
) -> CaseState:
    """构造 CaseState 初始状态。"""
    return CaseState(
        run_id=run_id,
        thread_id=thread_id,
        current_date=date.today(),
        user_goal=query,
        complexity=complexity,
        case_type=case_type,
    )


def run_agent_with_state(
    query: str,
    thread_id: str | None = None,
    complexity: str = "light",
    case_type: str | None = None,
) -> AgentResult:
    """运行律言 Agent，返回 :class:`AgentResult`（含完整最终状态）。

    与 :func:`run_agent` 的区别：本函数不捕获异常（交由调用方处理），且返回
    完整状态字典而非仅 Markdown 字符串，便于 CLI ``--json`` 与 API 层使用。
    """
    resolved_thread_id = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
    run_id = f"run-{uuid.uuid4().hex}"
    initial = _build_initial_state(
        run_id, resolved_thread_id, query, complexity, case_type
    )
    # P1-5 修复：CLI / Python API 改用共享图实例（同一 checkpointer），
    # 与 API 入口保持单一状态源，支持 interrupt resume
    graph = get_shared_graph()
    config = {"configurable": {"thread_id": resolved_thread_id}}
    result = graph.invoke(initial.model_dump(), config)
    state_dict = result if isinstance(result, dict) else {}
    final_output = state_dict.get("final_output") or ""

    # 图提前结束时（如 ask_user 路由），生成 fallback 输出
    if not final_output:
        from lvyan.api.sse import _build_fallback_output
        final_output = _build_fallback_output(state_dict, query)

    return AgentResult(
        final_output=final_output,
        thread_id=resolved_thread_id,
        state=state_dict,
    )


def run_agent(query: str, thread_id: str | None = None, complexity: str = "light") -> str:
    """运行律言 Agent，返回最终 Markdown 输出。

    Args:
        query: 用户法律问题或合同文本。
        thread_id: 会话线程 ID，用于 LangGraph checkpoint 恢复；为 None 时新生成。
        complexity: 输出复杂度档位，``light`` / ``deep`` / ``document``。

    Returns:
        ``final_output`` 字符串；若为空则返回提示串。异常被捕获并返回友好信息，
        不向调用方抛出。
    """
    try:
        result = run_agent_with_state(query, thread_id=thread_id, complexity=complexity)
        if result.final_output:
            return result.final_output
        return "[lvyan] 当前运行未产生 final_output；请检查各节点是否已接入真实逻辑。"
    except Exception as exc:  # noqa: BLE001 入口层需宽口径捕获，避免向调用方抛出
        return f"[lvyan] Agent 运行出错：{exc}"


def stream_agent(
    query: str,
    thread_id: str | None = None,
    complexity: str = "light",
    case_type: str | None = None,
) -> Iterator[dict[str, Any]]:
    """同步流式运行 Agent，逐个 yield 节点事件字典。

    供 CLI ``--verbose`` 输出节点执行进度。事件类型：
        - ``{"event": "node_end", "node": "<name>"}``
        - ``{"event": "final_output", "output": "..."}``
    """
    resolved_thread_id = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
    run_id = f"run-{uuid.uuid4().hex}"
    initial = _build_initial_state(
        run_id, resolved_thread_id, query, complexity, case_type
    )
    # P1-5 修复：CLI 流式入口同样使用共享图实例
    graph = get_shared_graph()
    config = {"configurable": {"thread_id": resolved_thread_id}}
    final_output = ""
    try:
        for chunk in graph.stream(initial.model_dump(), config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node_name, update in chunk.items():
                if not node_name:
                    continue
                yield {"event": "node_end", "node": node_name}
                if isinstance(update, dict):
                    out = update.get("final_output")
                    if out:
                        final_output = out
    except Exception as exc:  # noqa: BLE001 流式入口需宽口径捕获
        yield {"event": "error", "message": str(exc)}
        return
    yield {"event": "final_output", "output": final_output}


def main() -> None:
    """旧版 CLI 入口（``python -m lvyan.main``），委托给统一 CLI。"""
    # 延迟导入避免与 lvyan.cli 形成循环导入
    from lvyan.cli import run_cli

    sys.exit(run_cli())


if __name__ == "__main__":
    main()
