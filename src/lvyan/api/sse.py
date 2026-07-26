"""SSE 事件分发与 Agent 运行管理（v2：统一状态源 + LangGraph interrupt）。

核心改进（PR1）
----------------
- 使用共享图实例（``get_shared_graph``），不再每次 ``build_graph()``。
- HITL 改用 LangGraph ``interrupt()`` + ``Command(resume=...)`` 机制，
  替代旧的 ``asyncio.Event`` 应用层暂停。
- Agent 运行后自动检查 ``graph.get_state()`` 是否有待处理中断，
  有则发布 ``hitl_required`` 事件。
- HITL 恢复通过 ``graph.invoke(Command(resume=...))`` 驱动，
  状态由 checkpointer 持久化，服务重启不丢失。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from .models import HITLRequest

_logger = logging.getLogger("lvyan.api.sse")

# runner 协议：async (query, thread_id, complexity, ctx) -> final_output(str)
Runner = Callable[[str, str, str, "RunContext"], Awaitable[str]]


class RunContext:
    """单次 Agent 运行的上下文。"""

    def __init__(self, run_id: str, thread_id: str) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        # 状态：started / running / awaiting_hitl / completed / failed
        self.status: str = "started"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.final_output: str | None = None
        self.error: str | None = None
        # HITL 中断信息（LangGraph interrupt 机制）
        self.hitl_interrupt: dict[str, Any] | None = None

    async def publish(self, event: dict[str, Any]) -> None:
        """发布一个 SSE 事件到队列，供流式消费者读取。"""
        await self.queue.put(event)


class RunManager:
    """Agent 运行注册表与驱动器。"""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runs: dict[str, RunContext] = {}
        self._runner: Runner | None = runner

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------
    def create_run(
        self, query: str, thread_id: str | None, complexity: str
    ) -> RunContext:
        """创建并异步启动一次 Agent 运行。"""
        run_id = f"run-{uuid.uuid4().hex}"
        resolved_thread_id = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
        ctx = RunContext(run_id, resolved_thread_id)
        self._runs[run_id] = ctx
        asyncio.create_task(self._drive(ctx, query, complexity))
        return ctx

    async def _drive(self, ctx: RunContext, query: str, complexity: str) -> None:
        """驱动 runner 执行，捕获异常，最终推送 final_output 并关闭流。"""
        ctx.status = "running"
        try:
            if self._runner is not None:
                output = await self._runner(query, ctx.thread_id, complexity, ctx)
            else:
                output = await default_runner(query, ctx.thread_id, complexity, ctx)
            ctx.final_output = output or ""
            ctx.status = "completed"
            await ctx.publish({"event": "final_output", "output": ctx.final_output})
        except Exception as exc:  # noqa: BLE001 入口层需宽口径捕获
            ctx.status = "failed"
            ctx.error = str(exc)
            _logger.exception("Agent run %s failed", ctx.run_id)
            await ctx.publish({"event": "error", "message": str(exc)})
        finally:
            # 哨兵：通知 SSE 流结束
            await ctx.queue.put(None)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, run_id: str) -> RunContext | None:
        return self._runs.get(run_id)

    # ------------------------------------------------------------------
    # HITL：基于 LangGraph Command(resume=...)
    # ------------------------------------------------------------------
    async def resolve_hitl(self, run_id: str, request: HITLRequest) -> tuple[str, str]:
        """处理人工审批决策，通过 LangGraph Command(resume=...) 恢复图执行。

        返回 ``(status, message)``：``("resolved", ...)`` 或 ``("not_found", ...)``。
        """
        ctx = self._runs.get(run_id)
        if ctx is None:
            return ("not_found", f"run {run_id} 不存在")

        if ctx.status != "awaiting_hitl":
            return ("error", f"run {run_id} 不在等待 HITL 状态（当前: {ctx.status}）")

        try:
            # 通过 LangGraph Command(resume=...) 恢复执行
            from langgraph.types import Command

            graph = _get_graph()
            config = {"configurable": {"thread_id": ctx.thread_id}}

            resume_payload: dict[str, Any] = {"action": request.action}
            if request.action == "edit" and request.edited_output:
                resume_payload["edited_output"] = request.edited_output

            # 恢复执行并继续流式推送
            ctx.status = "running"
            asyncio.create_task(
                self._resume_drive(ctx, Command(resume=resume_payload), config)
            )
            return ("resolved", f"已收到 {request.action} 决策，Agent 正在恢复执行")
        except Exception as exc:  # noqa: BLE001
            ctx.status = "failed"
            _logger.exception("HITL 恢复失败 run %s", ctx.run_id)
            return ("error", f"恢复失败: {exc}")

    async def _resume_drive(
        self, ctx: RunContext, command: Any, config: dict[str, Any]
    ) -> None:
        """恢复中断的图执行并继续流式推送事件。"""
        try:
            graph = _get_graph()
            final_output = ctx.final_output or ""

            async for chunk in graph.astream(
                command, config, stream_mode="updates"
            ):
                if not isinstance(chunk, dict):
                    continue
                for node_name, update in chunk.items():
                    if node_name in (None, ""):
                        continue
                    await ctx.publish({"event": "node_start", "node": node_name})
                    await ctx.publish({"event": "node_end", "node": node_name})
                    if isinstance(update, dict):
                        out = update.get("final_output")
                        if out:
                            final_output = out

            # 再次检查是否还有中断
            interrupt_info = _check_interrupt(graph, config)
            if interrupt_info:
                ctx.status = "awaiting_hitl"
                ctx.hitl_interrupt = interrupt_info
                await ctx.publish(
                    {"event": "hitl_required", "run_id": ctx.run_id, "message": interrupt_info.get("message", "")}
                )
                return

            ctx.final_output = final_output or ""
            ctx.status = "completed"
            await ctx.publish({"event": "final_output", "output": ctx.final_output})
        except Exception as exc:  # noqa: BLE001
            ctx.status = "failed"
            ctx.error = str(exc)
            _logger.exception("HITL 恢复执行失败 run %s", ctx.run_id)
            await ctx.publish({"event": "error", "message": str(exc)})
        finally:
            await ctx.queue.put(None)


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------
def format_sse_event(event: dict[str, Any]) -> str:
    """将事件字典格式化为 SSE 数据帧字符串。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _get_graph() -> Any:
    """获取共享图实例。"""
    from lvyan.runtime import get_shared_graph
    return get_shared_graph()


