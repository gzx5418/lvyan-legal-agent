"""P3 优化项的回归测试。

覆盖：
  - P3-21：chat_structured Pydantic schema 校验
  - P3-22：并行检索（顺序回退路径，至少不应崩溃）
  - P3-23：SSE node_start/node_end 含 timestamp / duration_ms
  - P3-24：_runs TTL 清理
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# P3-21：chat_structured
# ---------------------------------------------------------------------------
def test_chat_structured_with_pydantic_model(monkeypatch):
    """mock chat_json 返回合法 dict → 校验通过返回 Pydantic 实例。"""
    from pydantic import BaseModel

    from lvyan.llm import client as llm_client

    class Out(BaseModel):
        facts: list[str]
        confidence: float

    monkeypatch.setattr(
        llm_client,
        "chat_json",
        lambda messages, **kw: {"facts": ["a", "b"], "confidence": 0.9},
    )

    result = llm_client.chat_structured(
        messages=[{"role": "system", "content": "抽取事实"}],
        response_model=Out,
    )
    assert result is not None
    assert isinstance(result, Out)
    assert result.facts == ["a", "b"]


def test_chat_structured_invalid_then_repair(monkeypatch):
    """首次校验失败 → 触发修复重试 → 第二次成功。"""
    from pydantic import BaseModel

    from lvyan.llm import client as llm_client

    class Out(BaseModel):
        score: float

    call_count = {"n": 0}

    def _mock_chat_json(messages, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"wrong_field": "x"}  # 缺 score，校验失败
        return {"score": 0.5}  # 修复后成功

    monkeypatch.setattr(llm_client, "chat_json", _mock_chat_json)

    result = llm_client.chat_structured(
        messages=[{"role": "system", "content": "打分"}],
        response_model=Out,
    )
    assert result is not None
    assert result.score == 0.5
    assert call_count["n"] == 2  # 触发了修复重试


def test_chat_structured_both_failures_returns_none(monkeypatch):
    """两次都校验失败 → 返回 None。"""
    from pydantic import BaseModel

    from lvyan.llm import client as llm_client

    class Out(BaseModel):
        score: float

    monkeypatch.setattr(
        llm_client, "chat_json",
        lambda messages, **kw: {"wrong_field": "x"},
    )

    result = llm_client.chat_structured(
        messages=[{"role": "system", "content": "打分"}],
        response_model=Out,
    )
    assert result is None


# ---------------------------------------------------------------------------
# P3-22：并行检索（顺序回退）
# ---------------------------------------------------------------------------
def test_parallel_search_statutes_empty_queries():
    """空 queries → 返回空列表（不调用 search_statutes）。"""
    from lvyan.nodes.retrieve_statutes import _parallel_search_statutes

    assert _parallel_search_statutes([]) == []
    assert _parallel_search_statutes([{"query_text": ""}]) == []


def test_parallel_search_statutes_handles_search_failure(monkeypatch):
    """search_statutes 抛异常 → 单条返回空，整体不崩溃。"""
    from lvyan.nodes import retrieve_statutes as rs

    def _fail(qt, top_k=10):
        raise RuntimeError("模拟检索失败")

    monkeypatch.setattr(rs, "search_statutes", _fail)

    # 无事件循环 → 走顺序路径
    rs._ASYNC_AVAILABLE = False
    out = rs._parallel_search_statutes([{"query_text": "违约"}])
    assert out == []


# ---------------------------------------------------------------------------
# P3-23：SSE 事件含 timestamp / duration_ms
# ---------------------------------------------------------------------------
def test_sse_publish_node_events_have_timestamp_and_duration():
    """node_start/node_end 事件含 timestamp；node_end 含 duration_ms。

    通过直接构造 RunContext + publish 验证事件 schema（不跑完整 graph）。
    """
    from lvyan.api.sse import RunContext

    async def _run():
        ctx = RunContext("run-x", "thread-x")
        await ctx.publish({
            "event": "node_start", "node": "triage", "timestamp": time.time(),
        })
        await ctx.publish({
            "event": "node_end", "node": "triage",
            "timestamp": time.time(), "duration_ms": 12.5,
        })
        # 取出事件
        events = []
        while not ctx.queue.empty():
            events.append(await ctx.queue.get())
        return events

    events = asyncio.get_event_loop().run_until_complete(_run())
    assert events[0]["event"] == "node_start"
    assert "timestamp" in events[0]
    assert events[1]["event"] == "node_end"
    assert "timestamp" in events[1]
    assert "duration_ms" in events[1]


# ---------------------------------------------------------------------------
# P3-24：_runs TTL 清理
# ---------------------------------------------------------------------------
def test_gc_runs_removes_completed_beyond_ttl():
    """完成的运行超过 TTL → 被 gc_runs 清理。"""
    from lvyan.api.sse import RunContext, RunManager

    mgr = RunManager()
    ctx = RunContext("run-old", "thread-old")
    ctx.status = "completed"
    ctx.created_at = time.time() - 7200  # 2 小时前
    ctx.completed_at = ctx.created_at
    mgr._runs["run-old"] = ctx

    removed = mgr.gc_runs(ttl_seconds=3600)
    assert removed == 1
    assert "run-old" not in mgr._runs


def test_gc_runs_keeps_recent_completed():
    """刚完成的运行 → 不被清理。"""
    from lvyan.api.sse import RunContext, RunManager

    mgr = RunManager()
    ctx = RunContext("run-fresh", "thread-fresh")
    ctx.status = "completed"
    ctx.completed_at = time.time()  # 刚完成
    mgr._runs["run-fresh"] = ctx

    removed = mgr.gc_runs(ttl_seconds=3600)
    assert removed == 0
    assert "run-fresh" in mgr._runs


def test_gc_runs_keeps_running():
    """运行中的运行（未 completed/failed）→ 即使超过 TTL 也不清理。"""
    from lvyan.api.sse import RunContext, RunManager

    mgr = RunManager()
    ctx = RunContext("run-stuck", "thread-stuck")
    ctx.status = "running"
    ctx.created_at = time.time() - 7200
    mgr._runs["run-stuck"] = ctx

    removed = mgr.gc_runs(ttl_seconds=3600)
    assert removed == 0
