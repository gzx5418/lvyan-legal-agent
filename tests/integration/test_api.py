"""FastAPI + SSE API 端点集成测试（TestClient + mock runner，不依赖真实 LLM/DB）。

PR1 适配
--------
- ``CaseMemory`` 替代 ``ShortTermMemory``：测试用 ``FakeCaseMemory`` 桩注入。
- HITL 测试标记 skip：新架构下 HITL 由 LangGraph ``interrupt()`` 触发，
  mock runner 无法模拟，需在 PR2 接入真实 LLM 后用端到端测试覆盖。
"""

from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvyan.api.server import create_app
from lvyan.schemas import CaseState


# ---------------------------------------------------------------------------
# FakeCaseMemory：测试用桩，实现 CaseMemory 协议（load/delete/list_threads）
# ---------------------------------------------------------------------------
class FakeCaseMemory:
    """内存字典实现的 CaseMemory 桩，用于 API 集成测试隔离。"""

    def __init__(self) -> None:
        self._store: dict[str, CaseState] = {}
        self._index: dict[str, dict[str, Any]] = {}

    def register(self, thread_id: str, title: str = "", complexity: str = "light") -> None:
        self._index[thread_id] = {
            "title": title or thread_id,
            "complexity": complexity,
            "created_at": time.time(),
            "has_output": False,
        }

    def mark_output(self, thread_id: str) -> None:
        if thread_id in self._index:
            self._index[thread_id]["has_output"] = True

    def save(self, thread_id: str, state: CaseState) -> None:
        """测试专用：直接注入状态（生产 CaseMemory 由 checkpointer 自动保存）。"""
        self._store[thread_id] = state
        if thread_id not in self._index:
            self.register(thread_id, title=(state.user_goal or "")[:40], complexity=state.complexity or "light")
        if state.final_output:
            self.mark_output(thread_id)

    def load(self, thread_id: str) -> CaseState | None:
        return self._store.get(thread_id)

    def load_strict(self, thread_id: str) -> CaseState | None:
        return self.load(thread_id)

    def delete(self, thread_id: str) -> bool:
        existed = thread_id in self._store
        self._store.pop(thread_id, None)
        self._index.pop(thread_id, None)
        return existed

    def delete_strict(self, thread_id: str) -> bool:
        return self.delete(thread_id)

    def list_threads(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._index.items())


# ---------------------------------------------------------------------------
# mock runner：模拟节点事件流
# ---------------------------------------------------------------------------
async def mock_runner(query, thread_id, complexity, ctx):
    await ctx.publish({"event": "node_start", "node": "triage"})
    await ctx.publish({"event": "node_end", "node": "triage", "duration_ms": 10})
    await ctx.publish({"event": "node_start", "node": "composer"})
    await ctx.publish({"event": "node_end", "node": "composer", "duration_ms": 20})
    return f"答案:{query}"


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
def test_state_returns_summary():
    mem = FakeCaseMemory()
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


def test_state_unknown_thread_returns_404():
    mem = FakeCaseMemory()
    app = create_app(runner=mock_runner, memory=mem)
    with TestClient(app) as client:
        resp = client.get("/api/agent/state/unknown-thread")
    assert resp.status_code == 404


def test_list_threads_returns_summaries_from_index():
    """list_threads 从索引读取元数据，不依赖 load。"""
    mem = FakeCaseMemory()
    mem.register("t1", title="押金纠纷咨询", complexity="light")
    mem.register("t2", title="劳动仲裁", complexity="deep")
    mem.mark_output("t1")

    app = create_app(runner=mock_runner, memory=mem)
    with TestClient(app) as client:
        resp = client.get("/api/agent/threads")
    assert resp.status_code == 200
    threads = resp.json()["threads"]
    assert len(threads) == 2
    by_id = {t["thread_id"]: t for t in threads}
    assert by_id["t1"]["title"] == "押金纠纷咨询"
    assert by_id["t1"]["has_output"] is True
    assert by_id["t2"]["complexity"] == "deep"
    assert by_id["t2"]["has_output"] is False


def test_delete_thread_removes_from_index():
    mem = FakeCaseMemory()
    cs = CaseState(
        run_id="run-x",
        thread_id="thread-del",
        current_date=date(2026, 7, 24),
        user_goal="待删除",
    )
    mem.save("thread-del", cs)
    app = create_app(runner=mock_runner, memory=mem)
    with TestClient(app) as client:
        resp = client.delete("/api/agent/state/thread-del")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # 再次列出不应包含
    with TestClient(app) as client:
        resp = client.get("/api/agent/threads")
    threads = resp.json()["threads"]
    assert all(t["thread_id"] != "thread-del" for t in threads)


# ---------------------------------------------------------------------------
# 5. POST /api/agent/hitl/{run_id} 处理 approve / reject / edit
#
# PR1 变更：HITL 改用 LangGraph interrupt() + Command(resume=...) 机制。
# 旧的 hitl_runner（基于 ctx.await_hitl()）已不适用，因 RunContext 不再
# 提供 await_hitl 方法。HITL 端到端测试需在 PR2 接入真实 LLM 后通过
# 真实 graph interrupt 场景覆盖。
# ---------------------------------------------------------------------------
_HITL_SKIP_REASON = (
    "PR1: HITL 改用 LangGraph interrupt + Command(resume=...)，"
    "mock runner 无法模拟 graph interrupt，需 PR2 端到端测试覆盖"
)


@pytest.mark.skip(reason=_HITL_SKIP_REASON)
def test_hitl_approve():
    pass


@pytest.mark.skip(reason=_HITL_SKIP_REASON)
def test_hitl_edit():
    pass


@pytest.mark.skip(reason=_HITL_SKIP_REASON)
def test_hitl_reject():
    pass


def test_hitl_unknown_run_returns_404():
    """run_id 不存在时 HITL 端点返回 404（无需真实 interrupt）。"""
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/hitl/run-nope", json={"action": "approve"}
        )
    assert resp.status_code == 404


def test_hitl_invalid_action_returns_422():
    """非法 action 触发 Pydantic 校验 422（无需真实 interrupt）。"""
    app = create_app(runner=mock_runner)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/hitl/run-fake", json={"action": "maybe"}
        )
    assert resp.status_code == 422
