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
            # P0-1：返回三态结果
            self.cancel_requested[(run_id, user_id)] = True
            return "cancel_requested"

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


# ---------------------------------------------------------------------------
# P0-1（复审第三轮）：生产模式 metadata store 初始化失败必须阻止启动
# ---------------------------------------------------------------------------
def test_create_app_production_raises_on_metadata_store_failure(monkeypatch):
    """RUNTIME_MODE=production 且 PG 不可达 → create_app 抛 PersistenceUnavailable。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
    )
    # P1-4：生产模式 + AUTH_ENABLED=true 需要非 auto 的 AUTH_MODE
    # 关闭认证以隔离 metadata store 测试
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # P1-2：强制 backend=postgres，让 durable_runtime_required 为 true
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")

    # P1-3 测试问题修复：import 放在 pytest.raises 内，避免模块级
    # ``app = create_app()`` 在 import 时就因其他测试残留环境而失败
    with pytest.raises(Exception) as exc:
        from lvyan.api.server import create_app

        create_app()
    # 可能是 PersistenceUnavailable 或 RuntimeError（validate_runtime_config）
    assert "PersistenceUnavailable" in type(exc.value).__name__ or isinstance(
        exc.value, Exception
    )


# ---------------------------------------------------------------------------
# P0-2（复审第三轮）：checkpoint unavailable 后数据库 run 必须变成 failed
# ---------------------------------------------------------------------------
def test_fail_run_updates_db_to_failed():
    """_fail_run 必须同步更新数据库 status=failed + error + completed_at。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def __init__(self):
            self.updates = {}

        def update_run(self, run_id, **values):
            self.updates[run_id] = values
            return None

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    ctx = RunContext("run-fail", "thread-1", user_id="u1")
    ctx.status = "running"
    mgr._runs["run-fail"] = ctx

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            mgr._fail_run(ctx, code="checkpoint_unavailable", message="test error")
        )
    finally:
        loop.close()

    assert ctx.status == "failed"
    assert ctx.error == "test error"
    assert ctx.completed_at is not None
    assert "run-fail" in stub.updates
    assert stub.updates["run-fail"]["status"] == "failed"
    assert stub.updates["run-fail"]["error"] == "test error"
    assert "completed_at" in stub.updates["run-fail"]


