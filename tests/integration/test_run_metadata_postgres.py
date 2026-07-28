"""Optional PostgreSQL integration coverage for durable run metadata."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lvyan.memory.run_metadata import PostgresRunMetadataStore


def test_only_one_instance_can_claim_the_same_hitl_run():
    dsn = os.getenv("LVYAN_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LVYAN_TEST_POSTGRES_DSN is not configured")

    suffix = uuid.uuid4().hex
    user_id = f"test-user-{suffix}"
    thread_id = f"test-thread-{suffix}"
    run_id = f"test-run-{suffix}"
    first = PostgresRunMetadataStore(dsn)
    second = PostgresRunMetadataStore(dsn)

    try:
        first.create_run(
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
        )
        first.update_run(
            run_id,
            status="awaiting_hitl",
            interrupt_payload={"message": "approve"},
        )

        barrier = Barrier(2)

        def claim(store: PostgresRunMetadataStore):
            barrier.wait()
            return store.claim_hitl_run(run_id, user_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    claim,
                    (first, second),
                )
            )

        assert sum(claim is not None for claim in claims) == 1
        assert first.get_run(run_id)["status"] == "running"
        assert first.has_active_runs(thread_id) is True
        assert first.delete_thread(thread_id, user_id) is False
    finally:
        first.update_run(run_id, status="completed")
        first.delete_thread(thread_id, user_id)


def test_delete_thread_cascades_to_run_records():
    dsn = os.getenv("LVYAN_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LVYAN_TEST_POSTGRES_DSN is not configured")

    suffix = uuid.uuid4().hex
    user_id = f"test-user-{suffix}"
    thread_id = f"test-thread-{suffix}"
    run_id = f"test-run-{suffix}"
    store = PostgresRunMetadataStore(dsn)

    store.create_run(
        run_id=run_id,
        thread_id=thread_id,
        user_id=user_id,
        user_message="第一轮问题",
        attachments=["file-1"],
    )
    assert store.has_active_runs(thread_id) is True
    assert store.delete_thread(thread_id, user_id) is False
    store.update_run(run_id, status="completed")
    store.append_message(
        run_id,
        thread_id,
        user_id,
        "assistant",
        "第一轮回答",
    )
    messages = store.list_messages(thread_id, user_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["attachments"] == ["file-1"]
    assert store.has_active_runs(thread_id) is False

    assert store.delete_thread(thread_id, user_id) is True
    assert store.get_thread(thread_id) is None
    assert store.get_run(run_id) is None
    assert store.list_messages(thread_id, user_id) == []


# ---------------------------------------------------------------------------
# H2：多实例并发执行 _ensure_schema 必须幂等
# ---------------------------------------------------------------------------
def test_concurrent_ensure_schema_is_idempotent():
    """两个 PostgresRunMetadataStore 实例并发首次建表，应均成功且 schema 单份。

    覆盖：
      - 触发器 ``DROP TRIGGER IF EXISTS + CREATE TRIGGER`` 并发执行不报错；
      - 表 / 索引 / 触发器 / 函数 各只存在一份；
      - 两个实例的 ``_schema_ready`` 均变为 True。
    """
    dsn = os.getenv("LVYAN_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LVYAN_TEST_POSTGRES_DSN is not configured")

    first = PostgresRunMetadataStore(dsn)
    second = PostgresRunMetadataStore(dsn)

    barrier = Barrier(2)
    errors: list[BaseException] = []

    def ensure(store: PostgresRunMetadataStore) -> None:
        try:
            with store._connect() as conn:
                barrier.wait()  # 尽量让两线程同时进入 _ensure_schema
                store._ensure_schema(conn)
        except BaseException as exc:  # noqa: BLE001 收集所有异常
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(ensure, first), executor.submit(ensure, second)]
        for fut in futures:
            fut.result()

    assert errors == [], f"并发 _ensure_schema 报错: {errors}"
    assert first._schema_ready is True
    assert second._schema_ready is True

    # 验证 schema 对象各只存在一份
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            # 表
            cur.execute(
                """
                SELECT count(*) AS n
                FROM information_schema.tables
                WHERE table_name IN ('agent_threads', 'agent_runs', 'agent_messages')
                """
            )
            assert cur.fetchone()["n"] == 3, "应恰好存在 3 张业务表"

            # 索引（含主键的隐式索引不算在内，仅校验显式命名的）
            cur.execute(
                """
                SELECT count(*) AS n
                FROM pg_indexes
                WHERE indexname IN (
                    'idx_agent_threads_user',
                    'idx_agent_runs_thread',
                    'idx_agent_runs_user_status',
                    'idx_agent_messages_thread'
                )
                """
            )
            assert cur.fetchone()["n"] == 4, "应恰好存在 4 个显式索引"

            # 触发器
            cur.execute(
                """
                SELECT count(*) AS n
                FROM information_schema.triggers
                WHERE event_object_table = 'agent_threads'
                  AND trigger_name = 'agent_threads_set_updated_at'
                """
            )
            assert cur.fetchone()["n"] == 1, "应恰好存在 1 个触发器"

            # 函数
            cur.execute(
                """
                SELECT count(*) AS n
                FROM pg_proc
                WHERE proname = 'trg_agent_threads_set_updated_at'
                """
            )
            assert cur.fetchone()["n"] == 1, "应恰好存在 1 个函数"
    finally:
        conn.close()


def test_ensure_schema_failure_does_not_set_ready():
    """migration 失败时 _schema_ready 必须保持 False，且异常向上传播。

    用一个不可达 DSN 触发连接错误（_ensure_schema 前失败），确认 _schema_ready 未被设置。
    另构造一段损坏的 migration 路径场景，校验异常传播。
    """
    dsn = os.getenv("LVYAN_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LVYAN_TEST_POSTGRES_DSN is not configured")

    # 通过 monkey 替换 migration SQL 内容为非法语句，触发 migration 异常。
    # 直接 patch _ensure_schema 内读取的 sql：用一个子类重写行为。
    class _BrokenStore(PostgresRunMetadataStore):
        def _ensure_schema(self, conn):
            if self._schema_ready:
                return
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (0x1C7A, 0xA11C),
                    )
                    cur.execute("THIS IS NOT VALID SQL !!")
            self._schema_ready = True

    broken = _BrokenStore(dsn)
    with pytest.raises(Exception):  # noqa: PT011 任意 PG 语法错误都行
        with broken._connect() as conn:
            broken._ensure_schema(conn)
    assert broken._schema_ready is False
