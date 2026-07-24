"""FastAPI + SSE API 端点集成测试（TestClient + mock runner，不依赖真实 LLM/DB）。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from lvyan.api.models import HITLRequest
from lvyan.api.server import create_app
from lvyan.memory.checkpoints import ShortTermMemory
from lvyan.schemas import CaseState


# ---------------------------------------------------------------------------
# mock runner：模拟节点事件流
# ---------------------------------------------------------------------------
async def mock_runner(query, thread_id, complexity, ctx):
    await ctx.publish({"event": "node_start", "node": "triage"})
    await ctx.publish({"event": "node_end", "node": "triage", "duration_ms": 10})
    await ctx.publish({"event": "node_start", "node": "composer"})
    await ctx.publish({"event": "node_end", "node": "composer", "duration_ms": 20})
    return f"答案:{query}"


# ---------------------------------------------------------------------------
# HITL mock runner：等待人工审批后返回
# ---------------------------------------------------------------------------
async def hitl_runner(query, thread_id, complexity, ctx):
    await ctx.publish({"event": "node_start", "node": "output_guardrail"})
    resp = await ctx.await_hitl()
    await ctx.publish({"event": "node_end", "node": "output_guardrail"})
    if resp.action == "approve":
        return "approved-output"
    if resp.action == "edit":
        return resp.edited_output or "edited"
    return "rejected-output"


def _parse_sse(body: str) -> list[dict]:
    """解析 SSE 流正文为事件字典列表。"""
    events = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        payload = chunk[len("data:"):].strip()
        if payload:
            events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# 1. /api/health 返回 200 与组件状态
# ---------------------------------------------------------------------------
def test_health_returns_200():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "database" in data
    assert "retrieval" in data
    assert "model_gateway" in data
    assert data["status"] in ("ok", "degraded")


# ---------------------------------------------------------------------------
# 2. POST /api/agent/run 启动 Agent 运行
# ---------------------------------------------------------------------------
def test_run_starts_agent():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.post("/api/agent/run", json={"query": "押金不退怎么办"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    assert data["run_id"]
    assert data["thread_id"]


def test_run_with_thread_id_uses_provided():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/run",
            json={"query": "继续", "thread_id": "thread-fixed-001"},
        )
    assert resp.status_code == 200
    assert resp.json()["thread_id"] == "thread-fixed-001"


def test_run_with_invalid_complexity_returns_422():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/run",
            json={"query": "x", "complexity": "invalid"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. GET /api/agent/stream/{run_id} 流式返回节点事件
# ---------------------------------------------------------------------------
def test_stream_returns_node_events_and_final_output():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        run = client.post("/api/agent/run", json={"query": "押金"}).json()
        resp = client.get(f"/api/agent/stream/{run['run_id']}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    names = [e.get("node") for e in events if e.get("event") == "node_start"]
    assert "triage" in names
    assert "composer" in names
    final = [e for e in events if e.get("event") == "final_output"]
    assert len(final) == 1
    assert final[0]["output"] == "答案:押金"


def test_stream_unknown_run_returns_404():
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.get("/api/agent/stream/run-does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. GET /api/agent/state/{thread_id} 返回 CaseState 摘要
# ---------------------------------------------------------------------------
def test_state_returns_summary(tmp_path):
    mem = ShortTermMemory(base_dir=tmp_path)
    cs = CaseState(
        run_id="run-001",
        thread_id="thread-001",
        current_date=date(2026, 7, 24),
        user_goal="押金纠纷",
        jurisdiction="中国大陆",
        case_type="合同纠纷",
        complexity="light",
        final_output="最终答复",
    )
    mem.save("thread-001", cs)

    app = create_app(runner=mock_runner, memory=mem)
    with TestClient(app) as client:
        resp = client.get("/api/agent/state/thread-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == "thread-001"
    assert data["jurisdiction"] == "中国大陆"
    assert data["case_type"] == "合同纠纷"
    assert data["complexity"] == "light"
    assert data["final_output"] == "最终答复"
    # 摘要不应包含敏感原始字段
    assert "uploaded_documents" not in data


def test_state_unknown_thread_returns_404(tmp_path):
    mem = ShortTermMemory(base_dir=tmp_path)
    app = create_app(runner=mock_runner, memory=mem)
    with TestClient(app) as client:
        resp = client.get("/api/agent/state/unknown-thread")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. POST /api/agent/hitl/{run_id} 处理 approve / reject / edit
# ---------------------------------------------------------------------------
def _start_hitl_run(client):
    run = client.post("/api/agent/run", json={"query": "发送律师函"}).json()
    return run["run_id"]


def test_hitl_approve():
    app = create_app(runner=hitl_runner)
    with TestClient(app) as client:
        run_id = _start_hitl_run(client)
        resp = client.post(
            f"/api/agent/hitl/{run_id}", json={"action": "approve"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"
        # 流应输出 approved-output
        stream = client.get(f"/api/agent/stream/{run_id}")
    events = _parse_sse(stream.text)
    final = [e for e in events if e.get("event") == "final_output"]
    assert final and final[0]["output"] == "approved-output"


def test_hitl_edit():
    app = create_app(runner=hitl_runner)
    with TestClient(app) as client:
        run_id = _start_hitl_run(client)
        resp = client.post(
            f"/api/agent/hitl/{run_id}",
            json={"action": "edit", "edited_output": "用户改写后的文书"},
        )
        assert resp.status_code == 200
        stream = client.get(f"/api/agent/stream/{run_id}")
    events = _parse_sse(stream.text)
    final = [e for e in events if e.get("event") == "final_output"]
    assert final and final[0]["output"] == "用户改写后的文书"


def test_hitl_reject():
    app = create_app(runner=hitl_runner)
    with TestClient(app) as client:
        run_id = _start_hitl_run(client)
        resp = client.post(
            f"/api/agent/hitl/{run_id}", json={"action": "reject"}
        )
        assert resp.status_code == 200
        stream = client.get(f"/api/agent/stream/{run_id}")
    events = _parse_sse(stream.text)
    final = [e for e in events if e.get("event") == "final_output"]
    assert final and final[0]["output"] == "rejected-output"


def test_hitl_unknown_run_returns_404():
    app = create_app(runner=hitl_runner)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/hitl/run-nope", json={"action": "approve"}
        )
    assert resp.status_code == 404


def test_hitl_invalid_action_returns_422():
    app = create_app(runner=hitl_runner)
    with TestClient(app) as client:
        run_id = _start_hitl_run(client)
        resp = client.post(
            f"/api/agent/hitl/{run_id}", json={"action": "maybe"}
        )
    assert resp.status_code == 422
