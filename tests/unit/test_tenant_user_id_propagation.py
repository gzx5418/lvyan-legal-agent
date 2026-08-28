"""CaseMemory / CLI 必须把 user_id 传给租户感知 checkpointer。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lvyan.common.constants import CLI_USER_ID
from lvyan.memory.store import CaseMemory


class _RecordingAsyncGraph:
    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, dict[str, Any] | None]] = []
        self.listed: list[dict[str, Any] | None] = []
        self.checkpointer = self

    async def aget_state(self, config: dict[str, Any]) -> Any:
        self.configs.append(config)
        return SimpleNamespace(values={}, next=(), tasks=None)

    async def alist(self, config: dict[str, Any] | None = None, **_kwargs):
        self.listed.append(config)
        yield SimpleNamespace(config={"configurable": {"thread_id": "thread-1"}})

    async def adelete_thread(self, thread_id: str, config: dict[str, Any] | None = None) -> None:
        self.deleted.append((thread_id, config))


class _RecordingSyncGraph:
    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, dict[str, Any] | None]] = []
        self.checkpointer = self

    def get_state(self, config: dict[str, Any]) -> Any:
        self.configs.append(config)
        return SimpleNamespace(values={}, next=(), tasks=None)

    def delete_thread(self, thread_id: str, config: dict[str, Any] | None = None) -> None:
        self.deleted.append((thread_id, config))


@pytest.mark.asyncio
async def test_case_memory_async_ops_pass_user_id(tmp_path):
    graph = _RecordingAsyncGraph()
    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    mem.register("thread-1", title="t", user_id="alice")

    await mem.aload_strict("thread-1", user_id="alice")
    await mem.alist_threads_strict(user_id="alice")
    await mem.adelete_strict("thread-1", user_id="alice")

    assert graph.configs[0]["configurable"]["user_id"] == "alice"
    assert graph.listed == [{"configurable": {"user_id": "alice"}}]
    assert graph.deleted[0][0] == "thread-1"
    assert graph.deleted[0][1]["configurable"]["user_id"] == "alice"


def test_case_memory_sync_ops_pass_user_id(tmp_path):
    graph = _RecordingSyncGraph()
    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    mem.register("thread-1", title="t", user_id="cli")

    mem.load_strict("thread-1", user_id="cli")
    mem.delete_strict("thread-1", user_id="cli")

    assert graph.configs[0]["configurable"]["user_id"] == "cli"
    assert graph.deleted[0][1]["configurable"]["user_id"] == "cli"


@pytest.mark.asyncio
async def test_alist_without_user_id_fails_when_rls_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    class _Inner:
        conn = None

        async def alist(self, config=None, **_kwargs):
            if False:
                yield None

    graph = SimpleNamespace(checkpointer=TenantAwareCheckpointer(_Inner()))
    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    mem.register("thread-1", title="t", user_id="alice")

    with pytest.raises(ValueError, match="user_id"):
        await mem.alist_threads_strict()


@pytest.mark.asyncio
async def test_aload_without_user_id_fails_when_rls_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("RLS_ENFORCED", "true")
    from lvyan.db.tenant_saver import TenantAwareCheckpointer

    class _Inner:
        conn = None

        async def aget_tuple(self, config):
            return None

    class _Graph:
        def __init__(self) -> None:
            self.checkpointer = TenantAwareCheckpointer(_Inner())

        async def aget_state(self, config):
            await self.checkpointer.aget_tuple(config)
            return SimpleNamespace(values={}, next=(), tasks=None)

    mem = CaseMemory(graph=_Graph(), index_path=tmp_path / "idx.json")
    with pytest.raises(ValueError, match="user_id"):
        await mem.aload_strict("thread-1")


def test_cli_invoke_config_includes_cli_tenant(monkeypatch):
    captured: dict[str, Any] = {}

    class _Graph:
        def invoke(self, state, config):
            captured["config"] = config
            captured["state"] = state
            return {"final_output": "ok"}

    monkeypatch.setattr("lvyan.main.get_shared_graph", lambda: _Graph())
    from lvyan.main import run_agent_with_state

    result = run_agent_with_state("咨询合同纠纷", thread_id="thread-cli")
    assert result.final_output == "ok"
    assert captured["config"]["configurable"]["thread_id"] == "thread-cli"
    assert captured["config"]["configurable"]["user_id"] == CLI_USER_ID
    assert captured["state"]["user_id"] == CLI_USER_ID


def test_cli_stream_config_includes_cli_tenant(monkeypatch):
    captured: dict[str, Any] = {}

    class _Graph:
        def stream(self, _state, config, stream_mode="updates"):
            captured["config"] = config
            return []

    monkeypatch.setattr("lvyan.main.get_shared_graph", lambda: _Graph())
    from lvyan.main import stream_agent

    list(stream_agent("咨询合同纠纷", thread_id="thread-cli"))
    assert captured["config"]["configurable"]["user_id"] == CLI_USER_ID
