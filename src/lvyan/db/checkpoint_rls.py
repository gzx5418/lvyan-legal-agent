"""LangGraph checkpoint 表的行级安全策略。"""

from __future__ import annotations

from typing import Any

from psycopg import sql

__all__ = ["ensure_checkpoint_rls", "ensure_checkpoint_rls_sync"]


_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _checkpoint_rls_statements() -> list[Any]:
    """生成全部 checkpoint 表的 RLS 安装语句（异步/同步共用）。"""
    statements: list[Any] = []
    for table_name in _CHECKPOINT_TABLES:
        table = sql.Identifier(table_name)
        policy = sql.Identifier(f"tenant_{table_name}")
        statements.append(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
        statements.append(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table))
        statements.append(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(policy, table))
        statements.append(
            sql.SQL(
                """
                CREATE POLICY {} ON {}
                FOR ALL
                USING (
                    EXISTS (
                        SELECT 1 FROM agent_threads
                        WHERE agent_threads.thread_id = {}.thread_id
                          AND agent_threads.user_id = current_setting('app.user_id', true)
                    )
                )
                WITH CHECK (
                    EXISTS (
                        SELECT 1 FROM agent_threads
                        WHERE agent_threads.thread_id = {}.thread_id
                          AND agent_threads.user_id = current_setting('app.user_id', true)
                    )
                )
                """
            ).format(policy, table, table, table)
        )
    return statements


async def ensure_checkpoint_rls(conn: Any) -> None:
    """在 checkpointer 建表后为全部 checkpoint 表安装租户 RLS 策略。

    LangGraph 在应用首次启动时才创建 checkpoint 表，因此这一步不能只依赖
    PostgreSQL 初始化目录中的静态迁移；必须在 ``AsyncPostgresSaver.setup()``
    成功后执行。缺少 ``agent_threads`` 或无法安装策略时让异常向上传播，避免
    ``RLS_ENFORCED=true`` 的生产实例静默以未隔离状态运行。
    """
    for statement in _checkpoint_rls_statements():
        await conn.execute(statement)


def ensure_checkpoint_rls_sync(conn: Any) -> None:
    """同步 PostgresSaver 使用的 RLS 安装入口。"""
    for statement in _checkpoint_rls_statements():
        conn.execute(statement)
