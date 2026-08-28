"""HTTPMetricsMiddleware（纯 ASGI）单元测试。

重构背景：原 BaseHTTPMiddleware 实现仅在响应头就绪时记录耗时，
SSE 流式响应的 duration 会在流结束前提前返回；纯 ASGI 实现改为在
响应体发送完毕（more_body=False）时记录，且异常路径必须归还
active_connections gauge。

测试不依赖 prometheus_client：向中间件实例注入假指标对象，
锁定指标名称维度（method/path/status_code）与归一化路径语义。
"""

from __future__ import annotations

import asyncio
from typing import Any

from lvyan.observability.http_metrics import HTTPMetricsMiddleware


class _FakeGauge:
    def __init__(self) -> None:
        self.value = 0.0

    def inc(self, amount: float = 1) -> None:
        self.value += amount

    def dec(self, amount: float = 1) -> None:
        self.value -= amount


class _FakeHistogram:
    def __init__(self) -> None:
        self.observations: list[tuple[dict[str, Any], float]] = []

    def labels(self, **labels: Any) -> "_FakeHistogram":
        self._last_labels = labels
        return self

    def observe(self, value: float) -> None:
        self.observations.append((dict(self._last_labels), value))


class _FakeCounter:
    def __init__(self) -> None:
        self.increments: list[tuple[dict[str, Any], float]] = []

    def labels(self, **labels: Any) -> "_FakeCounter":
        self._last_labels = labels
        return self

    def inc(self, amount: float = 1) -> None:
        self.increments.append((dict(self._last_labels), amount))


def _make_middleware(app: Any) -> tuple[HTTPMetricsMiddleware, _FakeGauge, _FakeHistogram, _FakeCounter]:
    """构造已启用的中间件（注入假指标，不依赖 prometheus_client）。"""
    mw = HTTPMetricsMiddleware(app)
    active, duration, total = _FakeGauge(), _FakeHistogram(), _FakeCounter()
    mw._enabled = True
    mw._active = active
    mw._duration = duration
    mw._total = total
    return mw, active, duration, total


def _call(mw: HTTPMetricsMiddleware, scope: dict[str, Any]) -> None:
    async def _receive() -> dict[str, Any]:  # pragma: no cover - 中间件不消费
        return {"type": "http.request"}

    async def _send(_message: dict[str, Any]) -> None:
        return None

    asyncio.run(mw(scope, _receive, _send))


def test_non_http_scope_passes_through_without_metrics():
    async def lifespan_app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"

    mw, active, duration, total = _make_middleware(lifespan_app)
    _call(mw, {"type": "lifespan", "asgi": {"version": "3.0"}})

    assert active.value == 0
    assert duration.observations == []
    assert total.increments == []


def test_single_body_response_records_after_full_body():
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw, active, duration, total = _make_middleware(app)
    _call(
        mw,
        {
            "type": "http",
            "method": "GET",
            "path": "/api/documents/12345/download",
        },
    )

    assert active.value == 0  # gauge 必须归还
    assert len(duration.observations) == 1
    labels, elapsed = duration.observations[0]
    assert labels == {
        "method": "GET",
        "path": "/api/documents/:id/download",
        "status_code": "200",
    }
    assert elapsed >= 0
    assert total.increments == [(labels, 1)]


def test_streaming_response_records_only_after_final_chunk():
    sent: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        for chunk in (b"delta-1", b"delta-2"):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            sent.append("chunk")
            # 记录只发生在最终 chunk 之后：中途 gauge 仍为 1
            assert mw._active.value == 1
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        sent.append("final")

    mw, active, duration, _total = _make_middleware(app)
    _call(mw, {"type": "http", "method": "POST", "path": "/api/agent/stream"})

    assert sent[-1] == "final"
    assert active.value == 0
    assert len(duration.observations) == 1
    assert duration.observations[0][0]["status_code"] == "200"


def test_app_error_before_response_does_not_leak_gauge():
    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    mw, active, duration, _total = _make_middleware(app)

    try:
        _call(mw, {"type": "http", "method": "GET", "path": "/api/agent/runs"})
    except RuntimeError:
        pass
    else:  # pragma: no cover - 必须抛出
        raise AssertionError("app 异常应向上传播")

    assert active.value == 0
    assert len(duration.observations) == 1
    assert duration.observations[0][0]["status_code"] == "500"


def test_skip_paths_are_not_measured():
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw, active, duration, total = _make_middleware(app)
    _call(mw, {"type": "http", "method": "GET", "path": "/readyz"})

    assert active.value == 0
    assert duration.observations == []
    assert total.increments == []


def test_disabled_middleware_passes_through():
    calls: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope.get("path", ""))

    mw = HTTPMetricsMiddleware(app)  # 无 prometheus_client 时 _enabled=False
    mw._enabled = False
    _call(mw, {"type": "http", "method": "GET", "path": "/api/agent/runs"})

    assert calls == ["/api/agent/runs"]
