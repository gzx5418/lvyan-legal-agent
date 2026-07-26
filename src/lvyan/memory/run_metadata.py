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
                        user_id = EXCLUDED.user_id,
                        title = CASE
                            WHEN EXCLUDED.title = '' THEN agent_threads.title
                            ELSE EXCLUDED.title
                        END,
                        complexity = EXCLUDED.complexity
                    """,
                    (thread_id, user_id, title, complexity),
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

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, thread_id, user_id, status,
                           interrupt_payload, created_at
                    FROM agent_runs
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else None


__all__ = ["RunMetadataStore", "PostgresRunMetadataStore"]
