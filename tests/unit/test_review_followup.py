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
