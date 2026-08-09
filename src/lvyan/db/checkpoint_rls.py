"""LangGraph checkpoint 表的行级安全策略。"""

from __future__ import annotations

from typing import Any

__all__ = ["ensure_checkpoint_rls"]


_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def ensure_checkpoint_rls(conn: Any) -> None:
    """在 checkpointer 建表后为全部 checkpoint 表安装租户 RLS 策略。

    LangGraph 在应用首次启动时才创建 checkpoint 表，因此这一步不能只依赖
    PostgreSQL 初始化目录中的静态迁移；必须在 ``AsyncPostgresSaver.setup()``
    成功后执行。缺少 ``agent_threads`` 或无法安装策略时让异常向上传播，避免
    ``RLS_ENFORCED=true`` 的生产实例静默以未隔离状态运行。
    """
    for table in _CHECKPOINT_TABLES:
        policy = f"tenant_{table}"
        await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        await conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        await conn.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        await conn.execute(
            f"""
            CREATE POLICY {policy} ON {table}
            FOR ALL
            USING (
                EXISTS (
                    SELECT 1 FROM agent_threads
                    WHERE agent_threads.thread_id = {table}.thread_id
                      AND agent_threads.user_id = current_setting('app.user_id', true)
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM agent_threads
                    WHERE agent_threads.thread_id = {table}.thread_id
                      AND agent_threads.user_id = current_setting('app.user_id', true)
                )
            )
            """
        )
