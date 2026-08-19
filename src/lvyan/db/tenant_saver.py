"""租户感知的 AsyncPostgresSaver 包装器。

在 LangGraph checkpointer 操作前/后自动注入 RLS 上下文（app.user_id），
确保 checkpoint 数据受 Row-Level Security 保护。

用法
----
替换原生 AsyncPostgresSaver::

    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    saver = TenantAwareCheckpointer(original_saver)
    # 所有 aput/aget 操作自动注入 user_id 上下文

设计决策
--------
- 采用组合模式（包装）而非继承：避免耦合 LangGraph 内部实现细节。
- user_id 通过 LangGraph config 的 configurable.user_id 传入。
- 若 configurable 中无 user_id：
  - RLS_ENFORCED=true → 抛 ValueError（拒绝无租户的操作）
  - RLS_ENFORCED=false → 允许通过（向后兼容）
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional, Sequence

_logger = logging.getLogger("lvyan.db.tenant_saver")

__all__ = ["TenantAwareCheckpointer", "SyncTenantAwareCheckpointer"]


class TenantAwareCheckpointer:
    """包装 AsyncPostgresSaver，在操作前注入租户上下文。"""

    def __init__(self, inner: Any) -> None:
        """
        Args:
            inner: LangGraph AsyncPostgresSaver 或兼容的 checkpointer 实例。
        """
        self._inner = inner
        from lvyan.config import is_rls_enforced

        self._rls_enforced = is_rls_enforced()
        # AsyncPostgresSaver 复用单一连接。tenant context 是连接级状态，必须把
        # “设置 user_id + 执行 saver 操作”串行化，避免并发请求串租户。
        self._tenant_lock = asyncio.Lock()

    @property
    def inner(self) -> Any:
        """获取底层 checkpointer（用于 setup/health check 等无需 RLS 的操作）。"""
        return self._inner

    def _extract_user_id(self, config: dict[str, Any] | None) -> str | None:
        """从 LangGraph config 中提取 user_id。"""
        configurable = (config or {}).get("configurable", {})
        user_id = configurable.get("user_id")
        if not user_id and self._rls_enforced:
            raise ValueError(
                "RLS_ENFORCED=true 但 config.configurable.user_id 为空。"
                "所有 checkpointer 操作必须携带 user_id。"
            )
        return user_id

    def _get_conn(self) -> Any | None:
        """取得底层 saver 的数据库连接（AsyncPostgresSaver 为 self.conn）。"""
        conn = getattr(self._inner, "conn", None)
        if conn is None:
            # 连接池模式 (psycopg_pool)
            conn = getattr(self._inner, "_conn", None)
        return conn

    async def _set_context(self, user_id: str | None) -> None:
        """在底层连接上设置租户上下文。

        拿不到底层连接时无法注入 RLS 上下文：RLS_ENFORCED=true 必须
        fail-closed 抛错（否则操作会以连接上残留的上一个租户上下文执行，
        造成跨租户读写）；仅 RLS_ENFORCED=false 时允许降级放行。
        """
        if not user_id:
            return

        conn = self._get_conn()
        if conn is None:
            if self._rls_enforced:
                raise RuntimeError(
                    "RLS_ENFORCED=true 但无法取得 checkpointer 底层连接以注入 app.user_id，"
                    "拒绝执行操作（fail-closed）"
                )
            _logger.warning(
                "无法取得 checkpointer 底层连接，跳过租户上下文注入（RLS_ENFORCED=false）"
            )
            return

        try:
            # AsyncPostgresSaver 使用 autocommit 连接，SET LOCAL 会在当前语句
            # 结束后丢失；使用会话级 set_config，操作结束后由 _clear_context 复位。
            await conn.execute("SELECT set_config('app.user_id', %s, false)", (user_id,))
        except Exception as exc:  # noqa: BLE001 boundary-exception: 设置上下文失败
            _logger.warning("设置租户上下文失败: %s", exc)
            if self._rls_enforced:
                raise

    async def _clear_context(self) -> None:
        """操作结束后复位租户上下文，避免会话级 app.user_id 残留。

        残留的上下文会让后续未带 user_id 的操作（或拿不到连接的操作）以
        上一个租户的身份执行 RLS 过滤的查询，读到错误租户的数据。
        """
        conn = self._get_conn()
        if conn is None:
            return
        try:
            await conn.execute("SELECT set_config('app.user_id', '', false)")
        except Exception as exc:  # noqa: BLE001 boundary-exception: 清除上下文失败
            _logger.warning("清除租户上下文失败: %s", exc)

    # ------------------------------------------------------------------
    # 代理 LangGraph Checkpointer 协议方法
    # ------------------------------------------------------------------

    async def aget(self, config: dict[str, Any]) -> Optional[Any]:
        """获取 checkpoint（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                return await self._inner.aget(config)
            finally:
                await self._clear_context()

    async def aget_tuple(self, config: dict[str, Any]) -> Optional[Any]:
        """获取完整 checkpoint tuple（LangGraph 状态读取的实际调用路径）。"""
        user_id = self._extract_user_id(config)
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                return await self._inner.aget_tuple(config)
            finally:
                await self._clear_context()

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: Any = None,
        new_versions: Any = None,
    ) -> dict[str, Any]:
        """写入 checkpoint（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                return await self._inner.aput(config, checkpoint, metadata, new_versions)
            finally:
                await self._clear_context()

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """写入中间 writes（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                return await self._inner.aput_writes(config, writes, task_id, task_path)
            finally:
                await self._clear_context()

    async def alist(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[dict[str, Any]] = None,
        limit: int | None = None,
    ) -> Any:
        """列出 checkpoints（带 RLS 上下文）。

        在锁内物化全部结果后再释放锁并逐条产出：若在 ``async with`` 内
        ``yield``，锁的持有期由消费方迭代速度决定（消费慢时全进程租户操作
        被串行阻塞），且消费方在同任务内再调用任何包装方法都会因
        ``asyncio.Lock`` 不可重入而死锁。checkpoint 列表规模有限（受
        limit / 单线程 checkpoint 数约束），物化成本可接受。
        """
        user_id = self._extract_user_id(config) if config else None
        if self._rls_enforced and user_id is None:
            raise ValueError("RLS_ENFORCED=true 时 alist 必须携带含 user_id 的 config")
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                items = [
                    item
                    async for item in self._inner.alist(
                        config, filter=filter, before=before, limit=limit
                    )
                ]
            finally:
                await self._clear_context()
        for item in items:
            yield item

    async def adelete_thread(
        self,
        thread_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        """删除 thread 的全部 checkpoint（带 RLS 上下文）。

        优先调用底层 saver 的**异步** ``adelete_thread``。禁止在事件循环
        线程内直接调用 ``AsyncPostgresSaver.delete_thread``——它是同步桥接，
        带「必须在非绑定循环线程调用」的守卫，在 loop 线程内调用必然抛
        ``InvalidStateError``（历史上正是这个回归导致 Postgres 部署的删除
        会话接口 100% 返回 503）。只有底层 saver 仅有同步实现时，才通过
        ``asyncio.to_thread`` 在 worker 线程调用（worker 线程内合法）。
        """
        user_id = self._extract_user_id(config)
        async with self._tenant_lock:
            await self._set_context(user_id)
            try:
                adelete = getattr(self._inner, "adelete_thread", None)
                if callable(adelete):
                    await adelete(thread_id)
                    return
                delete = getattr(self._inner, "delete_thread", None)
                if not callable(delete):
                    raise RuntimeError("checkpointer delete_thread unavailable")
                await asyncio.to_thread(delete, thread_id)
            finally:
                await self._clear_context()

    def delete_thread(
        self,
        thread_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        """阻止经 ``__getattr__`` 漏到未注入租户的底层 ``delete_thread``。

        异步包装器必须走 ``adelete_thread``：同步调用既无法使用
        ``asyncio.Lock``，也会在运行中的事件循环里死锁。
        """
        raise RuntimeError(
            "TenantAwareCheckpointer 请使用 await adelete_thread(thread_id, config=...)"
        )

    async def setup(self) -> None:
        """初始化 checkpointer schema，并在强制模式下安装 checkpoint RLS。"""
        if hasattr(self._inner, "setup"):
            await self._inner.setup()
        if self._rls_enforced:
            conn = getattr(self._inner, "conn", None) or getattr(self._inner, "_conn", None)
            if conn is None:
                raise RuntimeError("RLS_ENFORCED=true 但无法取得 checkpointer 数据库连接")
            from lvyan.db.checkpoint_rls import ensure_checkpoint_rls

            await ensure_checkpoint_rls(conn)

    # ------------------------------------------------------------------
    # 透传属性：让上层代码认为这就是原始 saver
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """透传未覆盖的属性到底层 saver。"""
        return getattr(self._inner, name)


class SyncTenantAwareCheckpointer:
    """同步 PostgresSaver 的租户上下文包装器，供 CLI/invoke 路径使用。"""

    def __init__(self, inner: Any) -> None:
        from lvyan.config import is_rls_enforced

        self._inner = inner
        self._rls_enforced = is_rls_enforced()
        self._tenant_lock = threading.RLock()

    @property
    def inner(self) -> Any:
        return self._inner

    def _extract_user_id(self, config: dict[str, Any] | None) -> str | None:
        user_id = (config or {}).get("configurable", {}).get("user_id")
        if not user_id and self._rls_enforced:
            raise ValueError("RLS_ENFORCED=true 时同步 checkpointer 操作必须携带 user_id")
        return user_id

    def _get_conn(self) -> Any | None:
        return getattr(self._inner, "conn", None) or getattr(self._inner, "_conn", None)

    def _set_context(self, user_id: str | None) -> None:
        if not user_id:
            return
        conn = self._get_conn()
        if conn is None:
            if self._rls_enforced:
                raise RuntimeError("无法取得同步 checkpointer 连接以注入 app.user_id")
            _logger.warning(
                "无法取得同步 checkpointer 底层连接，跳过租户上下文注入（RLS_ENFORCED=false）"
            )
            return
        try:
            conn.execute("SELECT set_config('app.user_id', %s, false)", (user_id,))
        except Exception as exc:  # noqa: BLE001 boundary-exception: 设置上下文失败
            _logger.warning("设置同步租户上下文失败: %s", exc)
            if self._rls_enforced:
                raise

    def _clear_context(self) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        try:
            conn.execute("SELECT set_config('app.user_id', '', false)")
        except Exception as exc:  # noqa: BLE001 boundary-exception: 清除上下文失败
            _logger.warning("清除同步租户上下文失败: %s", exc)

    def _call(self, method: str, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        user_id = self._extract_user_id(config)
        with self._tenant_lock:
            self._set_context(user_id)
            try:
                return getattr(self._inner, method)(config, *args, **kwargs)
            finally:
                self._clear_context()

    def get(self, config: dict[str, Any]) -> Any:
        return self._call("get", config)

    def get_tuple(self, config: dict[str, Any]) -> Any:
        return self._call("get_tuple", config)

    def put(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: Any = None,
        new_versions: Any = None,
    ) -> Any:
        return self._call("put", config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> Any:
        return self._call("put_writes", config, writes, task_id, task_path)

    def list(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[dict[str, Any]] = None,
        limit: int | None = None,
    ) -> Any:
        user_id = self._extract_user_id(config)
        with self._tenant_lock:
            self._set_context(user_id)
            try:
                items = list(self._inner.list(config, filter=filter, before=before, limit=limit))
            finally:
                self._clear_context()
        yield from items

    def delete_thread(
        self,
        thread_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        user_id = self._extract_user_id(config)
        with self._tenant_lock:
            self._set_context(user_id)
            try:
                delete = getattr(self._inner, "delete_thread", None)
                if not callable(delete):
                    raise RuntimeError("checkpointer delete_thread unavailable")
                delete(thread_id)
            finally:
                self._clear_context()

    def setup(self) -> None:
        if hasattr(self._inner, "setup"):
            self._inner.setup()
        if self._rls_enforced:
            conn = self._get_conn()
            if conn is None:
                raise RuntimeError("RLS_ENFORCED=true 但无法取得同步 checkpointer 数据库连接")
            from lvyan.db.checkpoint_rls import ensure_checkpoint_rls_sync

            ensure_checkpoint_rls_sync(conn)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
