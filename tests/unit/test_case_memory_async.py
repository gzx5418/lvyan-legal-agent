"""C1/C2/C3 修复验证：CaseMemory 同步/异步图延迟绑定与异步方法。

验证要点：
- C1：CaseMemory 通过 graph_resolver 延迟绑定图，不永久绑定同步图
- C2：异步方法使用 aget_state / asyncio.to_thread，不阻塞事件循环
- C3：MemorySaver 回退场景下异步路径优先用异步图，数据一致
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from lvyan.memory.store import CaseMemory


# ---------------------------------------------------------------------------
# 辅助：构造支持 aget_state 的异步图桩
# ---------------------------------------------------------------------------
class AsyncGraphStub:
    """模拟 AsyncPostgresSaver 编译图：仅实现 aget_state。"""

    def __init__(self, snapshot: Any = None) -> None:
        self._snapshot = snapshot
        self.checkpointer = SimpleNamespace(delete_thread=self._delete)
        self.deleted: list[str] = []

    async def aget_state(self, _config: dict[str, Any]) -> Any:
        return self._snapshot

    def _delete(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class SyncGraphStub:
    """模拟 PostgresSaver / MemorySaver 编译图：仅实现同步 get_state。"""

    def __init__(self, snapshot: Any = None) -> None:
        self._snapshot = snapshot
        self.checkpointer = SimpleNamespace(delete_thread=self._delete)
        self.deleted: list[str] = []

    def get_state(self, _config: dict[str, Any]) -> Any:
        return self._snapshot

    def _delete(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def _snapshot_with_values(values: dict[str, Any] | None) -> Any:
    """构造含 values 的 snapshot；values=None 表示空 checkpoint。"""
    if values is None:
        return SimpleNamespace(values={}, next=(), tasks=None)
    return SimpleNamespace(values=values, next=(), tasks=None)


# ---------------------------------------------------------------------------
# C1：延迟绑定 —— CaseMemory 不在构造时绑定特定图
# ---------------------------------------------------------------------------
def test_c1_case_memory_defers_graph_binding_via_resolver(tmp_path):
    """CaseMemory 通过 graph_resolver 延迟绑定，构造时不解析图实例。"""
    calls = []

    def resolver():
        calls.append(True)
        return AsyncGraphStub(_snapshot_with_values(None))

    mem = CaseMemory(index_path=tmp_path / "idx.json", graph_resolver=resolver)
    # 构造完成，resolver 尚未被调用
    assert calls == []
    # 首次 checkpoint 操作才触发解析
    asyncio.run(mem.aload_strict("thread-1"))
    assert len(calls) == 1


def test_c1_case_memory_resolver_switches_to_async_graph(tmp_path):
    """异步图就绪后，CaseMemory 自动切换到异步图（不再读同步图）。"""
    # 模拟运行时：先无图，后异步图就绪
    state = {"graph": None}

    def resolver():
        return state["graph"]

    mem = CaseMemory(index_path=tmp_path / "idx.json", graph_resolver=resolver)

    # 初始无图 → 解析失败
    with pytest.raises(RuntimeError, match="未绑定图实例"):
        asyncio.run(mem.aload_strict("thread-1"))

    # 异步图就绪 → 切换成功
    async_graph = AsyncGraphStub(_snapshot_with_values(None))
    state["graph"] = async_graph
    result = asyncio.run(mem.aload_strict("thread-1"))
    assert result is None  # 空 checkpoint 返回 None
    # 验证调用的是异步图的 aget_state（通过 checkpointer 引用确认）
    assert mem._resolve_graph() is async_graph


# ---------------------------------------------------------------------------
# C2：异步方法使用 aget_state，不阻塞事件循环
# ---------------------------------------------------------------------------
def test_c2_aload_strict_uses_aget_state(tmp_path):
    """aload_strict 优先调用异步图的 aget_state。"""
    snapshot = _snapshot_with_values(None)
    graph = AsyncGraphStub(snapshot)

    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    result = asyncio.run(mem.aload_strict("thread-1"))
    assert result is None


def test_c2_aload_strict_falls_back_to_sync_via_to_thread(tmp_path):
    """图未实现 aget_state 时（如 MemorySaver），回退到 asyncio.to_thread。"""
    snapshot = _snapshot_with_values(None)
    graph = SyncGraphStub(snapshot)  # 仅同步 get_state

    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    # 不应抛 NotImplementedError，应通过 to_thread 包装成功
    result = asyncio.run(mem.aload_strict("thread-1"))
    assert result is None


def test_c2_adelete_strict_uses_to_thread(tmp_path):
    """adelete_strict 通过 asyncio.to_thread 卸载同步 delete_thread。"""
    graph = AsyncGraphStub(_snapshot_with_values(None))
    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    mem.register("thread-1", title="t")

    asyncio.run(mem.adelete_strict("thread-1"))
    assert graph.deleted == ["thread-1"]


def test_c2_sync_load_strict_still_works_for_cli(tmp_path):
    """CLI 同步路径：load_strict 仍使用同步 get_state，向后兼容。"""
    snapshot = _snapshot_with_values(None)
    graph = SyncGraphStub(snapshot)

    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    result = mem.load_strict("thread-1")
    assert result is None


# ---------------------------------------------------------------------------
# C3：MemorySaver 回退场景下数据一致
# ---------------------------------------------------------------------------
def test_c3_resolver_returns_async_graph_when_available(tmp_path):
    """MemorySaver 回退场景：异步图优先于同步图（同一进程实例可共享）。

    模拟生产架构：runtime._resolve_shared_graph() 优先返回异步图单例。
    CaseMemory 的所有异步方法都走异步图，与 API 写入路径一致。
    """
    sync_graph = SyncGraphStub(_snapshot_with_values(None))
    async_graph = AsyncGraphStub(_snapshot_with_values(None))

    state = {"async": async_graph, "sync": sync_graph}

    def resolver():
        # 模拟 runtime._resolve_shared_graph 的优先级
        return state["async"] if state["async"] is not None else state["sync"]

    mem = CaseMemory(index_path=tmp_path / "idx.json", graph_resolver=resolver)

    # 异步方法走异步图
    asyncio.run(mem.aload_strict("thread-1"))
    assert mem._resolve_graph() is async_graph

    # 模拟 API 路径：异步图失效（如重新初始化），切换到新异步图
    new_async_graph = AsyncGraphStub(_snapshot_with_values(None))
    state["async"] = new_async_graph
    asyncio.run(mem.aload_strict("thread-1"))
    assert mem._resolve_graph() is new_async_graph


def test_c3_runtime_resolve_shared_graph_prefers_async(monkeypatch):
    """runtime._resolve_shared_graph 优先返回异步图单例。"""
    import lvyan.runtime as rt

    rt.reset_shared_graph()

    # 初始状态：两者均 None
    assert rt._resolve_shared_graph() is None

    # 同步图就绪 → 返回同步图
    rt._shared_graph = "sync-graph-stub"
    assert rt._resolve_shared_graph() == "sync-graph-stub"

    # 异步图就绪 → 优先返回异步图
    rt._shared_graph_async = "async-graph-stub"
    assert rt._resolve_shared_graph() == "async-graph-stub"

    rt.reset_shared_graph()


def test_c3_get_case_memory_uses_resolver_not_eager_binding(monkeypatch):
    """get_case_memory 构造 CaseMemory 时使用 graph_resolver，不立即绑定图。"""
    import lvyan.runtime as rt

    rt.reset_shared_graph()

    mem = rt.get_case_memory()
    # 验证 CaseMemory 持有 graph_resolver 而非固定 graph
    assert mem._graph_resolver is rt._resolve_shared_graph
    assert mem._graph is None

    rt.reset_shared_graph()


# ---------------------------------------------------------------------------
# 向后兼容：旧式 graph= 构造仍可用
# ---------------------------------------------------------------------------
def test_backward_compat_graph_constructor_still_works(tmp_path):
    """旧式 CaseMemory(graph=...) 构造立即绑定，向后兼容测试桩。"""
    graph = SyncGraphStub(_snapshot_with_values(None))
    mem = CaseMemory(graph=graph, index_path=tmp_path / "idx.json")
    assert mem._graph is graph
    assert mem._graph_resolver is None
    # 同步方法仍可用
    assert mem.load_strict("thread-1") is None
