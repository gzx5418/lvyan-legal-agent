"""SSE 失败/恢复路径的回归测试。

覆盖两个历史 bug：
1. ``_fail_run`` 只发布 error 事件、不关闭队列，非浏览器客户端在
   ``await queue.get()`` 上永久阻塞（连接悬挂）；
2. HITL checkpoint 恢复的 except 回滚逻辑引用 try 块内才赋值的变量，
   早期异常（DB 故障）触发 UnboundLocalError。
"""

from __future__ import annotations

import asyncio

import pytest

from lvyan.api.sse import RunContext, RunManager


def _make_ctx() -> RunContext:
    return RunContext(run_id="run-fail-test", thread_id="thread-a")


@pytest.mark.asyncio
async def test_fail_run_closes_sse_queue():
    """failed 终态必须推送 None 哨兵关闭 SSE 流。"""
    manager = RunManager()
    ctx = manager._bind_context(_make_ctx()) if hasattr(manager, "_bind_context") else _make_ctx()
    ctx.status = "running"

    await manager._fail_run(ctx, code="run_failed", message="boom")

    assert ctx.status == "failed"
    # error 事件之后必须紧跟 None 哨兵
    first = await asyncio.wait_for(ctx.queue.get(), timeout=1.0)
    second = await asyncio.wait_for(ctx.queue.get(), timeout=1.0)
    assert first.get("event") == "error"
    assert second is None


@pytest.mark.asyncio
async def test_fail_run_without_publish_still_closes_queue():
    """publish=False（runner 已推送 error）路径同样必须关闭队列。"""
    manager = RunManager()
    ctx = manager._bind_context(_make_ctx()) if hasattr(manager, "_bind_context") else _make_ctx()
    ctx.status = "running"

    await manager._fail_run(ctx, message="boom", publish=False)

    sentinel = await asyncio.wait_for(ctx.queue.get(), timeout=1.0)
    assert sentinel is None


@pytest.mark.asyncio
async def test_hitl_checkpoint_recovery_early_exception_no_unbound(monkeypatch):
    """checkpoint 恢复在 get_run（DB 故障）阶段抛错时，不得抛 UnboundLocalError。"""
    import lvyan.api.sse as sse_mod

    manager = RunManager()

    class _BrokenStore:
        def get_run(self, run_id):
            raise ConnectionError("db down")

    manager._metadata_store = _BrokenStore()

    async def _broken_graph():
        raise ConnectionError("db down")

    monkeypatch.setattr(sse_mod, "_get_graph", _broken_graph)

    from lvyan.api.models import HITLRequest

    result = await manager._resolve_hitl_from_checkpoint(
        "run-x", HITLRequest(action="approve"), current_user_id="user-a"
    )
    # 必须返回可控的 ("error", ...) 而不是抛 UnboundLocalError
    assert result[0] == "error"
