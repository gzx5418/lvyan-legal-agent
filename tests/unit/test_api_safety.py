"""P0/P1 安全收口与一致性测试（复审第二轮）。

覆盖：
- P0-1：生产模式禁止 MemorySaver 静默回退（PersistenceUnavailable）
- P0-2：_check_interrupt_status 三态 fail-closed
- P0-3：/run 引用附件缺失 → 404（不静默跳过）
- P1-2：gc_runs 清理 cancelled；cancel 持久化失败 → unavailable；
       request_cancel / is_cancel_requested（用桩 store）
- P1-3：/state 消息存在但 checkpoint 缺失 → 200 + checkpoint_available=false
- 三-1：AUTH_MODE jwt/trusted_proxy；身份冲突拒绝
- 四：thread_id 格式校验；extra=forbid
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# P0-1：生产模式禁止静默回退
# ---------------------------------------------------------------------------
def test_build_graph_production_raises_on_pg_unavailable(monkeypatch):
    """RUNTIME_MODE=production 且 PG 不可达 → PersistenceUnavailable，不回退 MemorySaver。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
    )
    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


def test_build_graph_development_falls_back_to_memory(monkeypatch):
    """development 模式（默认）仍允许回退 MemorySaver。"""
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
    )
    from lvyan.graph.builder import build_graph_with_postgres

    graph = build_graph_with_postgres()
    # 应当回退到 MemorySaver
    assert "memory" in type(graph.checkpointer).__name__.lower()


def test_persistence_required_flag_forces_raise_even_in_dev(monkeypatch):
    """PERSISTENCE_REQUIRED=true 时即使 development 也抛异常。"""
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
    )
    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


# ---------------------------------------------------------------------------
# P0-2：_check_interrupt_status 三态 fail-closed
# ---------------------------------------------------------------------------
def test_check_interrupt_status_unavailable_on_exception():
    from lvyan.api.sse import _check_interrupt_status

    class _BoomGraph:
        def get_state(self, config):
            raise RuntimeError("checkpoint DB down")

    result = _check_interrupt_status(_BoomGraph(), {"configurable": {"thread_id": "t"}})
    assert result.status == "unavailable"
    assert result.payload is not None


def test_check_interrupt_status_none_when_no_next():
    from lvyan.api.sse import _check_interrupt_status

    class _Snapshot:
        next = ()
        tasks = None

    class _Graph:
        def get_state(self, config):
            return _Snapshot()

    result = _check_interrupt_status(_Graph(), {"configurable": {"thread_id": "t"}})
    assert result.status == "none"


# ---------------------------------------------------------------------------
# P0-3：/run 引用附件缺失 → 404
# ---------------------------------------------------------------------------
def test_run_with_missing_attachment_returns_404(monkeypatch, tmp_path):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api import server
    from lvyan.api.server import create_app

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    app = create_app(runner=None, memory=None, metadata_store=None)
    with TestClient(app) as client:
        resp = client.post(
            "/api/agent/run",
            json={
                "query": "押金问题",
                "attachments": ["nonexistent_file_id"],
            },
        )
    # 附件不存在 → P0-3：整个请求失败，不静默跳过
    assert resp.status_code == 404
    assert "不存在" in resp.text or "attachment" in resp.text.lower()


# ---------------------------------------------------------------------------
# P1-2：gc_runs 清理 cancelled；cancel 持久化失败 → unavailable
# ---------------------------------------------------------------------------
def test_gc_runs_cleans_cancelled_contexts():
    """cancelled 状态的 RunContext 也应被 GC（不再永久驻留）。"""
    import asyncio
    import time

    from lvyan.api.sse import RunContext, RunManager

    mgr = RunManager()
    ctx = RunContext("run-cancelled", "thread-x")
    ctx.status = "cancelled"
    ctx.completed_at = time.time() - 7200  # 2 小时前
    mgr._runs["run-cancelled"] = ctx

    removed = mgr.gc_runs(ttl_seconds=3600)
    assert removed == 1
    assert "run-cancelled" not in mgr._runs


