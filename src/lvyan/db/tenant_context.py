"""租户上下文管理：每个数据库事务自动设置 app.user_id。

用法
----
同步（SQLAlchemy Core/ORM）::

    with get_tenant_session(engine, user_id) as session:
        session.execute(text("SELECT * FROM agent_threads"))
        # RLS 自动过滤，仅返回 user_id 匹配的行

异步（psycopg async / AsyncPostgresSaver）::

    async with get_async_tenant_conn(pool, user_id) as conn:
        await conn.execute("SELECT * FROM agent_runs")

设计要点
--------
- 使用 ``SET LOCAL app.user_id`` 而非 ``SET``：LOCAL 仅在当前事务内生效，
  事务结束后自动清除，连接归还池时无需手动重置。
- 必须在事务开始后立即设置（SAVEPOINT 之前），确保整个事务所有查询都受 RLS 约束。
- owner role 具有 BYPASSRLS，不受策略影响；runtime role 强制受约束。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncGenerator, Generator

_logger = logging.getLogger("lvyan.db.tenant_context")

__all__ = [
    "set_tenant_context",
    "set_tenant_context_async",
    "get_tenant_session",
    "get_async_tenant_conn",
]


def set_tenant_context(conn: Any, user_id: str) -> None:
    """在给定连接上设置租户上下文（同步）。

    Args:
        conn: SQLAlchemy Connection 或 psycopg Connection。
        user_id: 当前请求的用户标识。

    必须在事务内调用（BEGIN 之后），使用 SET LOCAL 确保仅在当前事务生效。
    """
    if not user_id or not user_id.strip():
        raise ValueError("user_id 不能为空：RLS 策略依赖 app.user_id 非空")

    # SQLAlchemy 使用具名绑定；psycopg 使用参数化 set_config。根据连接模块
    # 明确分流，不能把数据库执行错误误判为“需要回退”的驱动差异。
    module_name = type(conn).__module__
    if module_name.startswith("sqlalchemy"):
        from sqlalchemy import text

        conn.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_id})
    else:
        conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
    _logger.debug("tenant context set: user_id=%s", user_id[:8] + "...")


async def set_tenant_context_async(conn: Any, user_id: str) -> None:
    """在给定异步连接上设置租户上下文。

    Args:
        conn: psycopg.AsyncConnection 或兼容接口。
        user_id: 当前请求的用户标识。
    """
    if not user_id or not user_id.strip():
        raise ValueError("user_id 不能为空：RLS 策略依赖 app.user_id 非空")

    await conn.execute("SET LOCAL app.user_id = %s", (user_id,))
    _logger.debug("async tenant context set: user_id=%s", user_id[:8] + "...")


@contextlib.contextmanager
def get_tenant_session(engine: Any, user_id: str) -> Generator[Any, None, None]:
    """获取已设置租户上下文的 SQLAlchemy Session（同步）。

    使用 context manager 确保：
    1. 事务开始后立即 SET LOCAL
    2. 退出时自动 commit/rollback
    """
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        with session.begin():
            set_tenant_context(session.connection(), user_id)
            yield session


@contextlib.asynccontextmanager
async def get_async_tenant_conn(pool: Any, user_id: str) -> AsyncGenerator[Any, None]:
    """从连接池获取已设置租户上下文的异步连接。

    使用 async context manager 确保：
    1. 从池获取连接并开始事务
    2. SET LOCAL app.user_id
    3. 退出时归还连接

    Args:
        pool: psycopg_pool.AsyncConnectionPool 或兼容接口。
        user_id: 当前请求的用户标识。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await set_tenant_context_async(conn, user_id)
            yield conn
