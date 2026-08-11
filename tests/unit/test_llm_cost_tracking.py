"""P0-10 修复：LLMClient 成功调用必须把 token 与成本写入 CostTracker。

回归覆盖：
  1. ainvoke 成功 → 当前 cost thread 的 CostTracker 累计 input/output tokens
  2. ainvoke 失败 → 不计入成本（重试次数耗尽抛 RuntimeError）
  3. 未设置 cost thread → 不计入（避免归属错误）
"""

from __future__ import annotations

import pytest

from lvyan.llm import client as client_mod
from lvyan.observability import tracing


@pytest.fixture(autouse=True)
def _reset_cost_thread():
    """每个测试前后清除 cost thread 关联，避免相互污染。"""
    tracing.set_cost_thread(None)
    tracing._global_cost_tracker.reset()
    yield
    tracing.set_cost_thread(None)
    tracing._global_cost_tracker.reset()


def _make_client(monkeypatch):
    """构造 LLMClient 实例，注入可控网关。"""
    return client_mod.LLMClient(
        gateway_url="https://gateway.test",
        api_key="sk-test",
        default_chat_model="test-model",
        max_retries=1,
        timeout=5.0,
    )


@pytest.mark.asyncio
async def test_ainvoke_success_records_cost_to_tracker(monkeypatch):
    """ainvoke 成功后，当前 cost thread 应累计 input/output tokens。"""
    c = _make_client(monkeypatch)
    tracing.set_cost_thread("thread-1")

    async def fake_call_gateway(*, messages, model, temperature, max_tokens, **kwargs):
        return {
            "content": "ok",
            "input_tokens": 120,
            "output_tokens": 30,
        }

    monkeypatch.setattr(c, "_call_gateway", fake_call_gateway)

    await c.ainvoke(messages=[{"role": "user", "content": "hi"}])

    summary = tracing.get_cost_summary("thread-1")
    assert summary.total_tokens_in == 120, "input tokens 应计入 CostTracker"
    assert summary.total_tokens_out == 30, "output tokens 应计入 CostTracker"
    assert summary.total_cost >= 0.0, "cost 应非负"


@pytest.mark.asyncio
async def test_ainvoke_failure_does_not_record_cost(monkeypatch):
    """ainvoke 失败时不应计入成本（避免重试放大统计）。"""
    c = _make_client(monkeypatch)
    tracing.set_cost_thread("thread-fail")

    async def failing_call_gateway(*, messages, model, temperature, max_tokens, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(c, "_call_gateway", failing_call_gateway)

    with pytest.raises(RuntimeError):
        await c.ainvoke(messages=[{"role": "user", "content": "hi"}])

    summary = tracing.get_cost_summary("thread-fail")
    assert summary.total_tokens_in == 0
    assert summary.total_tokens_out == 0


@pytest.mark.asyncio
async def test_ainvoke_without_cost_thread_does_not_record(monkeypatch):
    """未设置 cost thread 时不应计入（避免归属错误）。"""
    c = _make_client(monkeypatch)
    tracing.set_cost_thread(None)

    async def fake_call_gateway(*, messages, model, temperature, max_tokens, **kwargs):
        return {"content": "ok", "input_tokens": 50, "output_tokens": 10}

    monkeypatch.setattr(c, "_call_gateway", fake_call_gateway)

    await c.ainvoke(messages=[{"role": "user", "content": "hi"}])

    summary = tracing.get_cost_summary("any-thread")
    assert summary.total_tokens_in == 0
    assert summary.total_tokens_out == 0


# ---------------------------------------------------------------------------
# P0-10 补全：同步兼容路径（chat_json → _legacy_request）同样计入成本。
# 生产节点实际走该路径，此前仅 ainvoke 记录导致成本追踪在生产链路空转。
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _FakeHttpClient:
    """可注入的 httpx.Client 替身（替代 httpx.Client）。"""

    def __init__(self, *args, **kwargs) -> None:
        self._resp = _FakeResp(
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        )

    def __enter__(self) -> "_FakeHttpClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, *args, **kwargs) -> _FakeResp:
        return self._resp


def test_legacy_request_records_cost_to_tracker(monkeypatch):
    """同步兼容路径成功调用后，当前 cost thread 应累计 input/output tokens。"""
    monkeypatch.setattr("httpx.Client", _FakeHttpClient)
    tracing.set_cost_thread("thread-legacy")

    result = client_mod._legacy_request(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        timeout=5.0,
    )
    assert result == '{"ok": true}'

    summary = tracing.get_cost_summary("thread-legacy")
    assert summary.total_tokens_in == 100, "同步路径 input tokens 应计入 CostTracker"
    assert summary.total_tokens_out == 20, "同步路径 output tokens 应计入 CostTracker"


def test_legacy_request_without_cost_thread_does_not_record(monkeypatch):
    """同步路径未设置 cost thread 时不应计入（避免归属错误）。"""
    monkeypatch.setattr("httpx.Client", _FakeHttpClient)
    tracing.set_cost_thread(None)

    result = client_mod._legacy_request(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        timeout=5.0,
    )
    assert result == '{"ok": true}'

    summary = tracing.get_cost_summary("thread-legacy-2")
    assert summary.total_tokens_in == 0
    assert summary.total_tokens_out == 0
