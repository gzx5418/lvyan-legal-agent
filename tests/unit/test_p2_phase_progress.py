"""P2 回归测试：SSE 语义阶段进度事件（phase_start / phase_progress）。

验证核心不变量：
1. 节点按阶段分组，后端在「新阶段首个节点启动」时发布 phase_start；
2. 阶段前进时补齐已完成的中间阶段（phase_progress，completed 为累计数）；
3. 重跑旧阶段节点（critic 回退重试 legal_reasoner）不使进度回退；
4. node_start / node_end 事件不受影响（向后兼容）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from lvyan.api.sse import PHASE_TOTAL, RunContext, _stream_graph_events


class _FakeGraph:
    """Fake graph：astream 依次产出预设的 chunk 列表（v2 格式）。"""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = list(chunks)

    async def astream(self, source: Any, config: Any, *, stream_mode=None, version=None):
        for chunk in self._chunks:
            yield chunk


def _task_chunk(kind: str, name: str) -> dict[str, Any]:
    """构造 v2 格式 tasks chunk：kind=input 表示节点开始；result 表示结束。"""
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
    return [e for e in events if e["event"] in ("phase_start", "phase_progress")]


# ---------------------------------------------------------------------------
# 1. 顺序推进：阶段按序启动、中间阶段被补齐完成
# ---------------------------------------------------------------------------
def test_phase_events_in_forward_order():
    """preflight→planner→parallel_retrieval→composer：阶段逐级推进。

    - phase_start(comprehension) 先发布；
    - 进入 preparation 时 comprehension 完成（completed=1）；
    - 进入 retrieval 时 preparation 完成（completed=2）；
    - 跳到 generation 时补发 retrieval/analysis/verification 三个完成事件。
    """
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

    seq = [(e["event"], e["phase"]) for e in phases]
    assert seq == [
        ("phase_start", "comprehension"),
        ("phase_progress", "comprehension"),
        ("phase_start", "preparation"),
        ("phase_progress", "preparation"),
        ("phase_start", "retrieval"),
        ("phase_progress", "retrieval"),
        ("phase_progress", "analysis"),
        ("phase_progress", "verification"),
        ("phase_start", "generation"),
    ]

    # completed 应为 1 基累计数，且 total 恒为阶段总数
    progresses = [e for e in phases if e["event"] == "phase_progress"]
    assert [e["completed"] for e in progresses] == [1, 2, 3, 4, 5]
    assert all(e["total"] == PHASE_TOTAL for e in phases)
    assert all(e.get("label") for e in phases), "阶段事件必须携带中文 label"


# ---------------------------------------------------------------------------
# 2. 跳阶段：跳过 preparation/retrieval（如无附件、HITL 恢复）也能补齐
# ---------------------------------------------------------------------------
def test_phase_events_skip_intermediate_phases():
    """HITL 恢复只跑 finalizer：应从 comprehension 直接跳到 generation。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", "legal_answer_finalizer"),
            _task_chunk("result", "legal_answer_finalizer"),
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    seq = [(e["event"], e["phase"]) for e in phases]
    # 直接跳到 generation：先补发全部 5 个阶段完成，再发布 generation 启动
    assert seq == [
        ("phase_progress", "comprehension"),
        ("phase_progress", "preparation"),
        ("phase_progress", "retrieval"),
        ("phase_progress", "analysis"),
        ("phase_progress", "verification"),
        ("phase_start", "generation"),
    ]
    progresses = [e for e in phases if e["event"] == "phase_progress"]
    assert [e["completed"] for e in progresses] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 3. 重跑不回退：critic 回退重试 legal_reasoner 不再发阶段事件
# ---------------------------------------------------------------------------
def test_phase_events_do_not_regress_on_rerun():
    """generation 阶段开始后重跑 analysis 节点（重试），不应产生新的阶段事件。"""
    graph = _FakeGraph(
        [
            _task_chunk("input", "preflight"),
            _task_chunk("input", "composer"),
            # 回退重试（critic→legal_reasoner→composer 再次执行）
            _task_chunk("input", "legal_reasoner"),
            _task_chunk("result", "legal_reasoner"),
            _task_chunk("input", "composer"),
            _task_chunk("result", "composer"),
        ]
    )
    events, _ = _run(graph)
    phases = _phases(events)

    # 前 2 个事件：comprehension 启动 → 跳转到 generation 前补发完成事件
    assert (phases[0]["event"], phases[0]["phase"]) == ("phase_start", "comprehension")
    # 重跑节点不应再触发任何阶段事件（进度单调）
    starts = [e for e in phases if e["event"] == "phase_start"]
    assert [e["phase"] for e in starts] == ["comprehension", "generation"]
    progresses = [e for e in phases if e["event"] == "phase_progress"]
    assert [e["completed"] for e in progresses] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 4. 向后兼容：node_start / node_end 事件仍正常发布
# ---------------------------------------------------------------------------
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
    node_events = [e for e in events if e["event"] in ("node_start", "node_end")]
    assert [e["node"] for e in node_events] == [
        "preflight",
        "preflight",
        "composer",
        "composer",
    ]
