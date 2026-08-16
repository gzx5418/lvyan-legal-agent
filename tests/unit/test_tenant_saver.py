"""TenantAwareCheckpointer 的租户上下文与 LangGraph 协议回归测试。"""

from __future__ import annotations

import pytest


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, query: object, params: object = None) -> None:
        # sql.Composed / sql.SQL 对象转为字符串以便测试断言
        q = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.calls.append((q, params))


class _FakeSaver:
    def __init__(self) -> None:
        self.conn = _FakeConn()
        self.configs: list[dict] = []
        self.setup_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def aget_tuple(self, config: dict):
        self.configs.append(config)
        return {"config": config}

    async def aget(self, config: dict):
        self.configs.append(config)
        return {"config": config}

    async def aput(self, config: dict, checkpoint, metadata, new_versions):
        self.configs.append(config)
        return config

    async def aput_writes(self, config: dict, writes, task_id: str, task_path: str = ""):
        self.configs.append(config)

    async def alist(self, config: dict | None, **_kwargs):
        self.configs.append(config or {})
        yield {"config": config}


def _config(user_id: str = "user-a") -> dict:
    return {"configurable": {"thread_id": "thread-a", "user_id": user_id}}


@pytest.mark.asyncio
async def test_checkpoint_reads_set_current_tenant(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _FakeSaver()
    saver = TenantAwareCheckpointer(inner)

    assert await saver.aget_tuple(_config()) == {"config": _config()}
    assert inner.configs == [_config()]
    assert inner.conn.calls[0] == ("SELECT set_config('app.user_id', %s, false)", ("user-a",))


@pytest.mark.asyncio
async def test_checkpoint_operations_reject_missing_tenant_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    saver = TenantAwareCheckpointer(_FakeSaver())
    with pytest.raises(ValueError, match="user_id"):
        await saver.aget_tuple({"configurable": {"thread_id": "thread-a"}})


@pytest.mark.asyncio
async def test_alist_remains_an_async_iterator_and_sets_tenant(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _FakeSaver()
    saver = TenantAwareCheckpointer(inner)

    assert [item async for item in saver.alist(_config())] == [{"config": _config()}]
    assert inner.conn.calls[0][1] == ("user-a",)


@pytest.mark.asyncio
async def test_setup_installs_checkpoint_rls_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _FakeSaver()
    await TenantAwareCheckpointer(inner).setup()

    assert inner.setup_called is True
    assert any(
        "CREATE POLICY" in query and "tenant_checkpoints" in query for query, _ in inner.conn.calls
    )


class _NoConnSaver(_FakeSaver):
    """没有任何可识别连接属性的 saver（模拟连接池/版本变更）。"""

    def __init__(self) -> None:
        super().__init__()
        del self.conn


@pytest.mark.asyncio
async def test_missing_conn_fails_closed_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    saver = TenantAwareCheckpointer(_NoConnSaver())
    with pytest.raises(RuntimeError, match="fail-closed"):
        await saver.aget_tuple(_config())


@pytest.mark.asyncio
async def test_tenant_context_cleared_after_operation(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _FakeSaver()
    saver = TenantAwareCheckpointer(inner)
    await saver.aget_tuple(_config())

    # 操作结束后必须复位会话级上下文，防止残留租户串读
    assert inner.conn.calls == [
        ("SELECT set_config('app.user_id', %s, false)", ("user-a",)),
        ("SELECT set_config('app.user_id', '', false)", None),
    ]


@pytest.mark.asyncio
async def test_alist_releases_lock_before_iteration(monkeypatch):
    """alist 迭代期间锁必须已释放（同任务内再调用包装方法不得死锁）。"""
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _FakeSaver()
    saver = TenantAwareCheckpointer(inner)

    consumed = []
    async for item in saver.alist(_config()):
        consumed.append(item)
        # 消费期间再次调用（锁必须可用，否则死锁）
        await saver.aget(_config())
    assert consumed == [{"config": _config()}]


class _DeleteSaver(_FakeSaver):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


@pytest.mark.asyncio
async def test_adelete_thread_sets_tenant_and_clears(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    inner = _DeleteSaver()
    saver = TenantAwareCheckpointer(inner)
    await saver.adelete_thread("thread-a", _config())

    assert inner.deleted == ["thread-a"]
    assert inner.conn.calls == [
        ("SELECT set_config('app.user_id', %s, false)", ("user-a",)),
        ("SELECT set_config('app.user_id', '', false)", None),
    ]


@pytest.mark.asyncio
async def test_adelete_thread_rejects_missing_tenant_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    saver = TenantAwareCheckpointer(_DeleteSaver())
    with pytest.raises(ValueError, match="user_id"):
        await saver.adelete_thread("thread-a")


@pytest.mark.asyncio
async def test_async_delete_thread_does_not_bypass_wrapper(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    saver = TenantAwareCheckpointer(_DeleteSaver())
    with pytest.raises(RuntimeError, match="adelete_thread"):
        saver.delete_thread("thread-a", _config())


class _FakeSyncConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: object, params: object = None) -> None:
        q = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.calls.append((q, params))


class _FakeSyncSaver:
    def __init__(self) -> None:
        self.conn = _FakeSyncConn()
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)

    def get(self, config: dict):
        return config


def test_sync_delete_thread_sets_tenant_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import SyncTenantAwareCheckpointer

    inner = _FakeSyncSaver()
    saver = SyncTenantAwareCheckpointer(inner)
    saver.delete_thread("thread-a", _config())

    assert inner.deleted == ["thread-a"]
    assert inner.conn.calls == [
        ("SELECT set_config('app.user_id', %s, false)", ("user-a",)),
        ("SELECT set_config('app.user_id', '', false)", None),
    ]


def test_sync_delete_thread_rejects_missing_tenant_when_enforced(monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import SyncTenantAwareCheckpointer

    saver = SyncTenantAwareCheckpointer(_FakeSyncSaver())
    with pytest.raises(ValueError, match="user_id"):
        saver.delete_thread("thread-a")
