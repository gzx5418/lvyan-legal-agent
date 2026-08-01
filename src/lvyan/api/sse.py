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
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from lvyan.memory.run_metadata import (
    RunMetadataStore,
    RunMetadataUnavailable,
    ThreadOwnershipError,
)

from .models import HITLRequest

_logger = logging.getLogger("lvyan.api.sse")

# runner 协议：async (query, thread_id, complexity, ctx) -> final_output(str)
Runner = Callable[[str, str, str, "RunContext"], Awaitable[str]]


class RunContext:
    """单次 Agent 运行的上下文。"""

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        user_id: str = "anonymous",
        law_as_of_date: date | None = None,
    ) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        # P2-13：归属用户；用于 stream / hitl 端点的 ownership 校验
        self.user_id: str = user_id
        self.law_as_of_date = law_as_of_date
        # 状态：started / running / awaiting_hitl / completed / failed / cancelled
        self.status: str = "started"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.final_output: str | None = None
        self.error: str | None = None
        self.non_recoverable: bool = False
        # HITL 中断信息（LangGraph interrupt 机制）
        self.hitl_interrupt: dict[str, Any] | None = None
        self.hitl_persisted: bool = False
        self._persist_hitl_callback: Callable[[dict[str, Any]], bool] | None = None
        # P3-24：TTL 摘要（用于 RunManager 自动清理过期运行）
        self.created_at: float = 0.0
        self.completed_at: float | None = None
        # P1-2：跨实例协作取消。``_cancel_check`` 由 RunManager 注入，
        # 返回 True 表示远端已请求取消；default_runner / _resume_drive 据此抛
        # ``asyncio.CancelledError``。``_last_cancel_poll`` 用于节流轮询频率。
        self._cancel_check: Callable[[], bool] | None = None
        self._last_cancel_poll: float = 0.0

    async def publish(self, event: dict[str, Any]) -> None:
        """发布一个 SSE 事件到队列，供流式消费者读取。"""
        await self.queue.put(event)

    def poll_cancel(self) -> bool:
        """节流地检查跨实例取消请求。间隔内重复调用直接返回 False。

        本地任务取消（asyncio.Task.cancel）仍由事件循环直接生效；本方法只
        处理「远端实例发起的取消请求」这一补充路径。
        """
        if self._cancel_check is None:
            return False
        now = time.time()
        from lvyan.config import settings as _settings

        if now - self._last_cancel_poll < _settings.cancel_poll_interval_seconds:
            return False
        self._last_cancel_poll = now
        try:
            return bool(self._cancel_check())
        except Exception as exc:  # noqa: BLE001 轮询失败不应中断运行
            _logger.debug("cancel 轮询失败 run %s: %s", self.run_id, exc)
            return False

    def persist_hitl_state(self, interrupt_info: dict[str, Any]) -> bool:
        """Persist HITL before exposing an approval action to the client."""
        if self._persist_hitl_callback is None:
            self.hitl_persisted = True
            return True
        self.hitl_persisted = bool(self._persist_hitl_callback(interrupt_info))
        return self.hitl_persisted