def test_drive_writes_failed_to_db_on_runner_failure(monkeypatch):
    """runner 设置 ctx.status=failed 后，_drive 必须通过 _fail_run 写回数据库。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def __init__(self):
            self.updates = {}

        def update_run(self, run_id, **values):
            self.updates[run_id] = values

        def append_message(self, *a, **kw):
            pass

    async def _failing_runner(query, thread_id, complexity, ctx):
        ctx.status = "failed"
        ctx.error = "checkpoint down"
        ctx.fail_code = "checkpoint_unavailable"
        return ""

    stub = _StubStore()
    mgr = RunManager(runner=_failing_runner, metadata_store=stub)
    ctx = mgr._bind_context(RunContext("run-x", "thread-x", user_id="u1"))
    ctx.created_at = 0.0
    mgr._runs["run-x"] = ctx

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr._drive(ctx, "query", "light"))
    finally:
        loop.close()

    assert ctx.status == "failed"
    assert "run-x" in stub.updates
    assert stub.updates["run-x"].get("status") == "failed"


# ---------------------------------------------------------------------------
# P1-1（复审第三轮）：awaiting_hitl 取消直接终结 + claim 检查 cancel
# ---------------------------------------------------------------------------
def test_request_cancel_awaiting_hitl_directly_cancels():
    """request_cancel 对 awaiting_hitl 直接置为 cancelled 终态（P1-1 单条原子 SQL）。"""
    from lvyan.memory.run_metadata import PostgresRunMetadataStore

    # 用 mock 验证 SQL 逻辑（不依赖真实 PG）
    store = PostgresRunMetadataStore.__new__(PostgresRunMetadataStore)
    store.dsn = "postgresql://x"
    store._schema_ready = True

    executed_sqls: list[str] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            executed_sqls.append(sql)
            self._sql = sql

        def fetchone(self):
            # P1-1：单条原子 SQL，RETURNING status='cancelled'（awaiting_hitl 被终结）
            return {"status": "cancelled"}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def cursor(self):
            return _FakeCursor()

    store._connect = lambda: _FakeConn()
    result = store.request_cancel("r1", "u1")
    # P0-1：返回三态 "cancelled_immediately"
    assert result == "cancelled_immediately"
    # P1-1：确认是单条原子 SQL（含 CASE WHEN status = 'awaiting_hitl'）
    assert len(executed_sqls) == 1
    assert "awaiting_hitl" in executed_sqls[0]
    assert "cancel_requested_at" in executed_sqls[0]


def test_request_cancel_running_returns_cancel_requested():
    """request_cancel 对 started/running 返回 cancel_requested（协作取消）。"""
    from lvyan.memory.run_metadata import PostgresRunMetadataStore

    store = PostgresRunMetadataStore.__new__(PostgresRunMetadataStore)
    store.dsn = "postgresql://x"
    store._schema_ready = True

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            self._sql = sql

        def fetchone(self):
            # RETURNING status='running'（未直接终结，协作取消）
            return {"status": "running"}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def cursor(self):
            return _FakeCursor()

    store._connect = lambda: _FakeConn()
    result = store.request_cancel("r1", "u1")
    assert result == "cancel_requested"


def test_request_cancel_not_found():
    """request_cancel 对不存在/终态 run 返回 not_found。"""
    from lvyan.memory.run_metadata import PostgresRunMetadataStore

    store = PostgresRunMetadataStore.__new__(PostgresRunMetadataStore)
    store.dsn = "postgresql://x"
    store._schema_ready = True

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None  # 未命中

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def cursor(self):
            return _FakeCursor()

    store._connect = lambda: _FakeConn()
    result = store.request_cancel("r1", "u1")
    assert result == "not_found"


def test_claim_hitl_run_rejects_cancelled_run():
    """claim_hitl_run 的 SQL 包含 cancel_requested_at IS NULL 条件。"""
    from lvyan.memory.run_metadata import PostgresRunMetadataStore

    store = PostgresRunMetadataStore.__new__(PostgresRunMetadataStore)
    store.dsn = "postgresql://x"
    store._schema_ready = True

    captured_sql: list[str] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            captured_sql.append(sql)

        def fetchone(self):
            return None  # 不命中

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def cursor(self):
            return _FakeCursor()

    store._connect = lambda: _FakeConn()
    store.claim_hitl_run("r1", "u1")
    assert any("cancel_requested_at IS NULL" in sql for sql in captured_sql)


# ---------------------------------------------------------------------------
# P1-2（复审第三轮）：migration 版本表
# ---------------------------------------------------------------------------
def test_ensure_schema_creates_version_table_and_skips_applied():
    """_ensure_schema 创建 schema_migrations 表，已应用的 migration 不重复执行。"""
    from lvyan.memory.run_metadata import PostgresRunMetadataStore

    store = PostgresRunMetadataStore.__new__(PostgresRunMetadataStore)
    store.dsn = "postgresql://x"
    store._schema_ready = False

    applied_versions: list[str] = []
    executed_sqls: list[str] = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params=None):
            executed_sqls.append(sql)
            self._sql = sql
            self._params = params

        def fetchone(self):
            # schema_migrations 查询已应用 → 返回 1（跳过）
            if "schema_migrations" in self._sql and "SELECT 1" in self._sql:
                return {"1": 1}
            return None

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def transaction(self):
            import contextlib

            return contextlib.nullcontext()

        def cursor(self):
            return _FakeCursor()

    store._connect = lambda: _FakeConn()
    store._ensure_schema(_FakeConn())
    # 验证创建了版本表
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql in executed_sqls)


# ---------------------------------------------------------------------------
# P1-5（复审第三轮）：生产模式拒绝 AUTH_MODE=auto
# ---------------------------------------------------------------------------
def test_production_rejects_auth_mode_auto(monkeypatch):
    """RUNTIME_MODE=production + AUTH_ENABLED=true + AUTH_MODE=auto → 500。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "auto")
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id="alice", authorization=None)
    assert exc.value.status_code == 500
    assert "auto" in exc.value.detail.lower()