def _check_interrupt(graph: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    """检查图是否有待处理的 LangGraph interrupt。

    Returns:
        中断信息字典（含 message 等），无中断返回 None。
    """
    try:
        snapshot = graph.get_state(config)
        if snapshot is None:
            return None
        # 有待执行节点 = 图被中断
        if snapshot.next:
            # 尝试获取 interrupt 详细信息
            tasks = getattr(snapshot, "tasks", None)
            if tasks:
                task_values = tasks.values() if isinstance(tasks, dict) else tasks
                for task in task_values:
                    interrupts = getattr(task, "interrupts", [])
                    if interrupts:
                        return interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            # 回退：返回通用中断信息
            return {
                "message": "Agent 执行中遇到需要人工确认的操作",
                "pending_nodes": list(snapshot.next),
            }
        return None
    except Exception:  # noqa: BLE001
        return None


def _build_fallback_output(state: dict[str, Any], query: str) -> str:
    """当图提前结束（如 ask_user 路由）时，生成用户友好的 fallback 输出。"""
    parts: list[str] = []

    case_type = state.get("case_type")
    if case_type:
        parts.append(f"**案件类型识别**：{case_type}\n")

    missing_facts = state.get("missing_facts", [])
    if missing_facts:
        parts.append("为了提供更准确的法律分析，请补充以下信息：\n")
        for i, mf in enumerate(missing_facts, 1):
            if isinstance(mf, dict):
                question = mf.get("question", "")
                reason = mf.get("reason", "")
            else:
                question = getattr(mf, "question", "")
                reason = getattr(mf, "reason", "")
            parts.append(f"{i}. **{question}**")
            if reason:
                parts.append(f"   _原因：{reason}_")
            parts.append("")

    facts = state.get("facts", [])
    if facts:
        parts.append("**已了解的事实**：")
        for f in facts:
            if isinstance(f, dict):
                content = f.get("content", "")
            else:
                content = getattr(f, "content", "")
            if content:
                parts.append(f"- {content}")
        parts.append("")

    if not parts:
        return (
            "我已收到您的问题，但在当前分析模式下无法生成完整回复。\n"
            "请尝试切换到**深度**模式，或提供更多细节信息。"
        )

    parts.append("---")
    parts.append("_以上为初步分析，补充信息后可获得更完整的法律意见。_")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 默认 runner：驱动共享 LangGraph 图并流式推送节点事件
# ---------------------------------------------------------------------------
async def default_runner(
    query: str, thread_id: str, complexity: str, ctx: RunContext
) -> str:
    """默认 runner：使用共享图实例运行 LangGraph，流式推送节点事件。

    PR1 改进：
    - 使用 ``get_shared_graph()`` 共享图实例（同一 checkpointer）。
    - 运行后检查 ``graph.get_state()`` 是否有 LangGraph interrupt。
    - 有中断时发布 ``hitl_required`` 事件，不关闭 SSE 流（等待 HITL 恢复）。
    - 注册 thread_id 到 CaseMemory 索引。
    """
    from datetime import date as _date

    from lvyan.observability.tracing import set_cost_thread
    from lvyan.runtime import get_case_memory
    from lvyan.schemas import CaseState

    set_cost_thread(thread_id)
    try:
        graph = _get_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # 注册到 CaseMemory 索引
        case_mem = get_case_memory()
        case_mem.register(
            thread_id,
            title=query[:40] if query else thread_id,
            complexity=complexity,
        )

        initial = CaseState(
            run_id=ctx.run_id,
            thread_id=thread_id,
            current_date=_date.today(),
            user_goal=query,
            complexity=complexity,
        )
        final_output = ""
        last_state: dict[str, Any] = {}

        async for chunk in graph.astream(
            initial.model_dump(), config, stream_mode="updates"
        ):
            if not isinstance(chunk, dict):
                continue
            for node_name, update in chunk.items():
                if node_name in (None, ""):
                    continue
                await ctx.publish({"event": "node_start", "node": node_name})
                await ctx.publish({"event": "node_end", "node": node_name})
                if isinstance(update, dict):
                    last_state.update(update)
                    out = update.get("final_output")
                    if out:
                        final_output = out

        # 检查是否有 LangGraph interrupt（HITL）
        interrupt_info = _check_interrupt(graph, config)
        if interrupt_info:
            ctx.status = "awaiting_hitl"
            ctx.hitl_interrupt = interrupt_info
            await ctx.publish(
                {
                    "event": "hitl_required",
                    "run_id": ctx.run_id,
                    "message": interrupt_info.get("message", "需要人工确认"),
                }
            )
            # 不关闭 SSE 流（不推送 None 哨兵），等待 HITL 恢复
            return ""

        # 如果图提前结束（如 ask_user 路由），生成 fallback 输出
        if not final_output:
            final_output = _build_fallback_output(last_state, query)

        # 标记会话已有输出
        case_mem.mark_output(thread_id)

        return final_output
    finally:
        set_cost_thread(None)


__all__ = [
    "RunContext",
    "RunManager",
    "Runner",
    "format_sse_event",
    "default_runner",
]
