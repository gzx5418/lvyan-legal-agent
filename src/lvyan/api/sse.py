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

P2-5：``RunContext`` 已拆分到 ``run_context.py``，本模块重导出以保持向后兼容。
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
from .run_context import RunContext, Runner  # P2-5：从拆分模块导入

_logger = logging.getLogger("lvyan.api.sse")


class RunManager:
    """Agent 运行注册表与驱动器。"""

    # P3-24：完成的运行在 _runs 中保留 1 小时后清理（可被 gc_runs 显式调用）
    _RUN_TTL_SECONDS: float = 3600.0
    # W3：awaiting_hitl 状态使用 24 小时 TTL，避免用户长时间不审批导致内存泄漏
    _HITL_TTL_SECONDS: float = 86400.0

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

    async def _fail_run(
        self,
        ctx: RunContext,
        *,
        code: str = "run_failed",
        message: str | None = None,
        publish: bool = True,
    ) -> None:
        """P0-2：统一失败收尾。

        确保所有 ``failed`` 路径都同步写回数据库终态（``status='failed'`` +
        ``error`` + ``completed_at``），避免 run 永久停在 ``running`` 导致
        ``has_active_runs`` 永远为 true、thread 无法删除。

        Args:
            code: SSE 错误事件中的 ``code`` 字段。
            message: 错误消息；为 None 时使用 ``ctx.error``。
            publish: 是否推送 SSE error 事件（runner 已推送过的路径设为 False）。
        """
        msg = message or ctx.error or "运行失败"
        ctx.status = "failed"
        ctx.error = msg
        ctx.completed_at = time.time()
        persisted = self._update_metadata(
            ctx.run_id,
            status="failed",
            error=msg,
            completed_at=datetime.now(timezone.utc),
        )
        if not persisted:
            ctx.non_recoverable = True
        if publish:
            await ctx.publish(
                {
                    "event": "error",
                    "code": code,
                    "message": msg,
                }
            )

    async def _cancel_context(
        self,
        ctx: RunContext,
        *,
        code: str = "cancelled",
        message: str = "用户已停止生成",
    ) -> tuple[str, str]:
        """P1-5 / P0-1：统一取消收尾。

        确保所有取消路径（``_drive`` 的 CancelledError、``_resume_drive`` 的
        CancelledError、``cancel_run`` 的本地手动取消、``awaiting_hitl`` 远端
        取消同步）都：
        1. 设置 ``ctx.status = "cancelled"`` + ``completed_at``；
        2. 写回数据库终态（``status='cancelled'`` + ``error`` + ``completed_at``）；
        3. 推送 ``cancelled`` SSE 事件并关闭队列；
        4. 返回 ``(status, message)``，持久化失败时返回 ``unavailable``。
        """
        ctx.status = "cancelled"
        ctx.error = message
        ctx.completed_at = time.time()
        persisted = self._update_metadata(
            ctx.run_id,
            status="cancelled",
            error=message,
            completed_at=datetime.now(timezone.utc),
        )
        self._append_message(ctx, "assistant", message)
        await ctx.publish({"event": "cancelled", "message": message})
        await ctx.queue.put(None)
        if not persisted:
            return (
                "unavailable",
                "运行已停止，但持久化失败；重启后状态可能不一致",
            )
        return ("cancelled", "已停止生成")

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

    def _start_cancel_watcher(self, ctx: RunContext) -> asyncio.Task[Any] | None:
        """P1-4：启动独立 cancel watcher，周期性检查远端取消请求。

        即使 graph 长时间无事件产生（如模型调用卡住），watcher 也能发现取消
        并取消主任务。watcher 在 ``_drive`` / ``_resume_drive`` 的 finally 块中
        被 :meth:`_stop_cancel_watcher` 停止。
        """
        if ctx._cancel_check is None:
            return None

        async def _watch() -> None:
            from lvyan.config import settings as _settings

            interval = _settings.cancel_poll_interval_seconds
            while True:
                await asyncio.sleep(interval)
                if await ctx.poll_cancel_async():
                    # 取消主运行任务
                    main_task = self._tasks.get(ctx.run_id)
                    if main_task is not None and not main_task.done():
                        main_task.cancel()
                    return

        return asyncio.create_task(_watch())

    def _stop_cancel_watcher(self, watcher: asyncio.Task[Any] | None) -> None:
        """P1-4：停止 cancel watcher 任务。"""
        if watcher is not None and not watcher.done():
            watcher.cancel()

    def has_active_thread_runs(self, thread_id: str) -> bool:
        """Return whether this process still owns an active run for a thread.

        P0-1：本地 ``ctx.status`` 可能与数据库终态不一致（远端实例已把
        ``awaiting_hitl`` 直接终结为 ``cancelled``，但本地仍为
        ``awaiting_hitl``）。在判断前先与数据库协调：若数据库已为终态，
        同步本地状态并返回 False。
        """
        for ctx in list(self._runs.values()):
            if ctx.thread_id != thread_id:
                continue
            if ctx.status not in {"started", "running", "awaiting_hitl"}:
                continue
            # P0-1：与数据库协调，检测远端取消
            if self._metadata_store is not None:
                try:
                    run_meta = self._metadata_store.get_run(ctx.run_id)
                except Exception as exc:  # noqa: BLE001
                    _logger.debug("has_active_thread_runs 查询失败: %s", exc)
                    return True  # fail-open：DB 不可达时保守返回 True
                if run_meta is not None:
                    db_status = run_meta.get("status")
                    if db_status in {"cancelled", "failed", "completed", "abandoned"}:
                        # 同步本地终态
                        ctx.status = db_status
                        if db_status == "cancelled":
                            ctx.error = run_meta.get("error") or "用户已停止生成"
                        ctx.completed_at = time.time()
                        continue
            return True
        return False

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
        attachment_refs: list[dict] | None = None,
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
                attachment_refs=attachment_refs,
                load_history=self._make_history_loader(resolved_thread_id, user_id),
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

    def _make_history_loader(self, thread_id: str, user_id: str) -> Any:
        """构造读取本 thread 历史消息的闭包。

        返回的 callable 签名 ``() -> list[dict]``；无 metadata_store 时返回
        ``None``（runner 据此跳过历史注入）。
        """
        if self._metadata_store is None:
            return None

        store = self._metadata_store

        def _load() -> list[dict]:
            try:
                return store.list_messages(thread_id, user_id)
            except Exception:  # noqa: BLE001  历史读取失败不阻断主流程
                return []

        return _load

    async def _drive(self, ctx: RunContext, query: str, complexity: str) -> None:
        """驱动 runner 执行，捕获异常，最终推送 final_output 并关闭流。

        关键：runner 进入 ``awaiting_hitl`` 状态时不覆盖、不发送 final_output、
        不关闭 SSE 流，由 :meth:`_resume_drive` 在 HITL 决策后负责收尾。

        P1-4：启动独立 cancel watcher 任务，不依赖 graph 事件产生即可取消。
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
        # P1-4：独立 cancel watcher —— 即使 graph 长时间无事件也能取消
        cancel_watcher = self._start_cancel_watcher(ctx)
        try:
            if self._runner is not None:
                output = await self._runner(query, ctx.thread_id, complexity, ctx)
            else:
                output = await default_runner(query, ctx.thread_id, complexity, ctx)

            if ctx.status == "failed":
                # P0-2：runner 已设置 failed（如 checkpoint unavailable），
                # 这里确保数据库终态同步，不推送重复事件。
                await self._fail_run(
                    ctx,
                    code=ctx.fail_code or "runner_failed",
                    publish=False,
                )
                return

            # P0-1 修复：runner 主动进入 awaiting_hitl 时不覆盖状态
            if ctx.status == "awaiting_hitl":
                if not ctx.hitl_persisted and not ctx.persist_hitl_state(ctx.hitl_interrupt or {}):
                    await self._fail_run(
                        ctx,
                        code="hitl_persistence_failed",
                        message="无法持久化 HITL 状态，运行不可安全恢复",
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
                legal_answer=ctx.legal_answer,
                document_file=ctx.document_file,
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
            await ctx.publish(_build_final_output_event(ctx))
        except Exception as exc:  # noqa: BLE001 入口层需宽口径捕获
            await self._fail_run(ctx, code="run_exception", message=str(exc))
            self._append_message(ctx, "assistant", f"运行错误：{ctx.error}")
            _logger.exception("Agent run %s failed", ctx.run_id)
        except asyncio.CancelledError:
            # P1-5：统一取消收尾，确保 DB 写入 completed_at
            await self._cancel_context(ctx, message="用户已停止生成")
            raise
        finally:
            # P1-4：停止 cancel watcher
            self._stop_cancel_watcher(cancel_watcher)
            # 仅在非中断且未取消时关闭 SSE 流（取消路径由 _cancel_context 关闭）
            if not interrupted and ctx.status not in {"cancelled", "failed"}:
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

        W3 修复：``awaiting_hitl`` 状态使用独立的更长 TTL
        （``_HITL_TTL_SECONDS``，默认 24 小时）。用户若长时间不回复审批，
        该 RunContext 仍会被清理，避免内存泄漏。终态（completed / failed /
        cancelled）使用默认 TTL（1 小时）。
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._RUN_TTL_SECONDS
        now = time.time()
        stale: list[str] = []
        for rid, ctx in self._runs.items():
            if ctx.status in ("completed", "failed", "cancelled"):
                completed = ctx.completed_at or ctx.created_at
                if completed and (now - completed) > ttl:
                    stale.append(rid)
            elif ctx.status == "awaiting_hitl":
                # W3：awaiting_hitl 使用更长 TTL，避免用户长时间不审批导致内存泄漏
                created = ctx.created_at or now
                if (now - created) > self._HITL_TTL_SECONDS:
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

        claimed = False  # P0-2：追踪 claim 是否成功，用于异常回滚
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

            # P0-2：claim 已成功（DB: awaiting_hitl→running），后续启动失败必须回滚 DB
            claimed = True
            # 恢复执行并继续流式推送
            ctx.status = "running"
            resume_awaitable = self._resume_drive(ctx, Command(resume=resume_payload), config)
            try:
                self._start_task(ctx, resume_awaitable)
            except BaseException:
                # 创建协程后若任务调度失败，必须显式关闭，避免资源泄漏警告。
                resume_awaitable.close()
                raise
            return ("resolved", f"已收到 {request.action} 决策，Agent 正在恢复执行")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("HITL 恢复失败 run %s", ctx.run_id)
            # P0-2：claim 成功后启动失败 → 统一回滚 DB 终态
            if claimed:
                await self._fail_run(
                    ctx,
                    code="hitl_resume_start_failed",
                    message=str(exc),
                    publish=False,
                )
            else:
                ctx.status = "failed"
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

            graph = await _get_graph()
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

            claimed = False  # P0-2：追踪 claim 是否成功
            for thread_id, meta in thread_candidates:
                config = {"configurable": {"thread_id": thread_id}}
                interrupt_info = await _check_interrupt_async(graph, config)
                if interrupt_info is None:
                    continue

                snapshot = await graph.aget_state(config)
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

                # P0-2：claim 成功，后续启动失败必须回滚 DB
                claimed = True
                ctx.status = "running"
                resume_awaitable = self._resume_drive(ctx, Command(resume=resume_payload), config)
                try:
                    self._start_task(ctx, resume_awaitable)
                except BaseException:
                    resume_awaitable.close()
                    raise
                return ("resolved", f"已从 checkpoint 恢复 run {run_id}，正在继续执行")

            return ("not_found", f"run {run_id} 不存在（进程内和 checkpoint 均未找到）")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("HITL checkpoint 恢复失败 run %s", run_id)
            # P0-2：claim 成功后启动失败 → 统一回滚 DB 终态
            if claimed and ctx is not None:
                await self._fail_run(
                    ctx,
                    code="hitl_resume_start_failed",
                    message=str(exc),
                    publish=False,
                )
            return ("error", f"checkpoint 恢复失败: {exc}")

    async def _resume_drive(self, ctx: RunContext, command: Any, config: dict[str, Any]) -> None:
        """恢复中断的图执行并继续流式推送事件。

        P1-4：启动独立 cancel watcher，不依赖 graph 事件产生即可取消。
        """
        interrupted = False
        # P1-4：独立 cancel watcher
        cancel_watcher = self._start_cancel_watcher(ctx)
        try:
            graph = await _get_graph()
            final_output = ctx.final_output or ""

            final_output, legal_answer, document_file = await _stream_graph_events(
                graph,
                command,
                config,
                ctx,
                final_output=final_output,
                document_file=ctx.document_file,
            )
            ctx.legal_answer = legal_answer
            ctx.document_file = document_file

            # 再次检查是否还有中断 —— P0-2 三态 fail-closed
            icr = await _check_interrupt_status_async(graph, config)
            if icr.status == "unavailable":
                await self._fail_run(
                    ctx,
                    code="checkpoint_unavailable",
                    message="无法读取 checkpoint 判断 HITL 状态，运行不可安全完成",
                )
                return
            if icr.status == "pending" and icr.payload:
                interrupt_info = icr.payload
                if not ctx.persist_hitl_state(interrupt_info):
                    await self._fail_run(
                        ctx,
                        code="hitl_persistence_failed",
                        message="无法持久化 HITL 状态，运行不可安全恢复",
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
                legal_answer=ctx.legal_answer,
                document_file=ctx.document_file,
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

            await ctx.publish(_build_final_output_event(ctx))
        except Exception as exc:  # noqa: BLE001
            await self._fail_run(ctx, code="hitl_resume_exception", message=str(exc))
            self._append_message(ctx, "assistant", f"运行错误：{ctx.error}")
            _logger.exception("HITL 恢复执行失败 run %s", ctx.run_id)
        except asyncio.CancelledError:
            # P1-5：统一取消收尾，确保 DB 写入 completed_at
            await self._cancel_context(ctx, message="用户已停止生成")
            raise
        finally:
            # P1-4：停止 cancel watcher
            self._stop_cancel_watcher(cancel_watcher)
            # 仅在非中断且未取消时关闭 SSE 流（取消路径由 _cancel_context 关闭）
            if not interrupted and ctx.status not in {"cancelled", "failed"}:
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

        P0-1 改进：
        - ``request_cancel`` 返回三态；``cancelled_immediately`` 表示远端已把
          ``awaiting_hitl`` 直接终结。若本地仍持有该 RunContext（awaiting_hitl），
          需同步本地状态、推送 cancelled SSE、关闭队列，避免前端永久等待。
        """
        ctx = self._runs.get(run_id)

        # 跨实例：本地没有该 run → 记录取消请求，让 owner 实例协作取消
        if ctx is None:
            if self._metadata_store is not None:
                try:
                    result = self._metadata_store.request_cancel(run_id, current_user_id)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("request_cancel 持久化失败 run %s: %s", run_id, exc)
                    return ("unavailable", f"无法记录取消请求：{exc}")
                if result == "cancelled_immediately":
                    return (
                        "cancelled",
                        f"run {run_id} 已直接取消（awaiting_hitl 远端终结）",
                    )
                if result == "cancel_requested":
                    return (
                        "cancel_requested",
                        f"已请求取消 run {run_id}，远端实例将在下个轮询周期停止",
                    )
                # not_found：run 不存在或不属于该用户
                return ("not_found", f"run {run_id} 不存在或不属于当前用户")
            return ("not_found", f"run {run_id} 不存在")

        if ctx.user_id != current_user_id:
            return ("forbidden", f"run {run_id} 不属于当前用户")
        if ctx.status not in {"started", "running", "awaiting_hitl"}:
            return ("conflict", f"run {run_id} 已处于 {ctx.status} 状态")

        # P0-1：awaiting_hitl 状态下主任务已结束、watcher 已停止，需直接同步本地
        if ctx.status == "awaiting_hitl":
            return await self._cancel_context(ctx, code="cancelled")

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if ctx.status != "cancelled":
            return await self._cancel_context(ctx, code="cancelled")
        return ("cancelled", "已停止生成")


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------
def _build_final_output_event(ctx: "RunContext") -> dict[str, Any]:
    """构建 final_output 事件。

    P0-3 去重：不再单独发送 ``markdown_fallback``。``output`` 字段同时承担
    「旧前端 Markdown 来源」与「新前端 fallback 来源」两种角色，避免同一份
    完整 Markdown 在单次 SSE 中被发送两次。

    P1-1：``document_file`` 只暴露 **public 视图**（filename / format / file_size /
    download_url），绝不包含 ``output_path`` 等服务器内部路径。
    """
    event: dict[str, Any] = {"event": "final_output", "output": ctx.final_output}
    if ctx.legal_answer:
        event["schema_version"] = ctx.legal_answer.get("schema_version", "legal_answer_v1")
        event["answer"] = ctx.legal_answer
    if ctx.document_file and ctx.document_file.get("success"):
        event["document_file"] = {
            "filename": ctx.document_file.get("filename") or "法律文书.docx",
            "format": ctx.document_file.get("format") or "docx",
            "file_size": ctx.document_file.get("file_size") or 0,
            "download_url": f"/api/documents/{ctx.run_id}/download",
        }
    return event


