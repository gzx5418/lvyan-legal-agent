"""SSE 事件分发与 Agent 运行管理。

``RunContext`` 封装单次 Agent 运行的状态：SSE 事件队列、HITL 同步原语、
最终输出。``RunManager`` 维护 run_id → RunContext 的注册表，并通过可注入的
``runner`` 异步驱动 Agent 运行。

设计要点
--------
- Agent 运行通过 ``asyncio.create_task`` 异步启动，不阻塞 HTTP 请求。
- SSE 流端点从 ``RunContext.queue`` 读取事件，遇到 ``None`` 哨兵即结束。
- HITL 端点通过 ``asyncio.Event`` 唤醒 ``await ctx.await_hitl()`` 等待中的 runner。
- ``runner`` 可注入，便于测试用 mock 替代真实图执行。
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
        self.hitl_event: asyncio.Event = asyncio.Event()
        self.hitl_response: HITLRequest | None = None
        self.final_output: str | None = None
        self.error: str | None = None

    async def publish(self, event: dict[str, Any]) -> None:
        """向 SSE 队列推送一个事件字典。"""
        await self.queue.put(event)

    async def await_hitl(self, timeout: float | None = None) -> HITLRequest:
        """等待人工审批响应；返回用户的 HITL 决策。

        调用此方法会将运行状态置为 ``awaiting_hitl``，直到
        :meth:`RunManager.resolve_hitl` 设置事件。
        """
        self.status = "awaiting_hitl"
        await self.publish({"event": "hitl_required", "run_id": self.run_id})
        if timeout is not None:
            await asyncio.wait_for(self.hitl_event.wait(), timeout=timeout)
        else:
            await self.hitl_event.wait()
        # type: ignore[next-line] 响应在 resolve_hitl 中被设置
        return self.hitl_response  # type: ignore[return-value]


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
    # HITL
    # ------------------------------------------------------------------
    async def resolve_hitl(self, run_id: str, request: HITLRequest) -> tuple[str, str]:
        """处理人工审批决策，唤醒等待中的 runner。

        返回 ``(status, message)``：``("resolved", ...)`` 或 ``("not_found", ...)``。
        """
        ctx = self._runs.get(run_id)
        if ctx is None:
            return ("not_found", f"run {run_id} 不存在")
        ctx.hitl_response = request
        ctx.hitl_event.set()
        ctx.status = "running"
        return ("resolved", f"已收到 {request.action} 决策")


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------
def format_sse_event(event: dict[str, Any]) -> str:
    """将事件字典格式化为 SSE 数据帧字符串。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _build_fallback_output(state: dict[str, Any], query: str) -> str:
    """当图提前结束（如 ask_user 路由）时，生成用户友好的 fallback 输出。

    场景：missing_fact_assessor 标记了 blocking 缺失事实 → 图路由到 END，
    但用户需要一个可读的回复而非空白。
    """
    parts: list[str] = []

    # 案件类型
    case_type = state.get("case_type")
    if case_type:
        parts.append(f"**案件类型识别**：{case_type}\n")

    # 缺失事实追问
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

    # 已提取的事实
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

    # 如果没有任何结构化信息，返回通用回复
    if not parts:
        return (
            "我已收到您的问题，但在当前分析模式下无法生成完整回复。\n"
            "请尝试切换到**深度**模式，或提供更多细节信息。"
        )

    parts.append("---")
    parts.append("_以上为初步分析，补充信息后可获得更完整的法律意见。_")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 默认 runner：驱动真实 LangGraph 图并流式推送节点事件
# ---------------------------------------------------------------------------
async def default_runner(
    query: str, thread_id: str, complexity: str, ctx: RunContext
) -> str:
    """默认 runner：构建并运行 LangGraph 图，流式推送节点事件。

    在真实 LLM/检索服务不可用时由各节点自行降级；本函数仅负责编排与事件推送。
    """
    # 延迟导入，避免 API 模块加载时即依赖 graph
    from datetime import date as _date

    from lvyan.graph import build_graph
    from lvyan.observability.tracing import set_cost_thread
    from lvyan.schemas import CaseState

    set_cost_thread(thread_id)
    try:
        graph = build_graph()
        config = {"configurable": {"thread_id": thread_id}}
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
            # chunk 形如 {"node_name": {state_update}}
            if not isinstance(chunk, dict):
                continue
            for node_name, update in chunk.items():
                if node_name in (None, ""):
                    continue
                await ctx.publish({"event": "node_start", "node": node_name})
                await ctx.publish({"event": "node_end", "node": node_name})
                if isinstance(update, dict):
                    # 合并到 last_state 用于后续 fallback
                    last_state.update(update)
                    out = update.get("final_output")
                    if out:
                        final_output = out

        # 如果图提前结束（如 ask_user 路由），生成 fallback 输出
        if not final_output:
            final_output = _build_fallback_output(last_state, query)

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