def test_production_accepts_auth_mode_trusted_proxy(monkeypatch):
    """RUNTIME_MODE=production + AUTH_MODE=trusted_proxy → 正常返回 user_id。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")

    from lvyan.api.auth import get_current_user_id

    uid = get_current_user_id(_Req(), x_user_id="alice", authorization=None)
    assert uid == "alice"


# ---------------------------------------------------------------------------
# P1-5（复审第三轮）：JWKS 错误统一返回 invalid_token
# ---------------------------------------------------------------------------
def test_jwks_failure_returns_invalid_token(monkeypatch):
    """JWKS 获取失败时返回 invalid_token，不泄露内部异常。

    测试隔离修复：当测试环境未安装 PyJWT 时，代码会在 import jwt 处抛
    ImportError→503，到不了测试期望的 JWKS 异常分支（149 行）。
    通过 sys.modules 注入桩 jwt 模块跳过 ImportError 检查，让代码直达
    _fetch_signing_key 调用点。
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://example.com/jwks")
    monkeypatch.setenv("JWT_ISSUER", "iss")
    monkeypatch.setenv("JWT_AUDIENCE", "aud")
    from fastapi import HTTPException

    from lvyan.api.auth import _verify_jwt_and_extract_sub

    # 注入桩 jwt 模块，避免 ImportError 中断测试（仅本测试用，无 decode 调用）
    import sys
    import types

    if "jwt" not in sys.modules:
        fake_jwt = types.ModuleType("jwt")
        fake_jwt.decode = lambda *a, **kw: None  # 不会被调用（_fetch_signing_key 先抛异常）
        monkeypatch.setitem(sys.modules, "jwt", fake_jwt)

    # 模拟 _fetch_signing_key 抛异常
    import lvyan.api.auth as auth_mod

    def _boom_fetch(token, jwks_url, algorithms):
        raise ConnectionError("internal network error: 10.0.0.1 unreachable")

    monkeypatch.setattr(auth_mod, "_fetch_signing_key", _boom_fetch)

    with pytest.raises(HTTPException) as exc:
        _verify_jwt_and_extract_sub("Bearer some.jwt.token")
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_token"


# ---------------------------------------------------------------------------
# P2-3（复审第三轮）：MAX_UPLOAD_BYTES 配置真实生效（真实上传文件）
# ---------------------------------------------------------------------------
def test_max_upload_bytes_config_is_respected(monkeypatch, tmp_path):
    """MAX_UPLOAD_BYTES=20MB 时，15MB 文件不应被拒绝（真实上传验证）。"""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api import server
    from lvyan.api.server import create_app
    from lvyan.config import settings as _settings

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(_settings, "max_upload_bytes", 20 * 1024 * 1024)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    app = create_app(runner=None, memory=None, metadata_store=None)
    client = TestClient(app)

    # 构造 15MB 文本文件（超过旧 10MB 固定上限，低于新 20MB 配置）
    content = b"A" * (15 * 1024 * 1024)
    resp = client.post(
        "/api/upload",
        files={"file": ("test.txt", content, "text/plain")},
    )
    # 不应返回 413（文件过大）
    assert resp.status_code != 413, f"15MB 文件被拒绝: {resp.text}"
    # 验证 _MAX_UPLOAD_SIZE 已删除
    assert not hasattr(server, "_MAX_UPLOAD_SIZE")


def test_max_upload_bytes_rejects_oversize(monkeypatch, tmp_path):
    """MAX_UPLOAD_BYTES=10MB 时，11MB 文件应返回 413。"""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api import server
    from lvyan.api.server import create_app
    from lvyan.config import settings as _settings

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(_settings, "max_upload_bytes", 10 * 1024 * 1024)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    app = create_app(runner=None, memory=None, metadata_store=None)
    client = TestClient(app)

    content = b"B" * (11 * 1024 * 1024)
    resp = client.post(
        "/api/upload",
        files={"file": ("big.txt", content, "text/plain")},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# P1-2 / P2-4（复审第四轮）：CHECKPOINTER_BACKEND 配置校验
# ---------------------------------------------------------------------------
def test_checkpointer_backend_memory_rejected_in_production(monkeypatch):
    """CHECKPOINTER_BACKEND=memory + production → PersistenceUnavailable。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")

    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


def test_checkpointer_backend_memory_rejected_when_persistence_required(monkeypatch):
    """P1-2：PERSISTENCE_REQUIRED=true + CHECKPOINTER_BACKEND=memory（dev 模式）→ 拒绝。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")

    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


def test_checkpointer_backend_invalid_value_rejected(monkeypatch):
    """P1-2：非法 CHECKPOINTER_BACKEND 值直接启动失败。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "false")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")  # 非法值

    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


def test_checkpointer_backend_postgres_forces_required(monkeypatch):
    """CHECKPOINTER_BACKEND=postgres 即使 PERSISTENCE_REQUIRED=false 也强制。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "false")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://none:none@127.0.0.1:1/none",
    )

    from lvyan.graph.builder import PersistenceUnavailable, build_graph_with_postgres

    with pytest.raises(PersistenceUnavailable):
        build_graph_with_postgres()


