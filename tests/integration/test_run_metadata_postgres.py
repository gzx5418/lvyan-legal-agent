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
            claims = list(executor.map(
                claim,
                (first, second),
            ))

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
