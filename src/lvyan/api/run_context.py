"""Agent 运行上下文（从 sse.py 拆分）。

``RunContext`` 是单次 Agent 运行的上下文对象，包含运行 ID、线程 ID、
用户归属、队列、状态、HITL 中断信息等。被 ``RunManager`` 和 SSE
流式推送共同使用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Callable

_logger = logging.getLogger("lvyan.api.run_context")

# runner 协议：async (query, thread_id, complexity, ctx) -> final_output(str)
Runner = Callable[..., Any]

# issue #16：重放历史上限。晚订阅者重放「最后 _EVENT_HISTORY_CAP 条事件 +
# 终态哨兵（close 时补发 None）」——超上限的早期事件只保证已实时广播给
# 当时的在线订阅者，不保证晚订阅者可重放；这足以支撑 SSE 断线重连场景
# （重连只关心最近进度与终态），同时防止长 run 的 history 无界增长。
_EVENT_HISTORY_CAP = 200


class RunContext:
    """单次 Agent 运行的上下文。"""

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        user_id: str = "anonymous",
        law_as_of_date: date | None = None,
        attachment_refs: list[dict] | None = None,
        user_preferences: dict[str, Any] | None = None,
        load_history: Any = None,
        legacy_queue: bool = True,
    ) -> None:
        """初始化运行上下文。

        Args:
            legacy_queue: 是否向 ``self.queue`` 投递事件副本。生产 SSE 路由只
                消费订阅者队列（``subscribe()``），旧队列无消费者、事件会被
                双份保留，故 ``api/sse.py`` 构造时传 ``False`` 关闭投递。
                默认 ``True`` 保持既有测试/旧调用方（直接读 ``ctx.queue``）
                兼容。
        """
        self.run_id = run_id
        self.thread_id = thread_id
        # P2-13：归属用户；用于 stream / hitl 端点的 ownership 校验
        self.user_id: str = user_id
        self.law_as_of_date = law_as_of_date
        # P0 性能：附件以 DocumentRef dict 形式传入，不再拼进 user_goal
        self.attachment_refs: list[dict] = list(attachment_refs or [])
        # Only sanitized display preferences belong here; case facts and raw
        # documents must remain in the checkpoint/vault channels.
        self.user_preferences: dict[str, Any] = dict(user_preferences or {})
        # 多轮记忆：由 RunManager 注入的回调，runner 调用它读取本 thread 历史。
        # 签名：() -> list[dict]；为 None 表示无持久化存储（无历史可读）。
        self.load_history: Any = load_history
        # 状态：started / running / awaiting_hitl / completed / failed / cancelled
        self.status: str = "started"
        # ``queue`` 保留给内部测试/旧调用方；SSE 使用独立订阅队列广播，避免
        # 多个客户端在同一 Queue 上竞争并各自只收到部分事件。生产路径构造
        # RunContext 时传 legacy_queue=False 关闭本队列投递（issue #16：无
        # 消费者时事件被双份保留浪费内存）；队列对象本身仍创建，close() 的
        # 终态哨兵照发，保证旧读取方语义不变。
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._legacy_queue: bool = bool(legacy_queue)
        self._event_history: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._stream_closed = False
        self.final_output: str | None = None
        # 结构化法律输出（LegalAnswerV1 dict），与 final_output 并行
        self.legal_answer: dict[str, Any] | None = None
        # P1-A：文书文件信息（document 模式 finalizer 渲染的 DOCX 元数据）
        self.document_file: dict[str, Any] | None = None
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
        # P0-2：runner 设置失败时记录错误码，供 _drive 的 _fail_run 使用
        self.fail_code: str | None = None

    async def publish(self, event: dict[str, Any]) -> None:
        """广播 SSE 事件，并保留本次 run 的有限重放历史。

        重放语义：history 只保留最后 ``_EVENT_HISTORY_CAP`` 条；晚订阅者由
        :meth:`subscribe` 重放这段尾部历史，run 结束时 :meth:`close` 再补发
        终态哨兵 ``None``，因此 cap 后「晚订阅者最终必然看到终态」的语义
        仍然成立（早期事件若被挤出 history，仅影响重连客户端的完整回放，
        不影响在线订阅者的实时收件）。
        """
        if self._stream_closed:
            _logger.debug("忽略已关闭 run %s 的迟到事件: %s", self.run_id, event.get("event"))
            return
        self._event_history.append(event)
        if len(self._event_history) > _EVENT_HISTORY_CAP:
            # 只裁掉超出上限的最旧事件（切片删除，避免每次重建列表）
            del self._event_history[: len(self._event_history) - _EVENT_HISTORY_CAP]
        if self._legacy_queue:
            await self.queue.put(event)
        for subscriber in tuple(self._subscribers):
            await subscriber.put(event)

    def subscribe(self) -> asyncio.Queue[Any]:
        """注册独立订阅队列，并重放订阅前已产生的事件。"""
        subscriber: asyncio.Queue[Any] = asyncio.Queue()
        for event in self._event_history:
            subscriber.put_nowait(event)
        if self._stream_closed:
            subscriber.put_nowait(None)
        else:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: asyncio.Queue[Any]) -> None:
        """注销断开的 SSE 订阅者。"""
        self._subscribers.discard(subscriber)

    async def close(self) -> None:
        """向旧队列及每个订阅者分别发送终态哨兵；可重复调用。"""
        if self._stream_closed:
            return
        self._stream_closed = True
        await self.queue.put(None)
        for subscriber in tuple(self._subscribers):
            await subscriber.put(None)
        self._subscribers.clear()

    def poll_cancel(self) -> bool:
        """节流地检查跨实例取消请求。间隔内重复调用直接返回 False。

        本地任务取消（asyncio.Task.cancel）仍由事件循环直接生效；本方法只
        处理「远端实例发起的取消请求」这一补充路径。

        P1-3：同步 DB 查询通过 ``asyncio.to_thread`` 卸载到线程池，避免阻塞
        event loop。调用方在 async 上下文中应改用 :meth:`poll_cancel_async`。
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

    async def poll_cancel_async(self) -> bool:
        """P1-3：异步版取消轮询，通过 ``asyncio.to_thread`` 避免阻塞 event loop。"""
        if self._cancel_check is None:
            return False
        now = time.time()
        from lvyan.config import settings as _settings

        if now - self._last_cancel_poll < _settings.cancel_poll_interval_seconds:
            return False
        self._last_cancel_poll = now
        try:
            return bool(await asyncio.to_thread(self._cancel_check))
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


__all__ = ["RunContext", "Runner"]
