"""SSE 语义阶段进度的回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from lvyan.api.sse import PHASE_TOTAL, RunContext, _stream_graph_events


class _FakeGraph:
    """按预设顺序产出 LangGraph v2 tasks chunk 的最小测试图。"""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = list(chunks)

    async def astream(self, source: Any, config: Any, *, stream_mode=None, version=None):
        for chunk in self._chunks:
            yield chunk


def _task_chunk(kind: str, name: str) -> dict[str, Any]:
    if kind == "input":
        return {"type": "tasks", "data": {"id": name, "name": name, "input": {}}}
    return {"type": "tasks", "data": {"id": name, "name": name, "result": {}}}


def _run(graph: _FakeGraph) -> tuple[list[dict[str, Any]], RunContext]:
    ctx = RunContext("run-phases", "thread-phases", user_id="u1")
    asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )
    events: list[dict[str, Any]] = []
    while not ctx.queue.empty():
        events.append(ctx.queue.get_nowait())
    return events, ctx


def _phases(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event"] in ("phase_start", "phase_progress")]


def test_phase_events_in_forward_order():
    """阶段按真实 DAG 顺序推进，EOF 时完成最后一个已启动阶段。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", "preflight"),
            _task_chunk("result", "preflight"),
            _task_chunk("input", "planner"),
            _task_chunk("result", "planner"),
            _task_chunk("input", "parallel_retrieval"),
            _task_chunk("result", "parallel_retrieval"),
            _task_chunk("input", "composer"),
            _task_chunk("result", "composer"),
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    assert [(event["event"], event["phase"]) for event in phases] == [
        ("phase_start", "material_reading"),
        ("phase_progress", "material_reading"),
        ("phase_start", "fact_analysis"),
        ("phase_progress", "fact_analysis"),
        ("phase_start", "retrieval"),
        ("phase_progress", "retrieval"),
        ("phase_progress", "analysis"),
        ("phase_start", "drafting_validation"),
        ("phase_progress", "drafting_validation"),
    ]
    progresses = [event for event in phases if event["event"] == "phase_progress"]
    assert [event["completed"] for event in progresses] == [1, 2, 3, 4, 5]
    assert all(event["total"] == PHASE_TOTAL for event in phases)
    assert all(event.get("label") for event in phases)


def test_phase_events_skip_intermediate_phases_and_flush_final_completion():
    """只跑 finalizer 也会补齐 1/6 到 6/6。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", "legal_answer_finalizer"),
            _task_chunk("result", "legal_answer_finalizer"),
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    assert [(event["event"], event["phase"]) for event in phases] == [
        ("phase_progress", "material_reading"),
        ("phase_progress", "fact_analysis"),
        ("phase_progress", "retrieval"),
        ("phase_progress", "analysis"),
        ("phase_progress", "drafting_validation"),
        ("phase_start", "generation"),
        ("phase_progress", "generation"),
    ]
    progresses = [event for event in phases if event["event"] == "phase_progress"]
    assert [event["completed"] for event in progresses] == [1, 2, 3, 4, 5, 6]


def test_phase_events_do_not_regress_on_rerun():
    """重跑旧阶段节点不会让单调进度回退或重复发起阶段。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", "preflight"),
            _task_chunk("input", "composer"),
            _task_chunk("input", "legal_reasoner"),
            _task_chunk("result", "legal_reasoner"),
            _task_chunk("input", "composer"),
            _task_chunk("result", "composer"),
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    starts = [event for event in phases if event["event"] == "phase_start"]
    assert [event["phase"] for event in starts] == ["material_reading", "drafting_validation"]
    progresses = [event for event in phases if event["event"] == "phase_progress"]
    assert [event["completed"] for event in progresses] == [1, 2, 3, 4, 5]


def test_phase_events_follow_composer_to_finalizer_main_chain():
    """校验不会在 citation_verifier 之前被宣告完成，完整运行必须到 6/6。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", name)
            for name in (
                "preflight",
                "attachment_retriever",
                "jurisdiction_triage",
                "fact_extractor",
                "missing_fact_assessor",
                "planner",
                "parallel_retrieval",
                "authority_resolver",
                "legal_reasoner",
                "critic",
                "composer",
                "citation_verifier",
                "output_guardrail",
                "legal_answer_finalizer",
            )
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    starts = [event["phase"] for event in phases if event["event"] == "phase_start"]
    progresses = [event for event in phases if event["event"] == "phase_progress"]
    assert starts == [
        "material_reading",
        "fact_analysis",
        "retrieval",
        "analysis",
        "drafting_validation",
        "generation",
    ]
    assert [event["completed"] for event in progresses] == [1, 2, 3, 4, 5, 6]
    assert [event["phase"] for event in progresses] == starts


def test_phase_events_do_not_break_node_events():
    graph = _FakeGraph(
        [
            _task_chunk("input", "preflight"),
            _task_chunk("result", "preflight"),
            _task_chunk("input", "composer"),
            _task_chunk("result", "composer"),
        ]
    )
    events, _ = _run(graph)
    node_events = [event for event in events if event["event"] in ("node_start", "node_end")]
    assert [event["node"] for event in node_events] == [
        "preflight",
        "preflight",
        "composer",
        "composer",
    ]
