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
    assert any("CREATE POLICY" in query and "tenant_checkpoints" in query for query, _ in inner.conn.calls)