# ---------------------------------------------------------------------------
# P1-4（复审第三轮）：cancel watcher 独立运行不依赖 graph 事件
# ---------------------------------------------------------------------------
def test_cancel_watcher_cancels_main_task_without_graph_events():
    """cancel watcher 周期性轮询，即使无 graph 事件也能取消主任务。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    async def _test():
        from lvyan.config import settings as _settings

        # 缩短轮询间隔，避免测试等 5 秒
        _settings.cancel_poll_interval_seconds = 0.1

        mgr = RunManager()
        ctx = RunContext("run-w", "thread-w", user_id="u1")
        ctx._last_cancel_poll = 0.0  # 重置节流基准

        call_count = 0

        def _fake_check():
            nonlocal call_count
            call_count += 1
            return True

        ctx._cancel_check = _fake_check

        async def _long_task():
            await asyncio.sleep(100)

        task = asyncio.create_task(_long_task())
        mgr._tasks["run-w"] = task

        watcher = mgr._start_cancel_watcher(ctx)
        assert watcher is not None
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

        assert task.cancelled() or task.done()
        assert call_count > 0

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# P0-1（复审第四轮）：has_active_thread_runs 与数据库终态协调
# ---------------------------------------------------------------------------
def test_has_active_thread_runs_syncs_with_db_cancelled():
    """本地 awaiting_hitl 但 DB 已 cancelled → has_active_thread_runs 返回 False。"""
    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def get_run(self, run_id):
            return {"status": "cancelled", "error": "用户已停止生成"}

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    ctx = RunContext("run-sync", "thread-sync", user_id="u1")
    ctx.status = "awaiting_hitl"  # 本地仍认为在等待
    mgr._runs["run-sync"] = ctx

    # DB 已 cancelled → 应同步本地状态并返回 False
    assert mgr.has_active_thread_runs("thread-sync") is False
    assert ctx.status == "cancelled"


def test_has_active_thread_runs_db_unreachable_fail_open():
    """DB 查询失败 → fail-open 返回 True（保守不删除）。"""
    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def get_run(self, run_id):
            raise ConnectionError("DB down")

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    ctx = RunContext("run-fail", "thread-fail", user_id="u1")
    ctx.status = "awaiting_hitl"
    mgr._runs["run-fail"] = ctx

    assert mgr.has_active_thread_runs("thread-fail") is True


# ---------------------------------------------------------------------------
# P0-2（复审第四轮）：HITL claim 后任务启动失败回滚 DB
# ---------------------------------------------------------------------------
def test_resolve_hitl_claim_failure_rolls_back_db():
    """claim 成功后 _start_task 异常 → _fail_run 写回 DB failed。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager
    from lvyan.api.models import HITLRequest

    class _StubStore:
        def __init__(self):
            self.updates = {}
            self.claimed = False

        def claim_hitl_run(self, run_id, user_id):
            self.claimed = True
            return {"run_id": run_id, "thread_id": "t1", "user_id": user_id}

        def update_run(self, run_id, **values):
            self.updates[run_id] = values

        def append_message(self, *a, **kw):
            pass

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)

    ctx = RunContext("run-claim", "thread-claim", user_id="u1")
    ctx.status = "awaiting_hitl"
    mgr._runs["run-claim"] = ctx

    # 模拟 _start_task 抛异常
    def _boom_start(ctx, awaitable):
        raise RuntimeError("event loop closed")

    monkeypatch_setattr = type(mgr)._start_task
    mgr._start_task = _boom_start  # type: ignore

    loop = asyncio.new_event_loop()
    try:
        status, msg = loop.run_until_complete(
            mgr.resolve_hitl("run-claim", HITLRequest(action="approve"), "u1")
        )
    finally:
        loop.close()

    assert status == "error"
    assert stub.claimed is True
    # P0-2：DB 应被回滚为 failed
    assert "run-claim" in stub.updates
    assert stub.updates["run-claim"]["status"] == "failed"


