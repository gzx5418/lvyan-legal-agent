"""HTTP 请求指标中间件。

自动记录：
- 请求总数 (by method, path, status)
- 请求延迟直方图 (by method, path, status)
- 活跃连接数

路径归一化
----------
避免高基数 label：/api/agent/state/{thread_id} → /api/agent/state/:id
"""

from __future__ import annotations

import re
import time
import logging
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_logger = logging.getLogger("lvyan.observability.http_metrics")

# UUID 和数字 ID 归一化（降低 Prometheus 标签基数）
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_ID_PATTERN = re.compile(r"/\d+(?=/|$)")

# 不收集指标的路径
_SKIP_PATHS = frozenset({"/livez", "/readyz", "/metrics", "/openapi.json"})


def _normalize_path(path: str) -> str:
    """归一化路径以降低标签基数。"""
    path = _UUID_PATTERN.sub(":id", path)
    path = _ID_PATTERN.sub("/:id", path)
    return path


class HTTPMetricsMiddleware(BaseHTTPMiddleware):
    """自动记录 HTTP 请求指标的中间件。"""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
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

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        normalized_path = _normalize_path(path)
        method = request.method

        self._active.inc()
        start = time.perf_counter()

        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.perf_counter() - start
            self._active.dec()
            self._duration.labels(
                method=method, path=normalized_path, status_code=status
            ).observe(duration)
            self._total.labels(
                method=method, path=normalized_path, status_code=status
            ).inc()

        return response
