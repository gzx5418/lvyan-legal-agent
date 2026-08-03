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
