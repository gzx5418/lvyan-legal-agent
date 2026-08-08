"""Regression tests for the fourth review's production entry points."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest


def test_agent_run_request_accepts_law_as_of_date():
    from lvyan.api.models import AgentRunRequest

    request = AgentRunRequest(
        query="2018 年合同争议适用什么法律？",
        law_as_of_date="2018-06-01",
    )
    assert request.law_as_of_date == date(2018, 6, 1)


def test_parallel_retrieval_passes_law_as_of_date(monkeypatch):
    from lvyan.nodes import retrieve_statutes as module

    calls: list[dict[str, Any]] = []

    def fake_search(query: str, **kwargs: Any) -> list[Any]:
        calls.append({"query": query, **kwargs})
        return []

    monkeypatch.setattr(module, "search_statutes", fake_search)
    monkeypatch.setattr(module, "search_cases", lambda *_a, **_kw: None)

    module.parallel_retrieval(
        {
            "retrieval_queries": [{"query_text": "合同违约"}],
            "law_as_of_date": date(2018, 6, 1),
            "user_goal": "合同纠纷",
            "plan": [],
        }
    )

    assert calls == [
        {
            "query": "合同违约",
            "as_of": date(2018, 6, 1),
            "top_k": 10,
        }
    ]


def test_authority_status_passes_validation_date_to_status_lookup(monkeypatch):
    from lvyan.schemas.authority import Authority
    from lvyan.validators import authority_status as module

    seen: list[date | None] = []

    def fake_verify(_source_id: str, as_of: date | None = None):
        seen.append(as_of)
        return SimpleNamespace(
            current_status="effective",
            superseded_by=None,
        )

    monkeypatch.setattr(module, "verify_statute_status", fake_verify)
    authority = Authority(
        source_id="old-contract-law",
        title="合同法",
        article_text="依法成立的合同受法律保护。",
        authority_level="法律",
        status="repealed",
        retrieved_at=datetime.now(timezone.utc),
    )

    report = module.validate_authority_status(
        [authority],
        current_date=date(2018, 6, 1),
    )

    assert seen == [date(2018, 6, 1)]
    assert report.passed is True


def test_repealed_now_but_effective_as_of_passes(monkeypatch):
    from lvyan.schemas.authority import Authority
    from lvyan.validators import authority_status as module

    monkeypatch.setattr(
        module,
        "verify_statute_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            current_status="repealed",
            is_effective_as_of=True,
            superseded_by="civil-code",
        ),
    )
    historical_law = Authority(
        source_id="contract-law",
        title="中华人民共和国合同法",
        article_text="依法成立的合同受法律保护。",
        authority_level="法律",
        effective_date=date(1999, 10, 1),
        expiry_date=date(2021, 1, 1),
        status="repealed",
        retrieved_at=datetime.now(timezone.utc),
    )

    report = module.validate_authority_status(
        [historical_law],
        current_date=date(2018, 6, 1),
    )

    assert report.passed is True


@pytest.mark.asyncio
async def test_default_runner_emits_each_task_once_and_no_error_for_none(monkeypatch):
    from lvyan.api import sse

    class FakeGraph:
        async def astream(self, *_args: Any, **_kwargs: Any):
            yield {
                "type": "tasks",
                "data": {"id": "1", "name": "planner", "input": {}},
            }
            yield {
                "type": "updates",
                "data": {"planner": {"case_type": "合同纠纷"}},
            }
            yield {
                "type": "tasks",
                "data": {
                    "id": "1",
                    "name": "planner",
                    "result": {"ok": True},
                    "error": None,
                },
            }
            yield {
                "type": "updates",
                "data": {"composer": {"final_output": "完成"}},
            }

        def get_state(self, _config: dict[str, Any]):
            return SimpleNamespace(next=(), values={}, tasks=())

        async def aget_state(self, _config: dict[str, Any]):
            return SimpleNamespace(next=(), values={}, tasks=())

    class FakeMemory:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_output(self, _thread_id: str) -> None:
            return None

    async def _fake_get_graph() -> Any:
        return FakeGraph()

    monkeypatch.setattr(sse, "_get_graph", _fake_get_graph)
    monkeypatch.setattr("lvyan.runtime.get_case_memory", lambda: FakeMemory())

    ctx = sse.RunContext("run-1", "thread-1")
    output = await sse.default_runner("问题", "thread-1", "light", ctx)

    events = []
    while not ctx.queue.empty():
        events.append(ctx.queue.get_nowait())
    planner_events = [event["event"] for event in events if event.get("node") == "planner"]
    assert output == "完成"
    assert planner_events == ["node_start", "node_end"]
    assert not any(event["event"] == "node_error" for event in events)


def test_citation_verifier_audits_final_output_with_empty_statutes():
    from lvyan.nodes.citation_verifier import citation_verifier

    result = citation_verifier(
        {
            "current_date": date(2026, 7, 26),
            "law_as_of_date": date(2018, 6, 1),
            "final_output": "依据《中华人民共和国合同法》第一百零七条，应承担违约责任。",
            "reasoning_result": None,
            "statutes": [],
            "iteration": 2,
            "retrieval_queries": [],
            "user_goal": "合同纠纷",
        }
    )

    audit = result["citation_audit"]
    assert audit["passed"] is False
    assert audit["total_citations"] == 1
    assert audit["fabricated"] == 1


@pytest.mark.asyncio
async def test_checkpoint_hitl_uses_run_metadata_without_sidecar(monkeypatch):
    from lvyan.api import sse
    from lvyan.api.models import HITLRequest

    class FakeStore:
        claimed = False

        def get_run(self, run_id: str) -> dict[str, Any] | None:
            assert run_id == "run-db"
            return {
                "run_id": run_id,
                "thread_id": "thread-db",
                "user_id": "user-a",
                "status": "awaiting_hitl",
                "created_at": 1.0,
            }

        def update_run(self, _run_id: str, **_values: Any) -> None:
            return None

        def claim_hitl_run(self, run_id: str, user_id: str) -> dict[str, Any] | None:
            assert run_id == "run-db"
            assert user_id == "user-a"
            if self.claimed:
                return None
            self.claimed = True
            return {
                "run_id": run_id,
                "thread_id": "thread-db",
                "user_id": user_id,
                "status": "running",
            }

    class FakeGraph:
        def get_state(self, config: dict[str, Any]):
            assert config["configurable"]["thread_id"] == "thread-db"
            interrupt = SimpleNamespace(value={"message": "approve"})
            task = SimpleNamespace(interrupts=[interrupt])
            return SimpleNamespace(
                next=("output_guardrail",),
                values={"run_id": "run-db", "user_id": "user-a"},
                tasks=(task,),
            )

        async def aget_state(self, config: dict[str, Any]):
            return self.get_state(config)

    manager = sse.RunManager(metadata_store=FakeStore())

    async def fake_resume(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_resume_drive", fake_resume)

    async def _fake_get_graph() -> Any:
        return FakeGraph()

    monkeypatch.setattr(sse, "_get_graph", _fake_get_graph)
    monkeypatch.setattr("lvyan.api.auth.is_auth_enabled", lambda: True)

    status, _message = await manager._resolve_hitl_from_checkpoint(
        "run-db",
        HITLRequest(action="approve"),
        current_user_id="user-a",
    )
    await asyncio.sleep(0)

    assert status == "resolved"
    assert manager.get("run-db").thread_id == "thread-db"


@pytest.mark.asyncio
async def test_in_process_hitl_claims_metadata_before_resume(monkeypatch):
    from lvyan.api import sse
    from lvyan.api.models import HITLRequest

    calls: list[tuple[str, str]] = []

    class FakeStore:
        def claim_hitl_run(self, run_id: str, user_id: str):
            calls.append((run_id, user_id))
            return {
                "run_id": run_id,
                "thread_id": "thread-1",
                "user_id": user_id,
                "status": "running",
            }

        def get_run(self, _run_id: str):
            return None

        def update_run(self, _run_id: str, **_values: Any) -> None:
            return None

    manager = sse.RunManager(metadata_store=FakeStore())
    ctx = sse.RunContext("run-1", "thread-1", user_id="user-a")
    ctx.status = "awaiting_hitl"
    manager._runs[ctx.run_id] = ctx

    async def fake_resume(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_resume_drive", fake_resume)
    status, _message = await manager.resolve_hitl(
        "run-1",
        HITLRequest(action="approve"),
        current_user_id="user-a",
    )
    await asyncio.sleep(0)

    assert status == "resolved"
    assert calls == [("run-1", "user-a")]
    assert ctx.status == "running"


@pytest.mark.asyncio
async def test_two_instances_only_one_can_claim_hitl(monkeypatch):
    from lvyan.api import sse
    from lvyan.api.models import HITLRequest

    class SharedStore:
        status = "awaiting_hitl"

        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "thread_id": "thread-db",
                "user_id": "user-a",
                "status": self.status,
                "created_at": 1.0,
            }

        def claim_hitl_run(self, run_id: str, user_id: str):
            if self.status != "awaiting_hitl":
                return None
            self.status = "running"
            return {
                "run_id": run_id,
                "thread_id": "thread-db",
                "user_id": user_id,
                "status": "running",
            }

        def update_run(self, _run_id: str, **_values: Any) -> None:
            return None

    class FakeGraph:
        def get_state(self, _config: dict[str, Any]):
            interrupt = SimpleNamespace(value={"message": "approve"})
            return SimpleNamespace(
                next=("output_guardrail",),
                values={"run_id": "run-db", "user_id": "user-a"},
                tasks=(SimpleNamespace(interrupts=[interrupt]),),
            )

        async def aget_state(self, config: dict[str, Any]):
            return self.get_state(config)

    shared = SharedStore()
    managers = [
        sse.RunManager(metadata_store=shared),
        sse.RunManager(metadata_store=shared),
    ]

    async def fake_resume(*_args: Any, **_kwargs: Any) -> None:
        return None

    for manager in managers:
        monkeypatch.setattr(manager, "_resume_drive", fake_resume)

    async def _fake_get_graph() -> Any:
        return FakeGraph()

    monkeypatch.setattr(sse, "_get_graph", _fake_get_graph)
    monkeypatch.setattr("lvyan.api.auth.is_auth_enabled", lambda: True)

    results = await asyncio.gather(
        *(
            manager.resolve_hitl(
                "run-db",
                HITLRequest(action="approve"),
                current_user_id="user-a",
            )
            for manager in managers
        )
    )
    await asyncio.sleep(0)

    assert sorted(status for status, _message in results) == ["error", "resolved"]


class _ApiMemory:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def list_threads(self):
        return list(self.items.items())

    def load(self, _thread_id: str):
        return None

    def load_strict(self, thread_id: str):
        return self.load(thread_id)

    def register(self, thread_id: str, **metadata: Any) -> None:
        self.items[thread_id] = metadata

    def mark_output(self, _thread_id: str) -> None:
        return None

    def delete(self, thread_id: str) -> None:
        self.items.pop(thread_id, None)

    def delete_strict(self, thread_id: str) -> bool:
        self.delete(thread_id)
        return True


def test_metadata_creation_failure_returns_503():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class FailingStore:
        def create_run(self, *_args: Any, **_kwargs: Any) -> None:
            raise OSError("database unavailable")

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "should not run"

    app = create_app(
        runner=runner,
        memory=_ApiMemory(),
        metadata_store=FailingStore(),
    )
    response = TestClient(app).post(
        "/api/agent/run",
        json={"query": "合同问题"},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_metadata_update_failure_marks_run_non_recoverable():
    from lvyan.api import sse

    class Store:
        def update_run(self, _run_id: str, **_values: Any) -> None:
            raise OSError("database unavailable")

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "result"

    manager = sse.RunManager(runner=runner, metadata_store=Store())
    ctx = sse.RunContext("run-1", "thread-1")
    manager._runs[ctx.run_id] = ctx

    await manager._drive(ctx, "question", "light")

    events = []
    while not ctx.queue.empty():
        event = ctx.queue.get_nowait()
        if event is not None:
            events.append(event)
    assert ctx.non_recoverable is True
    assert any(event.get("code") == "run_non_recoverable" for event in events)


def test_durable_thread_owner_cannot_be_overwritten(monkeypatch):
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Store:
        def get_thread(self, thread_id: str):
            assert thread_id == "thread-owned"
            return {"thread_id": thread_id, "user_id": "user-a"}

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "should not run"

    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = create_app(
        runner=runner,
        memory=_ApiMemory(),
        metadata_store=Store(),
    )
    response = TestClient(app).post(
        "/api/agent/run",
        headers={"X-User-ID": "user-b"},
        json={"query": "合同问题", "thread_id": "thread-owned"},
    )

    # 跨租户资源统一返回 404，避免泄露 thread 是否存在。
    assert response.status_code == 404


def test_cross_instance_stream_returns_completed_output():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Store:
        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "thread_id": "thread-1",
                "user_id": "anonymous",
                "status": "completed",
                "final_output": "durable result",
                "error": None,
            }

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return ""

    app = create_app(
        runner=runner,
        memory=_ApiMemory(),
        metadata_store=Store(),
    )
    response = TestClient(app).get("/api/agent/stream/run-other-instance")

    assert response.status_code == 200
    assert "durable result" in response.text


def test_cross_instance_running_stream_requires_affinity():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Store:
        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "thread_id": "thread-1",
                "user_id": "anonymous",
                "status": "running",
            }

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return ""

    app = create_app(
        runner=runner,
        memory=_ApiMemory(),
        metadata_store=Store(),
    )
    response = TestClient(app).get("/api/agent/stream/run-other-instance")

    assert response.status_code == 409
    assert "session affinity" in response.json()["detail"]


def test_cross_instance_cancelled_stream_returns_cancel_event():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Store:
        def get_run(self, run_id: str):
            return {
                "run_id": run_id,
                "thread_id": "thread-1",
                "user_id": "anonymous",
                "status": "cancelled",
                "error": "用户已停止生成",
            }

    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=_ApiMemory(),
            metadata_store=Store(),
        )
    ).get("/api/agent/stream/run-cancelled")

    assert response.status_code == 200
    assert '"event": "cancelled"' in response.text


@pytest.mark.asyncio
async def test_hitl_persistence_failure_does_not_publish_approval(monkeypatch):
    from lvyan.api import sse

    class FailingStore:
        def update_run(self, _run_id: str, **_values: Any) -> None:
            raise OSError("database unavailable")

    class FakeGraph:
        async def astream(self, *_args: Any, **_kwargs: Any):
            if False:
                yield None

        def get_state(self, _config: dict[str, Any]):
            interrupt = SimpleNamespace(value={"message": "approve"})
            return SimpleNamespace(
                next=("output_guardrail",),
                values={"run_id": "run-1"},
                tasks=(SimpleNamespace(interrupts=[interrupt]),),
            )

        async def aget_state(self, config: dict[str, Any]):
            return self.get_state(config)

    class Memory:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_output(self, _thread_id: str) -> None:
            return None

    manager = sse.RunManager(metadata_store=FailingStore())
    ctx = manager._bind_context(sse.RunContext("run-1", "thread-1"))
    manager._runs[ctx.run_id] = ctx

    async def _fake_get_graph() -> Any:
        return FakeGraph()

    monkeypatch.setattr(sse, "_get_graph", _fake_get_graph)
    monkeypatch.setattr("lvyan.runtime.get_case_memory", lambda: Memory())

    output = await sse.default_runner("问题", "thread-1", "light", ctx)

    events = []
    while not ctx.queue.empty():
        events.append(ctx.queue.get_nowait())
    assert output == ""
    assert ctx.status == "failed"
    assert not any(event.get("event") == "hitl_required" for event in events)
    assert any(event.get("code") == "hitl_persistence_failed" for event in events)


@pytest.mark.asyncio
async def test_second_hitl_persistence_failure_closes_without_approval(
    monkeypatch,
):
    from lvyan.api import sse

    class FailingStore:
        def update_run(self, _run_id: str, **_values: Any) -> None:
            raise OSError("database unavailable")

    class FakeGraph:
        async def astream(self, *_args: Any, **_kwargs: Any):
            if False:
                yield None

        def get_state(self, _config: dict[str, Any]):
            interrupt = SimpleNamespace(value={"message": "approve again"})
            return SimpleNamespace(
                next=("output_guardrail",),
                values={"run_id": "run-1"},
                tasks=(SimpleNamespace(interrupts=[interrupt]),),
            )

        async def aget_state(self, config: dict[str, Any]):
            return self.get_state(config)

    manager = sse.RunManager(metadata_store=FailingStore())
    ctx = manager._bind_context(sse.RunContext("run-1", "thread-1"))
    manager._runs[ctx.run_id] = ctx

    async def _fake_get_graph() -> Any:
        return FakeGraph()

    monkeypatch.setattr(sse, "_get_graph", _fake_get_graph)

    await manager._resume_drive(
        ctx,
        command={"resume": "approve"},
        config={"configurable": {"thread_id": "thread-1"}},
    )

    events = []
    while not ctx.queue.empty():
        event = ctx.queue.get_nowait()
        if event is not None:
            events.append(event)
    assert ctx.status == "failed"
    assert not any(event.get("event") == "hitl_required" for event in events)
    assert any(event.get("code") == "hitl_persistence_failed" for event in events)


def test_delete_thread_uses_durable_store_and_local_cleanup():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    calls: list[tuple[str, str]] = []
    memory = _ApiMemory()
    memory.items["thread-1"] = {"user_id": "anonymous"}

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

        def has_active_runs(self, _thread_id: str) -> bool:
            return False

        def delete_thread(self, thread_id: str, user_id: str) -> bool:
            calls.append((thread_id, user_id))
            return True

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return ""

    response = TestClient(
        create_app(
            runner=runner,
            memory=memory,
            metadata_store=Store(),
        )
    ).delete("/api/agent/state/thread-1")

    assert response.status_code == 200
    assert calls == [("thread-1", "anonymous")]
    assert "thread-1" not in memory.items


def test_thread_list_uses_durable_store_not_sidecar():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Store:
        def list_threads(self, user_id: str):
            assert user_id == "anonymous"
            return [
                (
                    "thread-db",
                    {
                        "thread_id": "thread-db",
                        "user_id": user_id,
                        "title": "数据库会话",
                        "complexity": "deep",
                        "created_at": 1.0,
                        "has_output": True,
                    },
                )
            ]

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return ""

    response = TestClient(
        create_app(
            runner=runner,
            memory=_ApiMemory(),
            metadata_store=Store(),
        )
    ).get("/api/agent/threads")

    assert response.status_code == 200
    assert response.json()["threads"][0]["thread_id"] == "thread-db"


def test_hitl_metadata_unavailable_returns_503(monkeypatch):
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app
    from lvyan.api.sse import RunManager

    async def unavailable(*_args: Any, **_kwargs: Any):
        return ("unavailable", "审批状态暂时不可用")

    monkeypatch.setattr(RunManager, "resolve_hitl", unavailable)
    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=_ApiMemory(),
        )
    ).post(
        "/api/agent/hitl/run-1",
        json={"action": "approve"},
    )

    assert response.status_code == 503


def test_readyz_returns_503_when_dependency_not_ready(monkeypatch):
    from fastapi.testclient import TestClient
    from lvyan.api import server

    monkeypatch.setattr(server, "_check_database_ready", lambda: "unavailable")
    monkeypatch.setattr(server, "_check_retrieval", lambda: "ok")
    monkeypatch.setattr(server, "_check_model_gateway_ready", lambda: "ok")

    response = TestClient(
        server.create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=_ApiMemory(),
        )
    ).get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not-ready"


def test_checkpoint_delete_failure_returns_503_without_deleting_metadata():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    metadata_deleted = False

    class Memory(_ApiMemory):
        def delete_strict(self, _thread_id: str) -> bool:
            raise OSError("checkpoint unavailable")

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

        def has_active_runs(self, _thread_id: str) -> bool:
            return False

        def delete_thread(self, _thread_id: str, _user_id: str) -> bool:
            nonlocal metadata_deleted
            metadata_deleted = True
            return True

    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=Memory(),
            metadata_store=Store(),
        )
    ).delete("/api/agent/state/thread-1")

    assert response.status_code == 503
    assert metadata_deleted is False


def test_active_durable_run_blocks_thread_deletion():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Memory(_ApiMemory):
        def delete_strict(self, _thread_id: str) -> bool:
            raise AssertionError("active thread checkpoint must not be deleted")

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

        def has_active_runs(self, _thread_id: str) -> bool:
            return True

    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=Memory(),
            metadata_store=Store(),
        )
    ).delete("/api/agent/state/thread-1")

    assert response.status_code == 409


def test_checkpoint_read_failure_returns_503():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Memory(_ApiMemory):
        def load_strict(self, _thread_id: str):
            raise OSError("checkpoint unavailable")

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=Memory(),
            metadata_store=Store(),
        )
    ).get("/api/agent/state/thread-1")

    assert response.status_code == 503


@pytest.mark.parametrize("operation", ["update_run", "mark_thread_output"])
def test_metadata_zero_row_update_fails(operation, monkeypatch):
    from lvyan.memory.run_metadata import (
        PostgresRunMetadataStore,
        RunMetadataUnavailable,
    )

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def cursor(self):
            return Cursor()

    store = PostgresRunMetadataStore("postgresql://unused")
    store._schema_ready = True
    monkeypatch.setattr(store, "_connect", lambda: Connection())

    with pytest.raises(RunMetadataUnavailable):
        if operation == "update_run":
            store.update_run("missing-run", status="running")
        else:
            store.mark_thread_output("missing-thread")


@pytest.mark.asyncio
async def test_completion_persistence_failure_sends_warning_before_result():
    from lvyan.api import sse

    class Store:
        def update_run(self, _run_id: str, **values: Any) -> None:
            if values.get("status") == "completed":
                raise OSError("database unavailable")

        def mark_thread_output(self, _thread_id: str) -> None:
            return None

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "generated result"

    manager = sse.RunManager(runner=runner, metadata_store=Store())
    ctx = sse.RunContext("run-1", "thread-1")
    manager._runs[ctx.run_id] = ctx

    await manager._drive(ctx, "question", "light")

    events = []
    while not ctx.queue.empty():
        event = ctx.queue.get_nowait()
        if event is not None:
            events.append(event)
    event_codes = [event.get("code") for event in events]
    event_names = [event.get("event") for event in events]
    assert "completion_not_persisted" in event_codes
    assert event_names.index("warning") < event_names.index("final_output")


def test_state_returns_complete_durable_message_history():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    class Memory(_ApiMemory):
        def load_strict(self, _thread_id: str):
            return SimpleNamespace(
                run_id="run-2",
                thread_id="thread-1",
                jurisdiction="中国大陆",
                case_type="合同纠纷",
                complexity="light",
                risk_level="low",
                confidence="medium",
                iteration=1,
                final_output="第二轮回答",
                facts=[],
                statutes=[],
                cases=[],
            )

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

        def list_messages(self, thread_id: str, user_id: str):
            assert (thread_id, user_id) == ("thread-1", "anonymous")
            return [
                {
                    "run_id": "run-1",
                    "role": "user",
                    "content": "第一轮问题",
                    "attachments": [],
                    "created_at": 1.0,
                },
                {
                    "run_id": "run-1",
                    "role": "assistant",
                    "content": "第一轮回答",
                    "attachments": [],
                    "created_at": 2.0,
                },
                {
                    "run_id": "run-2",
                    "role": "user",
                    "content": "第二轮追问",
                    "attachments": ["file-1"],
                    "created_at": 3.0,
                },
            ]

    response = TestClient(
        create_app(
            runner=lambda *_args, **_kwargs: None,
            memory=Memory(),
            metadata_store=Store(),
        )
    ).get("/api/agent/state/thread-1")

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [message["content"] for message in messages] == [
        "第一轮问题",
        "第一轮回答",
        "第二轮追问",
    ]
    assert messages[-1]["attachments"] == ["file-1"]


@pytest.mark.asyncio
async def test_create_run_persists_user_message_and_attachments():
    from lvyan.api import sse

    created: dict[str, Any] = {}

    class Store:
        def create_run(self, *_args: Any, **kwargs: Any) -> None:
            created.update(kwargs)

        def update_run(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_thread_output(self, _thread_id: str) -> None:
            return None

        def append_message(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "answer"

    manager = sse.RunManager(runner=runner, metadata_store=Store())
    ctx = manager.create_run(
        "# 待分析证据\n敏感文档全文\n# 用户问题\n用户问题",
        "thread-1",
        "light",
        attachments=["file-1"],
        display_query="用户问题",
    )
    await manager._tasks[ctx.run_id]

    assert created["user_message"] == "用户问题"
    assert created["title"] == "用户问题"
    assert created["attachments"] == ["file-1"]


@pytest.mark.asyncio
async def test_completion_persists_assistant_message():
    from lvyan.api import sse

    messages: list[tuple[str, str]] = []

    class Store:
        def update_run(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_thread_output(self, _thread_id: str) -> None:
            return None

        def append_message(
            self,
            _run_id: str,
            _thread_id: str,
            _user_id: str,
            role: str,
            content: str,
            _attachments: list[str] | None,
        ) -> None:
            messages.append((role, content))

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return "完整回答"

    manager = sse.RunManager(runner=runner, metadata_store=Store())
    ctx = sse.RunContext("run-1", "thread-1")
    manager._runs[ctx.run_id] = ctx

    await manager._drive(ctx, "问题", "light")

    assert messages == [("assistant", "完整回答")]


@pytest.mark.asyncio
async def test_cancel_run_stops_background_task_and_emits_event():
    from lvyan.api import sse

    started = asyncio.Event()

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        started.set()
        await asyncio.sleep(60)
        return "should not complete"

    manager = sse.RunManager(runner=runner)
    ctx = manager.create_run("问题", "thread-1", "light")
    await started.wait()

    status, _message = await manager.cancel_run(ctx.run_id, "anonymous")

    events = []
    while not ctx.queue.empty():
        event = ctx.queue.get_nowait()
        if event is not None:
            events.append(event)
    assert status == "cancelled"
    assert ctx.status == "cancelled"
    assert any(event.get("event") == "cancelled" for event in events)
    assert ctx.run_id not in manager._tasks
