"""Persistent run/thread metadata used by the API and cross-instance HITL."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from lvyan.config import AGENT_DIR, settings

_logger = logging.getLogger("lvyan.memory.run_metadata")


# H2：固定且稳定的 advisory lock 键（两个 int4）。
# pg_advisory_xact_lock 接受两个 32-bit 有符号整数作为键；此处选取一对
# 不太可能与其他业务模块冲突的常量。修改此值会使正在执行的旧实例释放不掉
# 旧锁，因此一旦发布就不要再改。
_SCHEMA_ADVISORY_LOCK_KEY: tuple[int, int] = (0x1C7A, 0xA11C)


class RunMetadataStore(Protocol):
    def create_run(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        *,
        title: str = "",
        complexity: str = "light",
        user_message: str = "",
        attachments: list[str] | None = None,
    ) -> None: ...

    def update_run(self, run_id: str, **values: Any) -> None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def get_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    def list_threads(self, user_id: str) -> list[tuple[str, dict[str, Any]]]: ...

    def delete_thread(self, thread_id: str, user_id: str) -> bool: ...

    def has_active_runs(self, thread_id: str) -> bool: ...

    def mark_thread_output(self, thread_id: str) -> None: ...

    def append_message(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        role: str,
        content: str,
        attachments: list[str] | None = None,
    ) -> None: ...

    def list_messages(
        self,
        thread_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]: ...

    def claim_hitl_run(self, run_id: str, user_id: str) -> dict[str, Any] | None: ...

    def request_cancel(self, run_id: str, user_id: str) -> bool: ...

    def is_cancel_requested(self, run_id: str, user_id: str) -> bool: ...


class RunMetadataUnavailable(RuntimeError):
    """The durable run registry could not be written."""


class ThreadOwnershipError(PermissionError):
    """A thread id is already owned by a different user."""


def _to_dsn(url: str) -> str:
    prefix = "postgresql+psycopg://"
    return "postgresql://" + url[len(prefix) :] if url.startswith(prefix) else url


class PostgresRunMetadataStore:
    """Small synchronous repository for ``agent_runs`` and ``agent_threads``.

    Connections are short lived so separate API instances can immediately observe
    one another's run records. Schema creation is lazy and idempotent.
    """

    _ALLOWED_UPDATE_COLUMNS = {
        "status",
        "interrupt_payload",
        "final_output",
        "error",
        "completed_at",
        "expires_at",
        "cancel_requested_at",
    }

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = _to_dsn(dsn or settings.database_url)
        self._schema_ready = False

    def _connect(self):
        """打开新连接。

        注意：``autocommit=True`` 仅用于 CRUD 路径（每条语句各自提交）。
        :meth:`_ensure_schema` 在内部临时把连接切到事务模式，确保 advisory
        lock 与 migration 在同一事务内提交（H2）。
        """
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=2,
            row_factory=dict_row,
        )

    def _ensure_schema(self, conn: Any) -> None:
        """在 ``conn`` 上以事务方式执行 migration。

        H2 修复：
          1. 获取事务级 advisory lock（``pg_advisory_xact_lock``），保证多个
             ``PostgresRunMetadataStore`` 实例（或多个进程）同时首次建表时
             串行化执行 migration，避免触发器 ``DROP TRIGGER + CREATE TRIGGER``
             之间的竞争。
          2. advisory lock 与 migration 在同一事务中执行并提交；事务结束
             自动释放锁。
          3. migration 抛异常时事务回滚，锁随之释放，``_schema_ready`` 保持
             ``False``，异常向上传播。

        P1-2 改进：增加 ``schema_migrations`` 版本表，已应用的 migration 不再
        重复执行（避免每次实例启动都 ``ALTER TABLE`` 取表锁）。

        ``conn`` 默认 ``autocommit=True``（见 :meth:`_connect`），本方法通过
        ``conn.transaction()`` 显式开启事务块；psycopg 的 ``transaction()``
        在 autocommit 连接上也能正常工作。
        """
        if self._schema_ready:
            return
        migrations_dir = AGENT_DIR / "migrations"
        # 按文件名排序依次执行所有 migration（001_…, 002_…, …）。
        migration_files = sorted(
            p for p in migrations_dir.glob("*.sql") if p.is_file()
        )
        if not migration_files:
            raise RuntimeError(f"未找到任何 migration 文件于 {migrations_dir}")
        # transaction() 在 autocommit=True 连接上显式开启事务；异常自动回滚
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    _SCHEMA_ADVISORY_LOCK_KEY,
                )
                # P1-2：创建 migration 版本表（幂等）
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                for migration in migration_files:
                    version = migration.name
                    cur.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = %s",
                        (version,),
                    )
                    if cur.fetchone():
                        continue
                    sql = migration.read_text(encoding="utf-8")
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s) "
                        "ON CONFLICT DO NOTHING",
                        (version,),
                    )
        # 事务已提交 → 锁已释放，schema 已就绪
        self._schema_ready = True

    def create_run(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        *,
        title: str = "",
        complexity: str = "light",
        user_message: str = "",
        attachments: list[str] | None = None,
    ) -> None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO agent_threads
                            (thread_id, user_id, title, complexity)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (thread_id) DO UPDATE SET
                            title = CASE
                                WHEN EXCLUDED.title = '' THEN agent_threads.title
                                ELSE EXCLUDED.title
                            END,
                            complexity = EXCLUDED.complexity
                        WHERE agent_threads.user_id = EXCLUDED.user_id
                        RETURNING user_id
                        """,
                        (thread_id, user_id, title, complexity),
                    )
                    owner_row = cur.fetchone()
                    if owner_row is None:
                        raise ThreadOwnershipError(f"thread {thread_id} belongs to another user")
                    cur.execute(
                        """
                        INSERT INTO agent_runs
                            (run_id, thread_id, user_id, status)
                        VALUES (%s, %s, %s, 'started')
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (run_id, thread_id, user_id),
                    )
                    if user_message:
                        from psycopg.types.json import Jsonb

                        cur.execute(
                            """
                            INSERT INTO agent_messages
                                (run_id, thread_id, user_id, role, content, attachments)
                            VALUES (%s, %s, %s, 'user', %s, %s)
                            ON CONFLICT (run_id, role) DO NOTHING
                            """,
                            (
                                run_id,
                                thread_id,
                                user_id,
                                user_message,
                                Jsonb(attachments or []),
                            ),
                        )

    def update_run(self, run_id: str, **values: Any) -> None:
        clean = {key: value for key, value in values.items() if key in self._ALLOWED_UPDATE_COLUMNS}
        if not clean:
            return
        if "interrupt_payload" in clean and isinstance(clean["interrupt_payload"], (dict, list)):
            from psycopg.types.json import Jsonb

            clean["interrupt_payload"] = Jsonb(clean["interrupt_payload"])
        assignments = ", ".join(f"{key} = %s" for key in clean)
        params = [*clean.values(), run_id]
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE agent_runs SET {assignments} WHERE run_id = %s",
                    params,
                )
                if cur.rowcount != 1:
                    raise RunMetadataUnavailable(f"run {run_id} does not exist")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, thread_id, user_id, status,
                           interrupt_payload, final_output, error,
                           created_at, completed_at
                    FROM agent_runs
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT thread_id, user_id, title, complexity,
                           has_output, created_at, updated_at
                    FROM agent_threads
                    WHERE thread_id = %s
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def list_threads(self, user_id: str) -> list[tuple[str, dict[str, Any]]]:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT thread_id, user_id, title, complexity,
                           has_output, created_at, updated_at
                    FROM agent_threads
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [(str(row["thread_id"]), dict(row)) for row in rows]

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        """Delete an inactive user-owned thread and cascade-delete its runs."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM agent_threads
                    WHERE thread_id = %s AND user_id = %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM agent_runs
                          WHERE agent_runs.thread_id = agent_threads.thread_id
                            AND status IN ('started', 'running', 'awaiting_hitl')
                      )
                    RETURNING thread_id
                    """,
                    (thread_id, user_id),
                )
                row = cur.fetchone()
        return row is not None

    def has_active_runs(self, thread_id: str) -> bool:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM agent_runs
                        WHERE thread_id = %s
                          AND status IN ('started', 'running', 'awaiting_hitl')
                    ) AS has_active_runs
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
        return bool(row and row["has_active_runs"])

    def mark_thread_output(self, thread_id: str) -> None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_threads
                    SET has_output = TRUE
                    WHERE thread_id = %s
                    """,
                    (thread_id,),
                )
                if cur.rowcount != 1:
                    raise RunMetadataUnavailable(f"thread {thread_id} does not exist")

    def append_message(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        role: str,
        content: str,
        attachments: list[str] | None = None,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported message role: {role}")
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_messages
                        (run_id, thread_id, user_id, role, content, attachments)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, role) DO UPDATE SET
                        content = EXCLUDED.content,
                        attachments = EXCLUDED.attachments
                    """,
                    (
                        run_id,
                        thread_id,
                        user_id,
                        role,
                        content,
                        Jsonb(attachments or []),
                    ),
                )

    def list_messages(
        self,
        thread_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, role, content, attachments, created_at
                    FROM agent_messages
                    WHERE thread_id = %s AND user_id = %s
                    ORDER BY message_id ASC
                    """,
                    (thread_id, user_id),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def claim_hitl_run(
        self,
        run_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Atomically transition one pending HITL run to ``running``.

        P1-1：已请求取消（``cancel_requested_at`` 非空）的 run 不得被 claim，
        防止用户取消后仍能提交审批将 run 恢复为 running。
        """
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'running'
                    WHERE run_id = %s
                      AND user_id = %s
                      AND status = 'awaiting_hitl'
                      AND cancel_requested_at IS NULL
                    RETURNING run_id, thread_id, user_id, status, created_at
                    """,
                    (run_id, user_id),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def healthcheck(self) -> bool:
        """Verify metadata tables are present and writable."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM agent_runs LIMIT 1")
                cur.fetchone()
                cur.execute("UPDATE agent_runs SET status = status WHERE FALSE")
        return True

    def request_cancel(self, run_id: str, user_id: str) -> bool:
        """P1-2：跨实例取消的请求侧。

        P1-1 改进：``awaiting_hitl`` 状态下没有 worker 轮询，无法通过协作取消
        终结。因此对 ``awaiting_hitl`` 直接原子地置为 ``cancelled`` 终态；
        对 ``started/running`` 仍只设置 ``cancel_requested_at``，由 owner 实例
        的 worker 轮询发现后停止。

        Returns:
            ``True`` 表示成功记录取消请求（或直接终结）；``False`` 表示 run
            不存在、不属于该用户、或已处于终态不可取消。
        """
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                # P1-1：awaiting_hitl 直接终结（无 worker 轮询）
                cur.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'cancelled',
                        cancel_requested_at = now(),
                        completed_at = now(),
                        error = '用户已停止生成'
                    WHERE run_id = %s
                      AND user_id = %s
                      AND status = 'awaiting_hitl'
                    RETURNING run_id
                    """,
                    (run_id, user_id),
                )
                row = cur.fetchone()
                if row is not None:
                    return True
                # started/running：协作取消（worker 轮询发现后停止）
                cur.execute(
                    """
                    UPDATE agent_runs
                    SET cancel_requested_at = now()
                    WHERE run_id = %s
                      AND user_id = %s
                      AND status IN ('started', 'running')
                    RETURNING run_id
                    """,
                    (run_id, user_id),
                )
                row = cur.fetchone()
        return row is not None

    def is_cancel_requested(self, run_id: str, user_id: str) -> bool:
        """P1-2：worker 侧协作取消轮询。``cancel_requested_at`` 非空即已请求取消。"""
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cancel_requested_at IS NOT NULL AS requested
                    FROM agent_runs
                    WHERE run_id = %s AND user_id = %s
                    """,
                    (run_id, user_id),
                )
                row = cur.fetchone()
        return bool(row and row["requested"])


__all__ = [
    "RunMetadataStore",
    "RunMetadataUnavailable",
    "ThreadOwnershipError",
    "PostgresRunMetadataStore",
]
