"""可观测性 tracing 装饰器、成本追踪与 Langfuse 降级单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from lvyan.observability import tracing
from lvyan.observability.tracing import (
    CostSummary,
    CostTracker,
    get_cost_summary,
    get_tracer,
    record_evaluation,
    record_llm_call,
    trace_node,
    trace_retrieval,
    trace_tool,
)


# ---------------------------------------------------------------------------
# 1. get_tracer 不报错并返回非 None
# ---------------------------------------------------------------------------
def test_get_tracer_returns_non_none():
    tracer = get_tracer("lvyan.test")
    assert tracer is not None


# ---------------------------------------------------------------------------
# 2. 装饰器在未配置 OpenTelemetry SDK 时不报错（降级 no-op）
# ---------------------------------------------------------------------------
def test_trace_node_without_otel_sdk_does_not_raise():
    @trace_node("triage")
    def run(x: int) -> int:
        return x + 1

    assert run(10) == 11


def test_trace_tool_without_otel_sdk_does_not_raise():
    @trace_tool("search_statutes")
    def run(q: str) -> str:
        return f"hit:{q}"

    assert run("民法典") == "hit:民法典"


def test_trace_retrieval_without_otel_sdk_does_not_raise():
    @trace_retrieval("hybrid")
    def run(q: str) -> list:
        return [q]

    assert run("第六百八十条") == ["第六百八十条"]


# ---------------------------------------------------------------------------
# 3. 配置真实 OTel SDK 时，装饰器记录节点名/耗时/输入输出摘要
# ---------------------------------------------------------------------------
class _InMemorySpanExporter:
    """最小内存 Span exporter，避免引入 opentelemetry-test-utils 依赖。"""

    from opentelemetry.sdk.trace.export import SpanExportResult as _Result

    def __init__(self) -> None:
        self._spans: list = []

    def export(self, spans) -> object:  # type: ignore[override]
        self._spans.extend(spans)
        return self._Result.SUCCESS

    def shutdown(self) -> None:  # type: ignore[override]
        pass

    def get_finished_spans(self) -> list:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()


def _install_exporter(monkeypatch):
    """安装内存 Span exporter，返回 exporter 实例。"""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    exporter = _InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # 让 tracing 模块使用本 provider 的 tracer
    monkeypatch.setattr(tracing, "get_tracer", lambda name: provider.get_tracer(name))
    return exporter


def test_trace_node_records_attributes(monkeypatch):
    exporter = _install_exporter(monkeypatch)

    @trace_node("jurisdiction_triage")
    def run(state):
        return {"jurisdiction": "中国大陆"}

    result = run({"user_goal": "押金"})
    assert result == {"jurisdiction": "中国大陆"}

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "jurisdiction_triage"
    assert span.attributes.get("node.name") == "jurisdiction_triage"
    assert "node.duration_ms" in span.attributes
    assert span.attributes["node.duration_ms"] >= 0.0
    # 输入/输出摘要应被记录
    assert "node.input_summary" in span.attributes
    assert "node.output_summary" in span.attributes


def test_trace_tool_records_tool_name(monkeypatch):
    exporter = _install_exporter(monkeypatch)

    @trace_tool("search_statutes")
    def run(query: str) -> dict:
        return {"count": 1}

    run("民法典")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("tool.name") == "search_statutes"


def test_trace_retrieval_records_strategy(monkeypatch):
    exporter = _install_exporter(monkeypatch)

    @trace_retrieval("bm25")
    def run(query: str) -> list:
        return []

    run("押金")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("retrieval.strategy") == "bm25"


# ---------------------------------------------------------------------------
# 4. 装饰器记录异常并重新抛出
# ---------------------------------------------------------------------------
def test_trace_node_records_exception_and_reraises(monkeypatch):
    exporter = _install_exporter(monkeypatch)

    @trace_node("bad_node")
    def run():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    # 状态应为 ERROR
    from opentelemetry.trace import StatusCode

    assert span.status.status_code == StatusCode.ERROR
    # 应记录 exception 事件
    assert any("exception" in str(e.name).lower() for e in span.events)


# ---------------------------------------------------------------------------
# 5. 装饰器支持异步函数
# ---------------------------------------------------------------------------
def test_trace_node_supports_async(monkeypatch):
    exporter = _install_exporter(monkeypatch)

    @trace_node("async_node")
    async def run(x: int) -> int:
        await asyncio.sleep(0)
        return x * 3

    result = asyncio.run(run(4))
    assert result == 12

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("node.name") == "async_node"


# ---------------------------------------------------------------------------
# 6. Langfuse 未配置时 record_llm_call / record_evaluation 不报错（降级 no-op）
# ---------------------------------------------------------------------------
def test_record_llm_call_no_op_without_langfuse(monkeypatch):
    # 强制 langfuse 不可用
    monkeypatch.setattr(tracing, "_langfuse_client", None, raising=False)
    # 不应抛出
    record_llm_call(
        model="qwen",
        prompt="你好",
        response="你好",
        tokens_in=10,
        tokens_out=5,
        cost=0.001,
    )


def test_record_evaluation_no_op_without_langfuse(monkeypatch):
    monkeypatch.setattr(tracing, "_langfuse_client", None, raising=False)
    record_evaluation("citation_accuracy", 0.95, comment="ok")


# ---------------------------------------------------------------------------
# 7. record_llm_call 同时计入成本追踪
# ---------------------------------------------------------------------------
def test_record_llm_call_tracks_cost(monkeypatch):
    # 使用全局成本追踪器，先重置
    from lvyan.observability.tracing import _global_cost_tracker

    _global_cost_tracker.reset("cost-test-thread")
    monkeypatch.setattr(tracing, "_langfuse_client", None, raising=False)
    # 让 record_llm_call 用指定 thread_id（通过 contextvar）
    tracing.set_cost_thread("cost-test-thread")
    try:
        record_llm_call(
            model="qwen",
            prompt="p",
            response="r",
            tokens_in=100,
            tokens_out=20,
            cost=0.02,
        )
        record_llm_call(
            model="qwen",
            prompt="p2",
            response="r2",
            tokens_in=50,
            tokens_out=10,
            cost=0.01,
        )
    finally:
        tracing.set_cost_thread(None)

    summary = get_cost_summary("cost-test-thread")
    assert summary.total_tokens_in == 150
    assert summary.total_tokens_out == 30
    assert summary.total_cost == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# 8. CostTracker 累计 token 与成本
# ---------------------------------------------------------------------------
def test_cost_tracker_accumulates():
    tracker = CostTracker()
    tracker.add("t1", tokens_in=100, tokens_out=50, cost=0.01)
    tracker.add("t1", tokens_in=200, tokens_out=30, cost=0.02)

    summary = tracker.get("t1")
    assert isinstance(summary, CostSummary)
    assert summary.thread_id == "t1"
    assert summary.total_tokens_in == 300
    assert summary.total_tokens_out == 80
    assert summary.total_cost == pytest.approx(0.03)


def test_cost_tracker_unknown_thread_returns_zero():
    tracker = CostTracker()
    summary = tracker.get("unknown")
    assert summary.total_tokens_in == 0
    assert summary.total_tokens_out == 0
    assert summary.total_cost == 0.0


def test_cost_tracker_threads_isolated():
    tracker = CostTracker()
    tracker.add("a", tokens_in=10, tokens_out=1, cost=0.001)
    tracker.add("b", tokens_in=20, tokens_out=2, cost=0.002)
    assert tracker.get("a").total_tokens_in == 10
    assert tracker.get("b").total_tokens_in == 20


def test_cost_tracker_reset():
    tracker = CostTracker()
    tracker.add("t", tokens_in=10, tokens_out=5, cost=0.01)
    tracker.reset("t")
    assert tracker.get("t").total_tokens_in == 0


# ---------------------------------------------------------------------------
# 9. metrics 桩不报错
# ---------------------------------------------------------------------------
def test_metrics_recorder_smoke():
    from lvyan.observability.metrics import MetricsRecorder

    rec = MetricsRecorder()
    rec.record_node("triage", duration_ms=12.3)
    rec.record_node("triage", duration_ms=20.0)
    rec.record_tool_call("search_statutes", duration_ms=5.0, success=True)
    snap = rec.snapshot()
    assert "nodes" in snap
    assert snap["nodes"]["triage"]["count"] == 2
    assert snap["nodes"]["triage"]["total_ms"] == pytest.approx(32.3)