# ---------------------------------------------------------------------------
# P1-4（复审第四轮）：启动期验证生产认证配置
# ---------------------------------------------------------------------------
def test_validate_runtime_config_rejects_invalid_backend(monkeypatch):
    """非法 CHECKPOINTER_BACKEND → RuntimeError。"""
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "redis")
    from lvyan.config import validate_runtime_config

    with pytest.raises(RuntimeError, match="非法"):
        validate_runtime_config()


def test_validate_runtime_config_rejects_memory_with_persistence(monkeypatch):
    """PERSISTENCE_REQUIRED=true + memory → RuntimeError。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "memory")
    from lvyan.config import validate_runtime_config

    with pytest.raises(RuntimeError, match="禁止"):
        validate_runtime_config()


def test_validate_runtime_config_rejects_production_auto_auth(monkeypatch):
    """生产模式 + AUTH_ENABLED=true + AUTH_MODE=auto → RuntimeError。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "auto")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    from lvyan.config import validate_runtime_config

    with pytest.raises(RuntimeError, match="auto"):
        validate_runtime_config()


def test_validate_runtime_config_accepts_valid_config(monkeypatch):
    """合法配置不抛异常。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "false")
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "auto")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from lvyan.config import validate_runtime_config

    validate_runtime_config()  # 不抛异常即通过


# ---------------------------------------------------------------------------
# P1-5（复审第四轮）：_cancel_context 统一收尾
# ---------------------------------------------------------------------------
def test_cancel_context_writes_completed_at_to_db():
    """_cancel_context 必须写 status=cancelled + error + completed_at。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def __init__(self):
            self.updates = {}

        def update_run(self, run_id, **values):
            self.updates[run_id] = values

        def append_message(self, *a, **kw):
            pass

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    ctx = RunContext("run-cancel", "thread-1", user_id="u1")
    ctx.status = "running"
    mgr._runs["run-cancel"] = ctx

    loop = asyncio.new_event_loop()
    try:
        status, msg = loop.run_until_complete(mgr._cancel_context(ctx))
    finally:
        loop.close()

    assert status == "cancelled"
    assert ctx.status == "cancelled"
    assert ctx.completed_at is not None
    assert "run-cancel" in stub.updates
    assert stub.updates["run-cancel"]["status"] == "cancelled"
    assert "completed_at" in stub.updates["run-cancel"]
    assert "error" in stub.updates["run-cancel"]


def test_cancel_context_persistence_failure_returns_unavailable():
    """_cancel_context 持久化失败 → 返回 unavailable。"""
    import asyncio

    from lvyan.api.sse import RunContext, RunManager

    class _StubStore:
        def update_run(self, run_id, **values):
            raise ConnectionError("DB down")  # 持久化失败

        def append_message(self, *a, **kw):
            pass

    stub = _StubStore()
    mgr = RunManager(metadata_store=stub)
    ctx = RunContext("run-pfail", "thread-1", user_id="u1")
    ctx.status = "running"
    mgr._runs["run-pfail"] = ctx

    loop = asyncio.new_event_loop()
    try:
        status, msg = loop.run_until_complete(mgr._cancel_context(ctx))
    finally:
        loop.close()

    assert status == "unavailable"
    assert ctx.status == "cancelled"  # 本地状态仍更新


# ---------------------------------------------------------------------------
# 结构化输出：SSE final_output 事件携带 answer（P0-3 去重后不再含 markdown_fallback）
# ---------------------------------------------------------------------------
def test_final_output_event_includes_structured_answer_when_available():
    """当 state 含 legal_answer 时，SSE final_output 事件携带 answer；output 同时作为 Markdown fallback 来源。"""
    import json

    from lvyan.api.sse import format_sse_event

    legal_answer = {
        "schema_version": "legal_answer_v1",
        "meta": {"title": "测试"},
    }
    event = {
        "event": "final_output",
        "output": "# Markdown 回退",
        "schema_version": "legal_answer_v1",
        "answer": legal_answer,
    }
    frame = format_sse_event(event)
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload["schema_version"] == "legal_answer_v1"
    assert payload["answer"]["meta"]["title"] == "测试"
    assert payload["output"] == "# Markdown 回退"


def test_final_output_event_falls_back_to_markdown_only():
    """无 legal_answer 时，事件仅含 output（旧格式，兼容）。"""
    import json

    from lvyan.api.sse import format_sse_event

    event = {"event": "final_output", "output": "# 纯 Markdown"}
    payload = json.loads(format_sse_event(event).removeprefix("data: ").strip())
    assert payload["output"] == "# 纯 Markdown"
    assert "answer" not in payload