def test_cancel_run_returns_unavailable_when_persistence_fails(monkeypatch):
    """本地取消成功但持久化失败 → 返回 unavailable（供 API 映射 503）。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    mgr = RunManager()
    ctx = RunContext("run-1", "thread-1", user_id="u1")
    ctx.status = "running"
    mgr._runs["run-1"] = ctx
    # 不注入真实 task；cancel_run 在 task 为 None 时仍走 persist 分支
    # 让 _update_metadata 返回 False（模拟持久化失败）
    monkeypatch.setattr(mgr, "_update_metadata", lambda *a, **kw: False)
    monkeypatch.setattr(mgr, "_append_message", lambda *a, **kw: False)

    loop = asyncio.new_event_loop()
    try:
        status, _msg = loop.run_until_complete(mgr.cancel_run("run-1", "u1"))
    finally:
        loop.close()
    assert status == "unavailable"


def test_request_cancel_and_is_cancel_requested_with_stub_store():
    """用内存桩 store 验证 RunManager 跨实例取消请求路径。"""
    import asyncio

    from lvyan.api.sse import RunManager

    class _StubStore:
        def __init__(self):
            self.cancel_requested = {}

        def request_cancel(self, run_id, user_id):
            # 模拟：run 存在且属于用户 → 记录并返回 True
            self.cancel_requested[(run_id, user_id)] = True
            return True

        def is_cancel_requested(self, run_id, user_id):
            return self.cancel_requested.get((run_id, user_id), False)

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    status, _msg = asyncio.new_event_loop().run_until_complete(
        mgr.cancel_run("run-remote", "u1")
    )
    assert status == "cancel_requested"
    assert stub.is_cancel_requested("run-remote", "u1") is True


# ---------------------------------------------------------------------------
# P1-3：/state 消息存在但 checkpoint 缺失 → 200 + checkpoint_available=false
# ---------------------------------------------------------------------------
def test_state_returns_200_with_messages_when_checkpoint_gone(monkeypatch):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api.server import create_app

    class _FakeMem:
        def list_threads(self):
            return []

        def load_strict(self, thread_id):
            return None  # checkpoint 不存在

    class _FakeStore:
        def get_thread(self, thread_id):
            return {
                "thread_id": thread_id,
                "user_id": "anonymous",
                "title": "T",
                "complexity": "light",
                "has_output": True,
                "created_at": 0,
                "updated_at": 0,
            }

        def list_messages(self, thread_id, user_id):
            return [
                {
                    "run_id": "r1",
                    "role": "user",
                    "content": "你好",
                    "attachments": [],
                    "created_at": 0,
                }
            ]

    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    app = create_app(memory=_FakeMem(), metadata_store=_FakeStore())
    with TestClient(app) as client:
        resp = client.get("/api/agent/state/thread-x")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checkpoint_available"] is False
    assert data["recoverable"] is True
    assert len(data["messages"]) == 1


# ---------------------------------------------------------------------------
# 三-1：AUTH_MODE + 身份冲突
# ---------------------------------------------------------------------------
class _Req:
    pass


def test_auth_mode_jwt_rejects_x_user_id_only(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id="alice", authorization=None)
    assert exc.value.status_code == 401


def test_auth_mode_trusted_proxy_rejects_bearer_only(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(
            _Req(), x_user_id=None, authorization="Bearer some.jwt.token"
        )
    assert exc.value.status_code == 401


def test_auth_mode_rejects_conflict_identity(monkeypatch):
    """同时携带 X-User-ID 与 Bearer → 401 identity_conflict。"""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("AUTH_MODE", raising=False)  # auto
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(
            _Req(),
            x_user_id="alice",
            authorization="Bearer some.jwt.token",
        )
    assert exc.value.status_code == 401
    assert exc.value.detail == "identity_conflict"


def test_auth_mode_trusted_proxy_accepts_x_user_id(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    from lvyan.api.auth import get_current_user_id

    uid = get_current_user_id(_Req(), x_user_id="alice", authorization=None)
    assert uid == "alice"


# ---------------------------------------------------------------------------
# 四：输入模型约束
# ---------------------------------------------------------------------------
def test_agent_run_request_rejects_unknown_field():
    from pydantic import ValidationError

    from lvyan.api.models import AgentRunRequest

    # 合法构造不应抛异常
    AgentRunRequest(query="hi", thread_id=None, complexity="light")
    # extra field → 应被拒绝
    with pytest.raises(ValidationError):
        AgentRunRequest(query="hi", bogus_field="x")  # type: ignore[call-arg]


def test_agent_run_request_thread_id_pattern():
    from pydantic import ValidationError

    from lvyan.api.models import AgentRunRequest

    # 合法
    AgentRunRequest(query="hi", thread_id="thread-123_ABC")
    # 非法：含路径分隔符
    with pytest.raises(ValidationError):
        AgentRunRequest(query="hi", thread_id="../etc/passwd")
    # 非法：含空格
    with pytest.raises(ValidationError):
        AgentRunRequest(query="hi", thread_id="has space")


def test_agent_run_request_thread_id_too_long():
    from pydantic import ValidationError

    from lvyan.api.models import AgentRunRequest

    with pytest.raises(ValidationError):
        AgentRunRequest(query="hi", thread_id="a" * 200)


def test_hitl_request_edited_output_length_limit():
    from pydantic import ValidationError

    from lvyan.api.models import HITLRequest

    HITLRequest(action="edit", edited_output="x" * 100_000)  # 刚好上限 OK
    with pytest.raises(ValidationError):
        HITLRequest(action="edit", edited_output="x" * 100_001)
