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

import logging
import os
from typing import Any, Optional, Sequence

_logger = logging.getLogger("lvyan.db.tenant_saver")

__all__ = ["TenantAwareCheckpointer"]


class TenantAwareCheckpointer:
    """包装 AsyncPostgresSaver，在操作前注入租户上下文。"""

    def __init__(self, inner: Any) -> None:
        """
        Args:
            inner: LangGraph AsyncPostgresSaver 或兼容的 checkpointer 实例。
        """
        self._inner = inner
        self._rls_enforced = os.getenv("RLS_ENFORCED", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @property
    def inner(self) -> Any:
        """获取底层 checkpointer（用于 setup/health check 等无需 RLS 的操作）。"""
        return self._inner

    def _extract_user_id(self, config: dict[str, Any]) -> str | None:
        """从 LangGraph config 中提取 user_id。"""
        configurable = config.get("configurable", {})
        user_id = configurable.get("user_id")
        if not user_id and self._rls_enforced:
            raise ValueError(
                "RLS_ENFORCED=true 但 config.configurable.user_id 为空。"
                "所有 checkpointer 操作必须携带 user_id。"
            )
        return user_id

    async def _set_context(self, user_id: str | None) -> None:
        """在底层连接上设置租户上下文。"""
        if not user_id:
            return

        # AsyncPostgresSaver 内部使用 self.conn (AsyncConnection)
        conn = getattr(self._inner, "conn", None)
        if conn is None:
            # 连接池模式 (psycopg_pool)
            conn = getattr(self._inner, "_conn", None)

        if conn is not None:
            try:
                await conn.execute(
                    "SELECT set_config('app.user_id', %s, true)", (user_id,)
                )
            except Exception as exc:  # noqa: BLE001 boundary-exception: 设置上下文失败
                _logger.warning("设置租户上下文失败: %s", exc)
                if self._rls_enforced:
                    raise

    # ------------------------------------------------------------------
    # 代理 LangGraph Checkpointer 协议方法
    # ------------------------------------------------------------------

    async def aget(self, config: dict[str, Any]) -> Optional[Any]:
        """获取 checkpoint（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        await self._set_context(user_id)
        return await self._inner.aget(config)

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: Any = None,
        new_versions: Any = None,
    ) -> dict[str, Any]:
        """写入 checkpoint（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        await self._set_context(user_id)
        return await self._inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        """写入中间 writes（带 RLS 上下文）。"""
        user_id = self._extract_user_id(config)
        await self._set_context(user_id)
        return await self._inner.aput_writes(config, writes, task_id)

    async def alist(
        self,
        config: Optional[dict[str, Any]] = None,
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[dict[str, Any]] = None,
        limit: int = 10,
    ) -> Any:
        """列出 checkpoints（带 RLS 上下文）。"""
        if config:
            user_id = self._extract_user_id(config)
            await self._set_context(user_id)
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    async def setup(self) -> None:
        """初始化 checkpointer schema（无需 RLS）。"""
        if hasattr(self._inner, "setup"):
            await self._inner.setup()

    # ------------------------------------------------------------------
    # 透传属性：让上层代码认为这就是原始 saver
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """透传未覆盖的属性到底层 saver。"""
        return getattr(self._inner, name)