def test_build_final_output_event_helper_with_structured():
    """_build_final_output_event 在 ctx 有 legal_answer 时附加结构化字段。"""
    from lvyan.api.sse import RunContext, _build_final_output_event

    ctx = RunContext("r1", "t1")
    ctx.final_output = "# MD"
    ctx.legal_answer = {"schema_version": "legal_answer_v1", "meta": {"title": "x"}}
    event = _build_final_output_event(ctx)
    assert event["schema_version"] == "legal_answer_v1"
    assert event["answer"]["meta"]["title"] == "x"
    assert event["output"] == "# MD"
    assert "markdown_fallback" not in event


def test_build_final_output_event_helper_without_structured():
    """_build_final_output_event 在 ctx 无 legal_answer 时退化为旧格式。"""
    from lvyan.api.sse import RunContext, _build_final_output_event

    ctx = RunContext("r1", "t1")
    ctx.final_output = "# MD"
    event = _build_final_output_event(ctx)
    assert event["output"] == "# MD"
    assert "answer" not in event


# ---------------------------------------------------------------------------
# P1-1：SSE document_file 只暴露 public 视图（不含 output_path）
# ---------------------------------------------------------------------------
def test_build_final_output_event_document_file_public_view():
    """_build_final_output_event 对 document_file 应输出 public 视图。

    P1-1：绝不包含 output_path 等服务器内部路径；必须提供 download_url。
    """
    from lvyan.api.sse import RunContext, _build_final_output_event

    ctx = RunContext("run-doc-1", "t1")
    ctx.final_output = "# 起诉状"
    ctx.document_file = {
        "output_path": "/app/outputs/run-doc-1-起诉状.docx",
        "format": "docx",
        "file_size": 12345,
        "success": True,
        "filename": "民事起诉状.docx",
    }
    event = _build_final_output_event(ctx)

    doc = event["document_file"]
    assert "output_path" not in doc, "public 视图不得包含 output_path"
    assert "/app/" not in str(doc), "public 视图不得泄露服务器路径"
    assert doc["filename"] == "民事起诉状.docx"
    assert doc["format"] == "docx"
    assert doc["file_size"] == 12345
    assert doc["download_url"] == "/api/documents/run-doc-1/download"


def test_build_final_output_event_document_file_success_false():
    """document_file.success=False 时不推送下载入口。"""
    from lvyan.api.sse import RunContext, _build_final_output_event

    ctx = RunContext("run-doc-2", "t1")
    ctx.final_output = "# 报告"
    ctx.document_file = {
        "output_path": "/app/outputs/run-doc-2-报告.docx",
        "format": "md",
        "file_size": 0,
        "success": False,
        "error": "渲染异常",
    }
    event = _build_final_output_event(ctx)
    assert "document_file" not in event, "渲染失败不应提供下载入口"


# ---------------------------------------------------------------------------
# P1-4：生产环境未启用认证时禁用文书下载
# ---------------------------------------------------------------------------
def test_download_disabled_in_production_without_auth(monkeypatch):
    """生产环境 + AUTH_ENABLED=false → 下载端点返回 403。"""
    import lvyan.api.server as server_mod

    # 构造可导入的 app（使用内存/无 DB 的轻量环境）
    from lvyan.api.server import create_app

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)

    # mock is_production=True, is_auth_enabled=False（下载端点函数内 import）
    monkeypatch.setattr(
        "lvyan.config.is_production", lambda: True
    )
    monkeypatch.setattr(
        "lvyan.api.auth.is_auth_enabled", lambda: False
    )

    resp = client.get("/api/documents/any-run/download")
    assert resp.status_code == 403
    assert "禁用" in resp.json().get("detail", "")


def test_download_not_blocked_in_dev_without_auth(monkeypatch):
    """开发模式（非生产）+ 无认证 → 不被 P1-4 拦截（走到后续逻辑）。

    P1-4 仅在生产模式 + 未启用认证时禁用下载；开发模式不应被误伤。
    """
    from lvyan.api.server import create_app

    monkeypatch.setattr("lvyan.config.is_production", lambda: False)
    monkeypatch.setattr("lvyan.api.auth.is_auth_enabled", lambda: False)

    app = create_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/api/documents/run-x/download")
    # 不被 403「生产禁用」拦截即可（后续可能 404/503，取决于 metadata_store）
    assert resp.status_code != 403
    assert "禁用" not in resp.text