def format_sse_event(event: dict[str, Any]) -> str:
    """将事件字典格式化为 SSE 数据帧字符串。

    实现：始终只输出 ``data: <json>\n\n``，事件类型由 JSON 内的 ``event``
    字段携带。这样前端 ``EventSource.onmessage`` 与既有解析（按 ``data:``
    行读取并 JSON 解析）都能正常工作；前端用 ``data.event`` 字段做分发。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# P2：用户语义阶段（前端进度条不再关心 LangGraph 节点数）
# ---------------------------------------------------------------------------
# key 为稳定英文标识，label 为可直接展示的中文文案（前端无需维护映射表，
# 彻底消除「前端 12 / 后端注释 13 / 实际 14」这类节点数漂移）。
PHASE_TOTAL = 6
PHASE_MAP: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "material_reading",
        "读取材料",
        ("preflight", "attachment_retriever"),
    ),
    (
        "fact_analysis",
        "梳理案情",
        ("jurisdiction_triage", "fact_extractor", "missing_fact_assessor", "planner"),
    ),
    ("retrieval", "检索法律", ("parallel_retrieval", "authority_resolver")),
    ("analysis", "分析争点", ("legal_reasoner", "critic")),
    ("drafting_validation", "起草与校验", ("composer", "citation_verifier", "output_guardrail")),
    ("generation", "生成结果", ("legal_answer_finalizer",)),
)
_NODE_PHASE_IDX: dict[str, int] = {
    node: idx for idx, (_key, _label, nodes) in enumerate(PHASE_MAP) for node in nodes
}


async def _publish_phase_progress(ctx: "RunContext", completed_index: int) -> None:
    """发布阶段完成事件（completed 为 1 基累计数）。"""
    phase_key, phase_label, _nodes = PHASE_MAP[completed_index]
    await ctx.publish(
        {
            "event": "phase_progress",
            "phase": phase_key,
            "label": phase_label,
            "completed": completed_index + 1,
            "total": PHASE_TOTAL,
        }
    )


async def _stream_graph_events(
    graph: Any,
    source: Any,
    config: dict[str, Any],
    ctx: "RunContext",
    *,
    final_output: str = "",
    document_file: dict[str, Any] | None = None,
    collect_state: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """统一处理 ``graph.astream`` 的事件流，推送 node_start/end/error 事件。

    被 ``default_runner``（source=initial_state）与 ``_resume_drive``
    （source=Command(resume=...)）共用，消除两处近似 50 行的重复逻辑。

    Args:
        graph: 共享 LangGraph 编译图。
        source: astream 的输入（CaseState.model_dump() 或 Command(resume=...)）。
        config: LangGraph 配置（含 thread_id）。
        ctx: RunContext，用于 ``publish`` SSE 事件。
        final_output: 初始的 final_output（用于 HITL 恢复时承接上文）。
        document_file: 初始的 document_file（用于 HITL 恢复时承接上文）。
        collect_state: 可选；若提供，会把 updates 写入该 dict（default_runner
            用于 fallback 输出生成）。

    Returns:
        ``(final_output, legal_answer, document_file)``：流式过程中累积的
        Markdown 输出、结构化 LegalAnswerV1 dict（若 composer 已写入）与
        文书文件元数据（document 模式 finalizer 渲染的 DOCX 信息）。
    """
    pending_starts: dict[str, float] = {}
    legal_answer: dict[str, Any] | None = None
    # P2：语义阶段进度（仅向前推进：重跑旧阶段节点不使进度回退）
    max_phase_idx = -1
    completed_phases = -1

    async for part in graph.astream(
        source,
        config,
        stream_mode=["updates", "tasks"],
        version="v2",
    ):
        # P1-2/P1-3：跨实例协作取消（异步轮询，不阻塞 event loop）
        if await ctx.poll_cancel_async():
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
                # P2：阶段推进 —— 新阶段的首个节点启动时，补齐已完成的中间阶段
                # 并发布当前阶段启动事件。
                phase_idx = _NODE_PHASE_IDX.get(task_name, -1)
                if phase_idx > max_phase_idx:
                    while completed_phases < phase_idx - 1:
                        completed_phases += 1
                        await _publish_phase_progress(ctx, completed_phases)
                    max_phase_idx = phase_idx
                    phase_key, phase_label, _nodes = PHASE_MAP[phase_idx]
                    await ctx.publish(
                        {
                            "event": "phase_start",
                            "phase": phase_key,
                            "label": phase_label,
                            "index": phase_idx + 1,
                            "total": PHASE_TOTAL,
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
                    # P0-A 修复：使用 "key in update" 显式覆盖，而非 truthy 判断。
                    # legal_answer_finalizer 在 HITL edited / 校验失败 / 隐私失败时会
                    # 显式返回 legal_answer=None（清空不可信的结构化答案）。若用
                    # ``if la:`` 判断，None 是 falsy 不会覆盖，导致 composer 旧版
                    # 结构化答案残留在内存中，前端继续收到 stale answer。
                    if "final_output" in update:
                        final_output = update["final_output"]
                    if "legal_answer" in update:
                        legal_answer = update["legal_answer"]
                    if "document_file" in update:
                        document_file = update["document_file"]

    # 图正常结束后，最后一个已启动阶段也必须被标记完成。否则最终阶段没有
    # 后继阶段触发 progress，UI 会停在 5/6（83%）。
    if max_phase_idx >= 0:
        while completed_phases < max_phase_idx:
            completed_phases += 1
            await _publish_phase_progress(ctx, completed_phases)

    return final_output, legal_answer, document_file


async def _get_graph() -> Any:
    """获取共享图实例（异步路径，使用 AsyncPostgresSaver）。

    必须在运行的事件循环中 ``await`` 调用，确保 ``AsyncConnection`` 绑定到
    当前循环。同步 CLI 路径请直接使用 ``runtime.get_shared_graph()``。
    """
    from lvyan.runtime import get_shared_graph_async

    return await get_shared_graph_async()


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

    注意：本函数使用同步 ``graph.get_state()``，仅适用于同步 ``PostgresSaver``
    或 ``MemorySaver``。使用 ``AsyncPostgresSaver`` 的 API 路径请用
    :func:`_check_interrupt_status_async`。
    """
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 checkpoint 故障
        _logger.warning("check_interrupt 读取 checkpoint 失败，按 fail-closed 处理: %s", exc)
        return InterruptCheckResult("unavailable", {"error": str(exc)})

    return _parse_interrupt_snapshot(snapshot)


