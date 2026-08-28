"""HTTP 请求指标中间件（纯 ASGI 实现）。

自动记录：
- 请求总数 (by method, path, status)
- 请求延迟直方图 (by method, path, status)
- 活跃连接数

路径归一化
----------
避免高基数 label：/api/agent/state/{thread_id} → /api/agent/state/:id

时序语义
--------
纯 ASGI（而非 BaseHTTPMiddleware）：duration/active_connections 在响应体
发送完毕（http.response.body 且 more_body=False）时才记录，因此 SSE 流式
响应的耗时覆盖整个流的生命周期，而非仅在响应头就绪时提前结束。
"""

from __future__ import annotations

import re
import time
import logging
from typing import Any

_logger = logging.getLogger("lvyan.observability.http_metrics")

# UUID 和数字 ID 归一化（降低 Prometheus 标签基数）
_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ID_PATTERN = re.compile(r"/\d+(?=/|$)")
# 本项目 run_id 格式为 run-<32位hex>、thread_id 为 thread-<hex12>，
# 出现在路径中如 /api/agent/stream/run-abc123...，需归一化避免高基数
_RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{8,}")
_THREAD_ID_PATTERN = re.compile(r"thread-[0-9a-f]{8,}")
# 无前缀的长 hex 段（≥16 位）同样视为可归一化的标识符
_LONG_HEX_PATTERN = re.compile(r"\b[0-9a-f]{16,}\b")

# 不收集指标的路径
_SKIP_PATHS = frozenset({"/livez", "/readyz", "/metrics", "/openapi.json"})


def _normalize_path(path: str) -> str:
    """归一化路径以降低标签基数。"""
    path = _UUID_PATTERN.sub(":id", path)
    path = _ID_PATTERN.sub("/:id", path)
    path = _RUN_ID_PATTERN.sub("run-:id", path)
    path = _THREAD_ID_PATTERN.sub("thread-:id", path)
    path = _LONG_HEX_PATTERN.sub(":hex", path)
    return path


def is_http_metrics_active() -> bool:
    """判断 HTTP 指标中间件在运行时是否真的会生效。

    与 ``HTTPMetricsMiddleware.__init__`` 使用同一可用性判据
    （``lvyan.observability.metrics`` 的 prometheus 可用性标志），供
    ``create_app`` 在注册时显式决策：只有中间件真正会采集指标时才把
    ``http_metrics`` 登记进 ``observability_components``，否则由
    /readyz 披露 degraded。
    """
    try:
        from lvyan.observability.metrics import _PROM_AVAILABLE
    except ImportError:
        return False
    return bool(_PROM_AVAILABLE)


class HTTPMetricsMiddleware:
    """纯 ASGI 中间件：响应体发送完毕后记录请求指标（支持 SSE 流式）。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._enabled = False
        try:
            from lvyan.observability.metrics import (
                HTTP_REQUEST_DURATION,
                HTTP_REQUEST_TOTAL,
                ACTIVE_CONNECTIONS,
                _PROM_AVAILABLE,
            )

            if _PROM_AVAILABLE:
                self._duration = HTTP_REQUEST_DURATION
                self._total = HTTP_REQUEST_TOTAL
                self._active = ACTIVE_CONNECTIONS
                self._enabled = True
        except ImportError:
            pass

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # 非 http scope（lifespan/websocket）或未启用时直接透传
        if scope.get("type") != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        normalized_path = _normalize_path(path)
        method = scope.get("method", "")

        status = "500"
        headers_sent = False
        recorded = False
        self._active.inc()
        start = time.perf_counter()

        async def send_wrapper(message: Any) -> None:
            nonlocal status, headers_sent, recorded
            if message["type"] == "http.response.start":
                headers_sent = True
                status = str(message.get("status", 500))
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                # 响应体发送完毕（含 SSE 流结束）才记录耗时并归还 gauge；
                # 幂等保护：病态的重复 final body 消息不得二次记录，
                # 否则会重复 observe/increment 并把 gauge 打成负数。
                if not recorded:
                    recorded = True
                    duration = time.perf_counter() - start
                    self._active.dec()
                    self._duration.labels(
                        method=method, path=normalized_path, status_code=status
                    ).observe(duration)
                    self._total.labels(
                        method=method, path=normalized_path, status_code=status
                    ).inc()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 语义：headers 未发出 → status 保持初始 "500"（从未收到
            # response.start）；headers 已发出 → 保留真实 status。两种情况
            # finally 都会用当前 status 补记指标并归还 gauge，异常原样上抛。
            raise
        finally:
            if not recorded:
                # 异常短路（未发出完整响应体）也要归还 gauge 并补记指标，
                # 避免 active_connections 泄漏。
                duration = time.perf_counter() - start
                self._active.dec()
                self._duration.labels(
                    method=method, path=normalized_path, status_code=status
                ).observe(duration)
                self._total.labels(method=method, path=normalized_path, status_code=status).inc()
