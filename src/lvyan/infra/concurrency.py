"""并发控制：LLM 和检索的信号量限制。

防止单实例内瞬时并发过高（例如 10 个用户同时发起 Agent run），
导致 LLM API 429 / 上下文超限 / 内存 OOM。

配置
----
- ``MAX_LLM_CONCURRENCY``：LLM 并发请求数上限（默认 10）
- ``MAX_RETRIEVAL_CONCURRENCY``：检索并发请求数上限（默认 20）

用法
----
    from lvyan.infra.concurrency import llm_semaphore, retrieval_semaphore

    async with llm_semaphore:
        response = await llm.invoke(messages)

    async with retrieval_semaphore:
        results = await search(query)
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from collections.abc import Callable
from typing import AsyncGenerator

_logger = logging.getLogger("lvyan.runtime.concurrency")

__all__ = [
    "llm_semaphore",
    "retrieval_semaphore",
    "get_llm_semaphore",
    "get_retrieval_semaphore",
]


class InstrumentedSemaphore:
    """带监控的异步信号量。

    ``_active`` / ``_waiting`` 仅在单一事件循环内更新，没有额外锁；
    不得从同步线程或其它 loop 读写这些计数。

    记录：
    - 当前活跃数
    - 等待队列长度
    - 获取延迟
    """

    def __init__(self, name: str, limit: int) -> None:
        self._name = name
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._active = 0
        self._waiting = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def available(self) -> int:
        return self._limit - self._active

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[None, None]:
        """获取信号量（带监控）。"""
        await self.__aenter__()
        try:
            yield
        finally:
            await self.__aexit__()

    async def __aenter__(self) -> "InstrumentedSemaphore":
        self._waiting += 1
        start = time.perf_counter()

        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1

        wait_time = time.perf_counter() - start
        self._active += 1

        if wait_time > 1.0:
            _logger.warning(
                "%s 信号量等待 %.2fs (active=%d/%d, waiting=%d)",
                self._name,
                wait_time,
                self._active,
                self._limit,
                self._waiting,
            )

        return self

    async def __aexit__(self, *args: object) -> None:
        self._active -= 1
        self._semaphore.release()


# 惰性初始化单例
_llm_sem: InstrumentedSemaphore | None = None
_retrieval_sem: InstrumentedSemaphore | None = None


def get_llm_semaphore() -> InstrumentedSemaphore:
    """获取 LLM 并发信号量。"""
    global _llm_sem
    if _llm_sem is None:
        from lvyan.config import settings

        limit = settings.max_llm_concurrency
        _llm_sem = InstrumentedSemaphore("llm", limit)
        _logger.info("LLM 信号量初始化: limit=%d", limit)
    return _llm_sem


def get_retrieval_semaphore() -> InstrumentedSemaphore:
    """获取检索并发信号量。"""
    global _retrieval_sem
    if _retrieval_sem is None:
        from lvyan.config import settings

        limit = settings.max_retrieval_concurrency
        _retrieval_sem = InstrumentedSemaphore("retrieval", limit)
        _logger.info("检索信号量初始化: limit=%d", limit)
    return _retrieval_sem


class _LazySemaphoreProxy:
    """惰性代理：首次访问 __aenter__/__aexit__/acquire 时初始化真实信号量。

    允许 ``async with llm_semaphore:`` 直接使用，而无需调用 getter 函数。
    """

    def __init__(self, factory: Callable[[], InstrumentedSemaphore]) -> None:
        self._factory = factory
        self._instance: InstrumentedSemaphore | None = None

    def _get(self) -> InstrumentedSemaphore:
        if self._instance is None:
            self._instance = self._factory()
        return self._instance

    async def __aenter__(self) -> InstrumentedSemaphore:
        return await self._get().__aenter__()

    async def __aexit__(self, *args: object) -> None:
        return await self._get().__aexit__(*args)

    def acquire(self) -> "AsyncGenerator[None, None]":
        return self._get().acquire()

    @property
    def active(self) -> int:
        return self._get().active

    @property
    def waiting(self) -> int:
        return self._get().waiting

    @property
    def limit(self) -> int:
        return self._get().limit

    @property
    def available(self) -> int:
        return self._get().available


# 便捷别名：可直接用 ``async with llm_semaphore:`` 语法
llm_semaphore = _LazySemaphoreProxy(get_llm_semaphore)
retrieval_semaphore = _LazySemaphoreProxy(get_retrieval_semaphore)