async def _check_interrupt_status_async(graph: Any, config: dict[str, Any]) -> InterruptCheckResult:
    """三态中断检查（异步版本，API 路径用）。

    使用 ``await graph.aget_state()``，兼容 ``AsyncPostgresSaver``。
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001 checkpoint 故障
        _logger.warning("check_interrupt 读取 checkpoint 失败，按 fail-closed 处理: %s", exc)
        return InterruptCheckResult("unavailable", {"error": str(exc)})

    return _parse_interrupt_snapshot(snapshot)


def _parse_interrupt_snapshot(snapshot: Any) -> InterruptCheckResult:
    """从 graph.get_state / aget_state 返回的 snapshot 解析中断状态。"""
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
                        interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
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


async def _check_interrupt_async(graph: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    """检查图是否有待处理的 LangGraph interrupt（异步版本）。

    保留旧二态语义（pending → dict / 否则 None），供不关心持久化故障的调用方使用。
    """
    result = await _check_interrupt_status_async(graph, config)
    if result.status == "pending":
        return result.payload
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
    from lvyan.schemas import CaseState, DocumentRef
    from lvyan.tools.conversation_history import format_conversation_summary

    set_cost_thread(thread_id)
    try:
        graph = await _get_graph()
        config = {"configurable": {"thread_id": thread_id}}

        # 注册到 CaseMemory 索引
        case_mem = get_case_memory()
        case_mem.register(
            thread_id,
            title=query[:40] if query else thread_id,
            complexity=complexity,
            user_id=ctx.user_id,
        )

        # P0 性能：附件以 DocumentRef 形式注入 state，不再拼进 user_goal；
        # attachment_retriever 节点会切块并按需检索相关段落写入 relevant_attachment_context。
        uploaded_docs = []
        for ref in ctx.attachment_refs:
            try:
                uploaded_docs.append(DocumentRef(**ref))
            except Exception:  # noqa: BLE001
                continue

        # 多轮记忆：读取本 thread 历史，格式化为紧凑摘要注入初始 state。
        history_msgs = ctx.load_history() if ctx.load_history else []
        conversation_summary = format_conversation_summary(history_msgs) if history_msgs else ""

        initial = CaseState(
            run_id=ctx.run_id,
            thread_id=thread_id,
            current_date=_date.today(),
            user_goal=query,
            complexity=complexity,
            user_id=ctx.user_id,
            law_as_of_date=ctx.law_as_of_date,
            uploaded_documents=uploaded_docs,
            conversation_summary=conversation_summary,
        )
        final_output = ""
        last_state: dict[str, Any] = {}

        # P0-1 修复：使用 version="v2" + stream_mode=["updates", "tasks"]
        # v2 格式统一返回 {"type": ..., "data": ...} 字典；
        # v1 多 stream mode 返回 (mode, data) 元组，会被 isinstance(dict) 跳过。
        # "tasks" 流提供节点任务的开始/完成/错误事件，比 "debug" 更语义化。
        try:
            final_output, legal_answer, document_file = await _stream_graph_events(
                graph,
                initial.model_dump(),
                config,
                ctx,
                final_output=final_output,
                collect_state=last_state,
            )
            ctx.legal_answer = legal_answer
            ctx.document_file = document_file
        except TypeError as exc:  # v2 参数不兼容 → 回退 v1 updates
            if "version" not in str(exc):
                raise
            async for chunk in graph.astream(initial.model_dump(), config, stream_mode="updates"):
                if not isinstance(chunk, dict):
                    continue
                for _node_name, update in chunk.items():
                    if isinstance(update, dict):
                        last_state.update(update)
                        # P0-A 修复：与 _stream_graph_events 保持一致，使用 "key in
                        # update" 显式覆盖，避免 legal_answer=None 不生效。
                        if "final_output" in update:
                            final_output = update["final_output"]
                        if "legal_answer" in update:
                            ctx.legal_answer = update["legal_answer"]
                        if "document_file" in update:
                            ctx.document_file = update["document_file"]

        # 检查是否有 LangGraph interrupt（HITL）—— P0-2 三态 fail-closed
        icr = await _check_interrupt_status_async(graph, config)
        if icr.status == "unavailable":
            # checkpoint 不可读：不得假定无 interrupt，不得发送 final_output
            # P0-2：设置状态和错误码，_drive 的 _fail_run 负责持久化
            ctx.status = "failed"
            ctx.error = "无法读取 checkpoint 判断 HITL 状态，运行不可安全完成"
            ctx.fail_code = "checkpoint_unavailable"
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
                ctx.fail_code = "hitl_persistence_failed"
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
