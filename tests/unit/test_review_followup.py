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

    class FakeMemory:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_output(self, _thread_id: str) -> None:
            return None

    monkeypatch.setattr(sse, "_get_graph", lambda: FakeGraph())
    monkeypatch.setattr("lvyan.runtime.get_case_memory", lambda: FakeMemory())

    ctx = sse.RunContext("run-1", "thread-1")
    output = await sse.default_runner("问题", "thread-1", "light", ctx)

    events = []
    while not ctx.queue.empty():
        events.append(ctx.queue.get_nowait())
    planner_events = [
        event["event"]
        for event in events
        if event.get("node") == "planner"
    ]
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

        def claim_hitl_run(
            self, run_id: str, user_id: str
        ) -> dict[str, Any] | None:
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

    manager = sse.RunManager(metadata_store=FakeStore())

    async def fake_resume(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(manager, "_resume_drive", fake_resume)
    monkeypatch.setattr(sse, "_get_graph", lambda: FakeGraph())
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

    shared = SharedStore()
    managers = [
        sse.RunManager(metadata_store=shared),
        sse.RunManager(metadata_store=shared),
    ]

    async def fake_resume(*_args: Any, **_kwargs: Any) -> None:
        return None

    for manager in managers:
        monkeypatch.setattr(manager, "_resume_drive", fake_resume)
    monkeypatch.setattr(sse, "_get_graph", lambda: FakeGraph())
    monkeypatch.setattr("lvyan.api.auth.is_auth_enabled", lambda: True)

    results = await asyncio.gather(*(
        manager.resolve_hitl(
            "run-db",
            HITLRequest(action="approve"),
            current_user_id="user-a",
        )
        for manager in managers
    ))
    await asyncio.sleep(0)

    assert sorted(status for status, _message in results) == ["error", "resolved"]


class _ApiMemory:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def list_threads(self):
        return list(self.items.items())

    def load(self, _thread_id: str):
        return None

    def register(self, thread_id: str, **metadata: Any) -> None:
        self.items[thread_id] = metadata

    def mark_output(self, _thread_id: str) -> None:
        return None

    def delete(self, thread_id: str) -> None:
        self.items.pop(thread_id, None)


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
    assert any(
        event.get("code") == "run_non_recoverable"
        for event in events
    )


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

    assert response.status_code == 403


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

    class Memory:
        def register(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def mark_output(self, _thread_id: str) -> None:
            return None

    manager = sse.RunManager(metadata_store=FailingStore())
    ctx = manager._bind_context(sse.RunContext("run-1", "thread-1"))
    manager._runs[ctx.run_id] = ctx
    monkeypatch.setattr(sse, "_get_graph", lambda: FakeGraph())
    monkeypatch.setattr("lvyan.runtime.get_case_memory", lambda: Memory())

    output = await sse.default_runner("问题", "thread-1", "light", ctx)

    events = []
    while not ctx.queue.empty():
        events.append(ctx.queue.get_nowait())
    assert output == ""
    assert ctx.status == "failed"
    assert not any(event.get("event") == "hitl_required" for event in events)
    assert any(
        event.get("code") == "hitl_persistence_failed"
        for event in events
    )


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

    manager = sse.RunManager(metadata_store=FailingStore())
    ctx = manager._bind_context(sse.RunContext("run-1", "thread-1"))
    manager._runs[ctx.run_id] = ctx
    monkeypatch.setattr(sse, "_get_graph", lambda: FakeGraph())

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
    assert any(
        event.get("code") == "hitl_persistence_failed"
        for event in events
    )


def test_delete_thread_uses_durable_store_and_local_cleanup():
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app

    calls: list[tuple[str, str]] = []
    memory = _ApiMemory()
    memory.items["thread-1"] = {"user_id": "anonymous"}

    class Store:
        def get_thread(self, thread_id: str):
            return {"thread_id": thread_id, "user_id": "anonymous"}

        def delete_thread(self, thread_id: str, user_id: str) -> bool:
            calls.append((thread_id, user_id))
            return True

    async def runner(*_args: Any, **_kwargs: Any) -> str:
        return ""

    response = TestClient(create_app(
        runner=runner,
        memory=memory,
        metadata_store=Store(),
    )).delete("/api/agent/state/thread-1")

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

    response = TestClient(create_app(
        runner=runner,
        memory=_ApiMemory(),
        metadata_store=Store(),
    )).get("/api/agent/threads")

    assert response.status_code == 200
    assert response.json()["threads"][0]["thread_id"] == "thread-db"


def test_hitl_metadata_unavailable_returns_503(monkeypatch):
    from fastapi.testclient import TestClient
    from lvyan.api.server import create_app
    from lvyan.api.sse import RunManager

    async def unavailable(*_args: Any, **_kwargs: Any):
        return ("unavailable", "审批状态暂时不可用")

    monkeypatch.setattr(RunManager, "resolve_hitl", unavailable)
    response = TestClient(create_app(
        runner=lambda *_args, **_kwargs: None,
        memory=_ApiMemory(),
    )).post(
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

    response = TestClient(server.create_app(
        runner=lambda *_args, **_kwargs: None,
        memory=_ApiMemory(),
    )).get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not-ready"
