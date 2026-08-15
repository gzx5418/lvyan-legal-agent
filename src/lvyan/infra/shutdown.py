"""优雅停机协调器。

职责
----
1. 注册 SIGTERM / SIGINT handler
2. 通知所有运行中的 Agent run 开始关闭
3. 等待最多 SHUTDOWN_GRACE_SECONDS 秒让运行中任务完成
4. 将未完成的 run 标记为 'interrupted'（可从 checkpoint 恢复）
5. 关闭连接池、Redis 连接等资源

与 Kubernetes 集成
-----------------
- K8s 发送 SIGTERM 后等待 terminationGracePeriodSeconds（默认 30s）
- Docker Compose: stop_grace_period: 45s
- 本模块在 grace period 内尝试完成所有工作
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Callable, Coroutine

_logger = logging.getLogger("lvyan.runtime.shutdown")

__all__ = ["GracefulShutdown", "get_shutdown_coordinator"]


class GracefulShutdown:
    """优雅停机协调器（单例）。"""

    def __init__(self, grace_seconds: int = 30) -> None:
        self._grace_seconds = grace_seconds
        self._shutting_down = False
        self._shutdown_event = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()
        self._cleanup_callbacks: list[Callable[[], Coroutine[Any, Any, None]]] = []

    @property
    def is_shutting_down(self) -> bool:
        """是否正在关闭中（其他组件可检查此标志拒绝新工作）。"""
        return self._shutting_down

    @property
    def shutdown_event(self) -> asyncio.Event:
        """关闭事件，可用于 await。"""
        return self._shutdown_event

    def register_active_task(self, task: asyncio.Task) -> None:
        """注册一个活跃的 Agent 任务（停机时等待它完成）。"""
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    def register_cleanup(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """注册异步清理回调（关闭连接池等）。"""
        self._cleanup_callbacks.append(callback)

    def reset_after_normal_lifespan_exit(self) -> bool:
        """复位测试/嵌入式 ASGI 场景中的正常生命周期退出状态。

        正常 lifespan 退出会执行清理序列但没有收到终止信号；这类进程内应用可被
        重建（例如 TestClient），不应永久拒绝新 run。若已经收到 SIGTERM/SIGINT，
        ``shutdown_event`` 已置位，必须保持 fail-closed 并返回 ``False``。
        """
        if self._shutdown_event.is_set():
            return False
        if self._shutting_down:
            self._shutting_down = False
            _logger.debug("正常 lifespan 退出后的 shutdown coordinator 已复位")
        return True

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """安装 SIGTERM/SIGINT handler。"""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _logger.warning("无法获取事件循环，信号处理未安装")
                return

        installed = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal, sig)
                installed = True
            except (NotImplementedError, OSError, RuntimeError):
                # Windows ProactorEventLoop 或已关闭的循环不支持 add_signal_handler
                pass

        if not installed:
            # Windows 回退：使用 signal.signal（仅主线程有效）
            try:
                signal.signal(signal.SIGINT, lambda s, f: self._handle_signal(s))
                signal.signal(signal.SIGTERM, lambda s, f: self._handle_signal(s))
            except (OSError, ValueError):
                # 非主线程或信号不可用
                _logger.debug("信号处理安装失败（非主线程或平台不支持）")
                return

        _logger.info("优雅停机 handler 已安装 (grace_seconds=%d)", self._grace_seconds)

    def _handle_signal(self, sig: Any) -> None:
        """信号处理入口。"""
        if self._shutting_down:
            _logger.warning("收到第二次终止信号，强制退出")
            raise SystemExit(1)

        sig_name = getattr(sig, "name", str(sig))
        _logger.info("收到 %s 信号，开始优雅停机...", sig_name)
        self._shutting_down = True
        self._shutdown_event.set()

        # 创建关闭协程任务
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._shutdown_sequence())
        except RuntimeError:
            pass

    async def begin_shutdown(self) -> None:
        """开始停机序列（公开入口，供 lifespan 等外部调用方使用）。

        若已处于停机流程（例如收到终止信号）则不重复执行。
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        await self._shutdown_sequence()

    async def _shutdown_sequence(self) -> None:
        """执行停机序列。"""
        start = time.monotonic()
        _logger.info(
            "等待 %d 个活跃任务完成 (max %ds)...",
            len(self._active_tasks),
            self._grace_seconds,
        )

        # 等待活跃任务完成
        if self._active_tasks:
            remaining = self._grace_seconds - (time.monotonic() - start)
            if remaining > 0:
                done, pending = await asyncio.wait(
                    self._active_tasks,
                    timeout=remaining,
                )
                if pending:
                    _logger.warning(
                        "%d 个任务未在宽限期内完成，标记为 interrupted",
                        len(pending),
                    )
                    for task in pending:
                        task.cancel()
                    # cancel 后必须 await 任务真正结束（CancelledError 传播完成）
                    # 再继续清理，否则任务可能在清理回调关闭连接池后仍在跑
                    done, still_pending = await asyncio.wait(
                        pending,
                        timeout=5.0,
                    )
                    if still_pending:
                        _logger.warning(
                            "%d 个任务取消超时（5s），继续停机流程",
                            len(still_pending),
                        )

        # 执行清理回调
        for callback in self._cleanup_callbacks:
            try:
                await asyncio.wait_for(callback(), timeout=5.0)
            except asyncio.TimeoutError:
                _logger.warning("清理回调超时: %s", callback.__name__)
            except Exception as exc:  # noqa: BLE001 boundary-exception: 清理不应阻断关闭
                _logger.error("清理回调异常: %s", exc)

        elapsed = time.monotonic() - start
        _logger.info("优雅停机完成 (耗时 %.1fs)", elapsed)


# 全局单例
_coordinator: GracefulShutdown | None = None


def get_shutdown_coordinator() -> GracefulShutdown:
    """获取全局停机协调器（惰性创建）。"""
    global _coordinator
    if _coordinator is None:
        import os

        grace = int(os.getenv("SHUTDOWN_GRACE_SECONDS", "30"))
        _coordinator = GracefulShutdown(grace_seconds=grace)
    return _coordinator