class RunManager:
    """Agent 运行注册表与驱动器。"""

    # P3-24：完成的运行在 _runs 中保留 1 小时后清理（可被 gc_runs 显式调用）
    _RUN_TTL_SECONDS: float = 3600.0

    def __init__(
        self,
        runner: Runner | None = None,
        metadata_store: RunMetadataStore | None = None,
    ) -> None:
        self._runs: dict[str, RunContext] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._runner: Runner | None = runner
        self._metadata_store = metadata_store

    def _bind_context(self, ctx: RunContext) -> RunContext:
        ctx._persist_hitl_callback = lambda interrupt_info: self._update_metadata(
            ctx.run_id,
            status="awaiting_hitl",
            interrupt_payload=interrupt_info,
        )
        # P1-2：注入跨实例取消轮询回调（同步 DB 查询，由 poll_cancel 节流）
        if self._metadata_store is not None:
            store = self._metadata_store
            uid = ctx.user_id

            def _cancel_check() -> bool:
                return store.is_cancel_requested(ctx.run_id, uid)

            ctx._cancel_check = _cancel_check
        return ctx

    def _update_metadata(self, run_id: str, **values: Any) -> bool:
        if self._metadata_store is None:
            return True
        try:
            self._metadata_store.update_run(run_id, **values)
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.warning("run metadata update failed for %s: %s", run_id, exc)
            ctx = self._runs.get(run_id)
            if ctx is not None:
                ctx.non_recoverable = True
            return False

    def _claim_hitl(
        self,
        run_id: str,
        user_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically claim a durable HITL run before issuing Command(resume)."""
        if self._metadata_store is None:
            return ("claimed", None)
        try:
            claimed = self._metadata_store.claim_hitl_run(run_id, user_id)
            if claimed is not None:
                return ("claimed", claimed)
            existing = self._metadata_store.get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("HITL claim failed for run %s", run_id)
            return ("unavailable", {"error": str(exc)})
        if existing is None:
            return ("not_found", None)
        if str(existing.get("user_id", "")) != user_id:
            return ("forbidden", existing)
        return ("conflict", existing)

    def _mark_thread_output(self, thread_id: str) -> bool:
        if self._metadata_store is None:
            return True
        try:
            self._metadata_store.mark_thread_output(thread_id)
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "thread metadata output mark failed for %s: %s",
                thread_id,
                exc,
            )
            return False

    def _append_message(
        self,
        ctx: RunContext,
        role: str,
        content: str,
        attachments: list[str] | None = None,
    ) -> bool:
        if self._metadata_store is None:
            return True
        try:
            self._metadata_store.append_message(
                ctx.run_id,
                ctx.thread_id,
                ctx.user_id,
                role,
                content,
                attachments,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "thread message persistence failed for run %s: %s",
                ctx.run_id,
                exc,
            )
            ctx.non_recoverable = True
            return False

    def _start_task(self, ctx: RunContext, awaitable: Awaitable[None]) -> None:
        task = asyncio.create_task(awaitable)
        self._tasks[ctx.run_id] = task
        task.add_done_callback(
            lambda completed, run_id=ctx.run_id: (
                self._tasks.pop(run_id, None) if self._tasks.get(run_id) is completed else None
            )
        )

    def has_active_thread_runs(self, thread_id: str) -> bool:
        """Return whether this process still owns an active run for a thread."""
        return any(
            ctx.thread_id == thread_id and ctx.status in {"started", "running", "awaiting_hitl"}
            for ctx in self._runs.values()
        )

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------
    def create_run(
        self,
        query: str,
        thread_id: str | None,
        complexity: str,
        user_id: str = "anonymous",
        law_as_of_date: date | None = None,
        attachments: list[str] | None = None,
        display_query: str | None = None,
    ) -> RunContext:
        """创建并异步启动一次 Agent 运行。"""
        import time as _time

        run_id = f"run-{uuid.uuid4().hex}"
        resolved_thread_id = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
        visible_query = display_query if display_query is not None else query
        ctx = self._bind_context(
            RunContext(
                run_id,
                resolved_thread_id,
                user_id=user_id,
                law_as_of_date=law_as_of_date,
            )
        )
        ctx.created_at = _time.time()
        self._runs[run_id] = ctx
        if self._metadata_store is not None:
            try:
                self._metadata_store.create_run(
                    run_id,
                    resolved_thread_id,
                    user_id,
                    title=visible_query[:40],
                    complexity=complexity,
                    user_message=visible_query,
                    attachments=attachments,
                )
            except ThreadOwnershipError:
                self._runs.pop(run_id, None)
                raise
            except Exception as exc:  # noqa: BLE001
                self._runs.pop(run_id, None)
                raise RunMetadataUnavailable(f"无法持久化 run metadata: {exc}") from exc
        self._start_task(ctx, self._drive(ctx, query, complexity))
        # 顺手做一次 TTL 清理
        self.gc_runs()
        return ctx

    async def _drive(self, ctx: RunContext, query: str, complexity: str) -> None:
        """驱动 runner 执行，捕获异常，最终推送 final_output 并关闭流。

        关键：runner 进入 ``awaiting_hitl`` 状态时不覆盖、不发送 final_output、
        不关闭 SSE 流，由 :meth:`_resume_drive` 在 HITL 决策后负责收尾。
        """
        ctx.status = "running"
        if not self._update_metadata(ctx.run_id, status="running"):
            await ctx.publish(
                {
                    "event": "warning",
                    "code": "run_non_recoverable",
                    "message": "运行状态无法持久化；服务重启后可能无法恢复",
                }
            )
        interrupted = False
        try:
            if self._runner is not None:
                output = await self._runner(query, ctx.thread_id, complexity, ctx)
            else:
                output = await default_runner(query, ctx.thread_id, complexity, ctx)

            if ctx.status == "failed":
                return

            # P0-1 修复：runner 主动进入 awaiting_hitl 时不覆盖状态
            if ctx.status == "awaiting_hitl":
                if not ctx.hitl_persisted and not ctx.persist_hitl_state(ctx.hitl_interrupt or {}):
                    ctx.status = "failed"
                    ctx.error = "无法持久化 HITL 状态，运行不可安全恢复"
                    await ctx.publish(
                        {
                            "event": "error",
                            "code": "hitl_persistence_failed",
                            "message": ctx.error,
                        }
                    )
                    return
                interrupted = True
                return

            ctx.final_output = output or ""
            ctx.status = "completed"
            ctx.completed_at = time.time()
            run_persisted = self._update_metadata(
                ctx.run_id,
                status="completed",
                final_output=ctx.final_output,
                completed_at=datetime.now(timezone.utc),
            )
            thread_marked = self._mark_thread_output(ctx.thread_id)
            message_persisted = self._append_message(
                ctx,
                "assistant",
                ctx.final_output,
            )
            if not run_persisted or not thread_marked or not message_persisted:
                await ctx.publish(
                    {
                        "event": "warning",
                        "code": "completion_not_persisted",
                        "message": "结果已生成，但持久化失败，请保存当前内容",
                    }
                )
            await ctx.publish({"event": "final_output", "output": ctx.final_output})
        except Exception as exc:  # noqa: BLE001 入口层需宽口径捕获
            ctx.status = "failed"
            ctx.error = str(exc)
            ctx.completed_at = time.time()
            self._update_metadata(
                ctx.run_id,
                status="failed",
                error=ctx.error,
                completed_at=datetime.now(timezone.utc),
            )
            self._append_message(ctx, "assistant", f"运行错误：{ctx.error}")
            _logger.exception("Agent run %s failed", ctx.run_id)
            await ctx.publish({"event": "error", "message": str(exc)})
        except asyncio.CancelledError:
            ctx.status = "cancelled"
            ctx.error = "用户已停止生成"
            self._update_metadata(ctx.run_id, status="cancelled", error=ctx.error)
            self._append_message(ctx, "assistant", ctx.error)
            await ctx.publish({"event": "cancelled", "message": ctx.error})
            raise
        finally:
            # 仅在非中断时关闭 SSE 流；HITL 由 _resume_drive 收尾
            if not interrupted:
                await ctx.queue.put(None)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, run_id: str) -> RunContext | None:
        return self._runs.get(run_id)

    def gc_runs(self, ttl_seconds: float | None = None) -> int:
        """清理已完成且超过 TTL 的运行记录，返回清理数量。

        P3-24：长时间运行的服务会持续累积 RunContext；本方法在
        ``create_run`` 时自动调用一次，亦可由外部定时器周期调用。

        P1-2：``cancelled`` 状态也纳入清理，避免取消的 RunContext 永久驻留。
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._RUN_TTL_SECONDS
        now = time.time()
        stale: list[str] = []
        for rid, ctx in self._runs.items():
            if ctx.status in ("completed", "failed", "cancelled"):
                completed = ctx.completed_at or ctx.created_at
                if completed and (now - completed) > ttl:
                    stale.append(rid)
        for rid in stale:
            self._runs.pop(rid, None)
        return len(stale)

    # ------------------------------------------------------------------
    # HITL：基于 LangGraph Command(resume=...)
    # ------------------------------------------------------------------
    async def resolve_hitl(
        self, run_id: str, request: HITLRequest, current_user_id: str = ""
    ) -> tuple[str, str]:
        """处理人工审批决策，通过 LangGraph Command(resume=...) 恢复图执行。

        P0-5：current_user_id 用于 checkpoint 恢复时的 ownership 校验。
        进程内 RunContext 已在 server 层校验过；checkpoint 恢复路径
        需要从 state metadata 中恢复 owner 并与当前请求比较。

        返回 ``(status, message)``：``("resolved", ...)`` 或 ``("not_found", ...)``。
        """
        ctx = self._runs.get(run_id)

        # P1-7：进程内无 run → 尝试从 checkpoint 恢复（服务重启 / 多实例场景）
        if ctx is None:
            return await self._resolve_hitl_from_checkpoint(run_id, request, current_user_id)

        if ctx.status != "awaiting_hitl":
            return ("error", f"run {run_id} 不在等待 HITL 状态（当前: {ctx.status}）")

        try:
            # 通过 LangGraph Command(resume=...) 恢复执行
            from langgraph.types import Command

            config = {"configurable": {"thread_id": ctx.thread_id}}

            resume_payload: dict[str, Any] = {"action": request.action}
            if request.action == "edit" and request.edited_output:
                resume_payload["edited_output"] = request.edited_output

            claim_status, _claim = self._claim_hitl(run_id, current_user_id)
            if claim_status == "forbidden":
                return ("forbidden", f"run {run_id} 不属于当前用户")
            if claim_status in ("conflict",):
                return ("error", f"run {run_id} 的审批已被处理或正在处理")
            if claim_status == "not_found":
                return ("not_found", f"run {run_id} 不存在")
            if claim_status == "unavailable":
                return ("unavailable", f"run {run_id} 的审批状态暂时不可用")

            # 恢复执行并继续流式推送
            ctx.status = "running"
            self._start_task(
                ctx,
                self._resume_drive(ctx, Command(resume=resume_payload), config),
            )
            return ("resolved", f"已收到 {request.action} 决策，Agent 正在恢复执行")
        except Exception as exc:  # noqa: BLE001
            ctx.status = "failed"
            _logger.exception("HITL 恢复失败 run %s", ctx.run_id)
            return ("error", f"恢复失败: {exc}")

    async def _resolve_hitl_from_checkpoint(
        self, run_id: str, request: HITLRequest, current_user_id: str = ""
    ) -> tuple[str, str]:
        """P1-7：从 PostgreSQL checkpoint 恢复 HITL 审批（跨实例/重启场景）。

        P0-5：恢复后执行 ownership 校验，防止跨租户审批。

        生产流程：
          1. 按 run_id 查询 agent_runs，直接取得 thread_id / user_id；
          2. 校验该 thread checkpoint 的 interrupt 与 state.run_id；
          3. 创建新的 RunContext 并恢复执行；
          4. 找不到则返回 not_found。
        未注入 metadata store 时仅保留本地开发兼容路径。
        """
        try:
            from langgraph.types import Command

            graph = _get_graph()
            run_meta: dict[str, Any] | None = None
            if self._metadata_store is not None:
                run_meta = self._metadata_store.get_run(run_id)
                if run_meta is None:
                    return ("not_found", f"run {run_id} 不存在")
                if run_meta.get("status") != "awaiting_hitl":
                    return (
                        "error",
                        f"run {run_id} 不在等待 HITL 状态"
                        f"（当前: {run_meta.get('status', 'unknown')}）",
                    )
                thread_candidates = [
                    (
                        str(run_meta["thread_id"]),
                        {
                            "user_id": str(run_meta["user_id"]),
                            "created_at": run_meta.get("created_at", 0.0),
                        },
                    )
                ]
            else:
                # 本地开发兼容路径；生产实例必须注入 PostgreSQL metadata store。
                thread_candidates = _get_case_memory().list_threads()

            for thread_id, meta in thread_candidates:
                config = {"configurable": {"thread_id": thread_id}}
                interrupt_info = _check_interrupt(graph, config)
                if interrupt_info is None:
                    continue

                snapshot = graph.get_state(config)
                if snapshot is None:
                    continue

                state_values = snapshot.values if hasattr(snapshot, "values") else {}
                state_run_id = (
                    state_values.get("run_id", "") if isinstance(state_values, dict) else ""
                )
                if state_run_id != run_id:
                    continue

                # 找到匹配 thread → P0-5 ownership 校验
                user_id = meta.get("user_id", "anonymous")
                from .auth import is_auth_enabled

                if is_auth_enabled() and user_id != current_user_id:
                    return ("forbidden", f"run {run_id} 不属于当前用户（owner={user_id}）")

                ctx = self._bind_context(RunContext(run_id, thread_id, user_id=user_id))
                ctx.status = "awaiting_hitl"
                ctx.hitl_interrupt = interrupt_info
                ctx.created_at = meta.get("created_at", 0.0)
                self._runs[run_id] = ctx

                resume_payload: dict[str, Any] = {"action": request.action}
                if request.action == "edit" and request.edited_output:
                    resume_payload["edited_output"] = request.edited_output

                claim_status, _claim = self._claim_hitl(run_id, current_user_id)
                if claim_status == "forbidden":
                    self._runs.pop(run_id, None)
                    return ("forbidden", f"run {run_id} 不属于当前用户")
                if claim_status == "conflict":
                    self._runs.pop(run_id, None)
                    return ("error", f"run {run_id} 的审批已被处理或正在处理")
                if claim_status == "not_found":
                    self._runs.pop(run_id, None)
                    return ("not_found", f"run {run_id} 不存在")
                if claim_status == "unavailable":
                    self._runs.pop(run_id, None)
                    return ("unavailable", f"run {run_id} 的审批状态暂时不可用")

                ctx.status = "running"
                self._start_task(
                    ctx,
                    self._resume_drive(ctx, Command(resume=resume_payload), config),
                )
                return ("resolved", f"已从 checkpoint 恢复 run {run_id}，正在继续执行")

            return ("not_found", f"run {run_id} 不存在（进程内和 checkpoint 均未找到）")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("HITL checkpoint 恢复失败 run %s", run_id)
            return ("error", f"checkpoint 恢复失败: {exc}")

    async def _resume_drive(self, ctx: RunContext, command: Any, config: dict[str, Any]) -> None:
        """恢复中断的图执行并继续流式推送事件。"""
        interrupted = False
        try:
            graph = _get_graph()
            final_output = ctx.final_output or ""

            final_output = await _stream_graph_events(
                graph,
                command,
                config,
                ctx,
                final_output=final_output,
            )

            # 再次检查是否还有中断 —— P0-2 三态 fail-closed
            icr = _check_interrupt_status(graph, config)
            if icr.status == "unavailable":
                ctx.status = "failed"
                ctx.error = "无法读取 checkpoint 判断 HITL 状态，运行不可安全完成"
                await ctx.publish(
                    {
                        "event": "error",
                        "code": "checkpoint_unavailable",
                        "message": ctx.error,
                    }
                )
                return
            if icr.status == "pending" and icr.payload:
                interrupt_info = icr.payload
                if not ctx.persist_hitl_state(interrupt_info):
                    ctx.status = "failed"
                    ctx.error = "无法持久化 HITL 状态，运行不可安全恢复"
                    await ctx.publish(
                        {
                            "event": "error",
                            "code": "hitl_persistence_failed",
                            "message": ctx.error,
                        }
                    )
                    return
                ctx.status = "awaiting_hitl"
                ctx.hitl_interrupt = interrupt_info
                interrupted = True
                await ctx.publish(
                    {
                        "event": "hitl_required",
                        "run_id": ctx.run_id,
                        "message": interrupt_info.get("message", ""),
                    }
                )
                return

            ctx.final_output = final_output or ""
            ctx.status = "completed"
            ctx.completed_at = time.time()
            run_persisted = self._update_metadata(
                ctx.run_id,
                status="completed",
                final_output=ctx.final_output,
                completed_at=datetime.now(timezone.utc),
            )
            thread_marked = self._mark_thread_output(ctx.thread_id)
            message_persisted = self._append_message(
                ctx,
                "assistant",
                ctx.final_output,
            )
            if not run_persisted or not thread_marked or not message_persisted:
                await ctx.publish(
                    {
                        "event": "warning",
                        "code": "completion_not_persisted",
                        "message": "结果已生成，但持久化失败，请保存当前内容",
                    }
                )

            # 标记会话已有输出
            try:
                case_mem = _get_case_memory()
                case_mem.mark_output(ctx.thread_id)
            except Exception:  # noqa: BLE001
                pass

            await ctx.publish({"event": "final_output", "output": ctx.final_output})
        except Exception as exc:  # noqa: BLE001
            ctx.status = "failed"
            ctx.error = str(exc)
            ctx.completed_at = time.time()
            self._update_metadata(
                ctx.run_id,
                status="failed",
                error=ctx.error,
                completed_at=datetime.now(timezone.utc),
            )
            self._append_message(ctx, "assistant", f"运行错误：{ctx.error}")
            _logger.exception("HITL 恢复执行失败 run %s", ctx.run_id)
            await ctx.publish({"event": "error", "message": str(exc)})
        except asyncio.CancelledError:
            ctx.status = "cancelled"
            ctx.error = "用户已停止生成"
            self._update_metadata(ctx.run_id, status="cancelled", error=ctx.error)
            self._append_message(ctx, "assistant", ctx.error)
            await ctx.publish({"event": "cancelled", "message": ctx.error})
            raise
        finally:
            if not interrupted:
                await ctx.queue.put(None)

    async def cancel_run(
        self,
        run_id: str,
        current_user_id: str,
    ) -> tuple[str, str]:
        """取消运行。

        P1-2 修复：
        - 持久化取消状态失败时返回 ``unavailable``（供 API 层映射为 503），
          不再吞掉 ``_update_metadata`` 的返回值。
        - 本实例无该 run 时，尝试在 metadata store 中写入 ``cancel_requested``
          标记，供运行该 run 的远端实例协作取消（跨实例取消的请求侧）。
        """
        ctx = self._runs.get(run_id)

        # 跨实例：本地没有该 run → 记录取消请求，让 owner 实例协作取消
        if ctx is None:
            if self._metadata_store is not None:
                try:
                    requested = self._metadata_store.request_cancel(
                        run_id, current_user_id
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("request_cancel 持久化失败 run %s: %s", run_id, exc)
                    return ("unavailable", f"无法记录取消请求：{exc}")
                if requested:
                    return (
                        "cancel_requested",
                        f"已请求取消 run {run_id}，远端实例将在下个轮询周期停止",
                    )
                # request_cancel 返回 False：run 不存在或不属于该用户
                return ("not_found", f"run {run_id} 不存在或不属于当前用户")
            return ("not_found", f"run {run_id} 不存在")

        if ctx.user_id != current_user_id:
            return ("forbidden", f"run {run_id} 不属于当前用户")
        if ctx.status not in {"started", "running", "awaiting_hitl"}:
            return ("conflict", f"run {run_id} 已处于 {ctx.status} 状态")

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if ctx.status != "cancelled":
            ctx.status = "cancelled"
            ctx.error = "用户已停止生成"
            ctx.completed_at = time.time()
            # P1-2：检查持久化结果；失败时返回 unavailable（API 层 → 503）
            persisted = self._update_metadata(
                run_id, status="cancelled", error=ctx.error
            )
            self._append_message(ctx, "assistant", ctx.error)
            await ctx.publish({"event": "cancelled", "message": ctx.error})
            await ctx.queue.put(None)
            if not persisted:
                return (
                    "unavailable",
                    "运行已在本实例停止，但持久化失败；重启后状态可能不一致",
                )
        return ("cancelled", "已停止生成")


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------
def format_sse_event(event: dict[str, Any]) -> str:
    """将事件字典格式化为 SSE 数据帧字符串。

    实现：始终只输出 ``data: <json>\n\n``，事件类型由 JSON 内的 ``event``
    字段携带。这样前端 ``EventSource.onmessage`` 与既有解析（按 ``data:``
    行读取并 JSON 解析）都能正常工作；前端用 ``data.event`` 字段做分发。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _stream_graph_events(
    graph: Any,
    source: Any,
    config: dict[str, Any],
    ctx: "RunContext",
    *,
    final_output: str = "",
    collect_state: dict[str, Any] | None = None,
) -> str:
    """统一处理 ``graph.astream`` 的事件流，推送 node_start/end/error 事件。

    被 ``default_runner``（source=initial_state）与 ``_resume_drive``
    （source=Command(resume=...)）共用，消除两处近似 50 行的重复逻辑。

    Args:
        graph: 共享 LangGraph 编译图。
        source: astream 的输入（CaseState.model_dump() 或 Command(resume=...)）。
        config: LangGraph 配置（含 thread_id）。
        ctx: RunContext，用于 ``publish`` SSE 事件。
        final_output: 初始的 final_output（用于 HITL 恢复时承接上文）。
        collect_state: 可选；若提供，会把 updates 写入该 dict（default_runner
            用于 fallback 输出生成）。

    Returns:
        流式过程中累积的 ``final_output``。
    """
    pending_starts: dict[str, float] = {}

    async for part in graph.astream(
        source,
        config,
        stream_mode=["updates", "tasks"],
        version="v2",
    ):
        # P1-2：跨实例协作取消。远端发起取消后，本 worker 在下个流事件察觉并终止。
        if ctx.poll_cancel():
            raise asyncio.CancelledError()
        if not isinstance(part, dict):
            continue
        mode = part.get("type", "")
        payload = part.get("data", part)

        if mode == "tasks" and isinstance(payload, dict):
            task_id = payload.get("id", "")
            task_name = payload.get("name", "")
            if not task_name:
                continue
            if "input" in payload:
                pending_starts[task_id or task_name] = time.time()
                await ctx.publish(
                    {
                        "event": "node_start",
                        "node": task_name,
                        "timestamp": pending_starts[task_id or task_name],
                    }
                )
            elif "result" in payload or "error" in payload:
                start_ts = pending_starts.pop(task_id or task_name, None)
                now = time.time()
                duration_ms = max(0.0, (now - (start_ts or now)) * 1000.0)
                await ctx.publish(
                    {
                        "event": "node_end",
                        "node": task_name,
                        "timestamp": now,
                        "duration_ms": round(duration_ms, 2),
                    }
                )
                error = payload.get("error")
                if error is not None:
                    await ctx.publish(
                        {
                            "event": "node_error",
                            "node": task_name,
                            "error": str(error),
                        }
                    )

        elif mode == "updates" and isinstance(payload, dict):
            for _node_name, update in payload.items():
                if isinstance(update, dict):
                    if collect_state is not None:
                        collect_state.update(update)
                    out = update.get("final_output")
                    if out:
                        final_output = out

    return final_output


def _get_graph() -> Any:
    """获取共享图实例。"""
    from lvyan.runtime import get_shared_graph

    return get_shared_graph()


def _get_case_memory() -> Any:
    """获取共享 CaseMemory 实例。"""
    from lvyan.runtime import get_case_memory

    return get_case_memory()


def _check_interrupt(graph: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    """检查图是否有待处理的 LangGraph interrupt（向后兼容包装）。

    保留旧二态语义（pending → dict / 否则 None），供不关心持久化故障的调用方使用。
    安全敏感路径（runner / HITL 恢复）应改用 :func:`_check_interrupt_status`，
    以区分「无中断」与「checkpoint 不可读」（P0-2 fail-closed）。
    """
    result = _check_interrupt_status(graph, config)
    if result.status == "pending":
        return result.payload
    return None


class InterruptCheckResult:
    """P0-2：中断检查的三态结果。

    - ``pending``：存在待审批 interrupt，``payload`` 为中断信息。
    - ``none``：明确无 interrupt（checkpoint 正常读取且无待执行节点）。
    - ``unavailable``：checkpoint 读取失败。安全护栏已触发但无法读取时，
      必须 fail-closed：调用方不得发送 final_output / 标记 completed。
    """

    __slots__ = ("status", "payload")

    def __init__(self, status: str, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self.payload = payload


def _check_interrupt_status(graph: Any, config: dict[str, Any]) -> InterruptCheckResult:
    """三态中断检查（P0-2 fail-closed）。

    checkpoint 读取异常时返回 ``unavailable``，不再被静默当作「无中断」。
    """
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 checkpoint 故障
        _logger.warning("check_interrupt 读取 checkpoint 失败，按 fail-closed 处理: %s", exc)
        return InterruptCheckResult("unavailable", {"error": str(exc)})

    if snapshot is None:
        return InterruptCheckResult("none")
    # 有待执行节点 = 图被中断
    if snapshot.next:
        tasks = getattr(snapshot, "tasks", None)
        if tasks:
            task_values = tasks.values() if isinstance(tasks, dict) else tasks
            for task in task_values:
                interrupts = getattr(task, "interrupts", [])
                if interrupts:
                    payload = (
                        interrupts[0].value
                        if hasattr(interrupts[0], "value")
                        else interrupts[0]
                    )
                    return InterruptCheckResult("pending", payload)
        return InterruptCheckResult(
            "pending",
            {
                "message": "Agent 执行中遇到需要人工确认的操作",
                "pending_nodes": list(snapshot.next),
            },
        )
    return InterruptCheckResult("none")


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
async def default_runner(query: str, thread_id: str, complexity: str, ctx: RunContext) -> str:
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
            user_id=ctx.user_id,
        )

        initial = CaseState(
            run_id=ctx.run_id,
            thread_id=thread_id,
            current_date=_date.today(),
            user_goal=query,
            complexity=complexity,
            user_id=ctx.user_id,
            law_as_of_date=ctx.law_as_of_date,
        )
        final_output = ""
        last_state: dict[str, Any] = {}

        # P0-1 修复：使用 version="v2" + stream_mode=["updates", "tasks"]
        # v2 格式统一返回 {"type": ..., "data": ...} 字典；
        # v1 多 stream mode 返回 (mode, data) 元组，会被 isinstance(dict) 跳过。
        # "tasks" 流提供节点任务的开始/完成/错误事件，比 "debug" 更语义化。
        try:
            final_output = await _stream_graph_events(
                graph,
                initial.model_dump(),
                config,
                ctx,
                final_output=final_output,
                collect_state=last_state,
            )
        except TypeError as exc:  # v2 参数不兼容 → 回退 v1 updates
            if "version" not in str(exc):
                raise
            async for chunk in graph.astream(initial.model_dump(), config, stream_mode="updates"):
                if not isinstance(chunk, dict):
                    continue
                for _node_name, update in chunk.items():
                    if isinstance(update, dict):
                        last_state.update(update)
                        out = update.get("final_output")
                        if out:
                            final_output = out

        # 检查是否有 LangGraph interrupt（HITL）—— P0-2 三态 fail-closed
        icr = _check_interrupt_status(graph, config)
        if icr.status == "unavailable":
            # checkpoint 不可读：不得假定无 interrupt，不得发送 final_output
            ctx.status = "failed"
            ctx.error = "无法读取 checkpoint 判断 HITL 状态，运行不可安全完成"
            await ctx.publish(
                {
                    "event": "error",
                    "code": "checkpoint_unavailable",
                    "message": ctx.error,
                }
            )
            return ""
        if icr.status == "pending" and icr.payload:
            interrupt_info = icr.payload
            if not ctx.persist_hitl_state(interrupt_info):
                ctx.status = "failed"
                ctx.error = "无法持久化 HITL 状态，运行不可安全恢复"
                await ctx.publish(
                    {
                        "event": "error",
                        "code": "hitl_persistence_failed",
                        "message": ctx.error,
                    }
                )
                return ""
            ctx.status = "awaiting_hitl"
            ctx.hitl_interrupt = interrupt_info
            # The persisted run_id -> thread_id mapping is the cross-instance
            # discovery path; the checkpoint remains LangGraph's state source.
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
