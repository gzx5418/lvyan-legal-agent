"""Persistent run/thread metadata used by the API and cross-instance HITL."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from lvyan.config import AGENT_DIR, settings

_logger = logging.getLogger("lvyan.memory.run_metadata")


class RunMetadataStore(Protocol):
    def create_run(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        *,
        title: str = "",
        complexity: str = "light",
    ) -> None: ...

    def update_run(self, run_id: str, **values: Any) -> None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def get_thread(self, thread_id: str) -> dict[str, Any] | None: ...

    def list_threads(self, user_id: str) -> list[tuple[str, dict[str, Any]]]: ...

    def delete_thread(self, thread_id: str, user_id: str) -> bool: ...

    def has_active_runs(self, thread_id: str) -> bool: ...

    def mark_thread_output(self, thread_id: str) -> None: ...

    def claim_hitl_run(
        self, run_id: str, user_id: str
    ) -> dict[str, Any] | None: ...


class RunMetadataUnavailable(RuntimeError):
    """The durable run registry could not be written."""


class ThreadOwnershipError(PermissionError):
    """A thread id is already owned by a different user."""


def _to_dsn(url: str) -> str:
    prefix = "postgresql+psycopg://"
    return "postgresql://" + url[len(prefix):] if url.startswith(prefix) else url


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
    }

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = _to_dsn(dsn or settings.database_url)
        self._schema_ready = False

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            self.dsn,
            autocommit=True,
            connect_timeout=2,
            row_factory=dict_row,
        )

    def _ensure_schema(self, conn: Any) -> None:
        if self._schema_ready:
            return
        migration = AGENT_DIR / "migrations" / "001_agent_runs_threads.sql"
        sql = migration.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
        self._schema_ready = True

    def create_run(
        self,
        run_id: str,
        thread_id: str,
        user_id: str,
        *,
        title: str = "",
        complexity: str = "light",
    ) -> None:
        with self._connect() as conn:
            self._ensure_schema(conn)
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
                    raise ThreadOwnershipError(
                        f"thread {thread_id} belongs to another user"
                    )
                cur.execute(
                    """
                    INSERT INTO agent_runs
                        (run_id, thread_id, user_id, status)
                    VALUES (%s, %s, %s, 'started')
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run_id, thread_id, user_id),
                )

    def update_run(self, run_id: str, **values: Any) -> None:
        clean = {
            key: value
            for key, value in values.items()
            if key in self._ALLOWED_UPDATE_COLUMNS
        }
        if not clean:
            return
        if "interrupt_payload" in clean and isinstance(
            clean["interrupt_payload"], (dict, list)
        ):
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
                    raise RunMetadataUnavailable(
                        f"run {run_id} does not exist"
                    )

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
                    ORDER BY created_at DESC
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [
            (str(row["thread_id"]), dict(row))
            for row in rows
        ]

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
                    raise RunMetadataUnavailable(
                        f"thread {thread_id} does not exist"
                    )

    def claim_hitl_run(
        self,
        run_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Atomically transition one pending HITL run to ``running``."""
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
                cur.execute(
                    "UPDATE agent_runs SET status = status WHERE FALSE"
                )
        return True


__all__ = [
    "RunMetadataStore",
    "RunMetadataUnavailable",
    "ThreadOwnershipError",
    "PostgresRunMetadataStore",
]
