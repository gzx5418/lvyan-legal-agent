"""验证 RunManager 为 RunContext 注入历史读取回调。"""
from __future__ import annotations

from lvyan.api.sse import RunManager


def test_make_history_loader_returns_callable_when_store_present():
    class FakeStore:
        def __init__(self):
            self.called = False

        def list_messages(self, thread_id, user_id):
            self.called = True
            return [{"role": "user", "content": "历史问题"}]

    store = FakeStore()
    mgr = RunManager(metadata_store=store)
    loader = mgr._make_history_loader("thread-1", "u1")
    assert callable(loader)
    msgs = loader()
    assert store.called is True
    assert msgs == [{"role": "user", "content": "历史问题"}]


def test_make_history_loader_returns_none_when_no_store():
    mgr = RunManager(metadata_store=None)
    assert mgr._make_history_loader("thread-1", "u1") is None


def test_make_history_loader_swallows_errors():
    class BadStore:
        def list_messages(self, thread_id, user_id):
            raise RuntimeError("db down")

    mgr = RunManager(metadata_store=BadStore())
    loader = mgr._make_history_loader("thread-1", "u1")
    assert loader() == []


def test_default_runner_writes_conversation_summary_into_initial_state(monkeypatch):
    """default_runner 调用 load_history 并把格式化结果写入初始 state。"""
    import asyncio
    from lvyan.api.sse import RunContext, default_runner

    captured = {}

    async def fake_stream(graph, initial, config, ctx, **kw):
        captured["initial"] = initial
        return ("", None)

    monkeypatch.setattr("lvyan.api.sse._stream_graph_events", fake_stream)

    async def fake_get_graph():
        return object()

    monkeypatch.setattr("lvyan.api.sse._get_graph", fake_get_graph)

    class FakeCaseMem:
        def register(self, *a, **kw):
            pass

    monkeypatch.setattr("lvyan.runtime.get_case_memory", lambda: FakeCaseMem())
    monkeypatch.setattr("lvyan.observability.tracing.set_cost_thread", lambda _: None)

    ctx = RunContext("run-1", "thread-1", user_id="u1")
    ctx.load_history = lambda: [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    asyncio.run(default_runner("本轮问题", "thread-1", "deep", ctx))
    assert captured["initial"]["conversation_summary"].count("上一轮问题") == 1
    assert captured["initial"]["conversation_summary"].count("上一轮回答") == 1

