"""基于滑动窗口的内存速率限制中间件。

为关键写入端点提供 per-IP 速率限制，防止未认证场景下的资源滥用。
非分布式（进程内），适用于单实例部署；多实例生产部署应在 API Gateway 层做限流。

配置
----
通过环境变量控制：
  - ``RATE_LIMIT_ENABLED``：是否启用（默认 true）
  - ``RATE_LIMIT_RUN_RPM``：/api/agent/run 每分钟请求上限（默认 10）
  - ``RATE_LIMIT_UPLOAD_RPM``：/api/upload 每分钟请求上限（默认 20）
  - ``RATE_LIMIT_DEFAULT_RPM``：其他写入端点每分钟请求上限（默认 60）
"""

from __future__ import annotations

import os
import time
import logging
from collections import defaultdict
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_logger = logging.getLogger("lvyan.api.rate_limit")

__all__ = ["RateLimitMiddleware"]


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _is_enabled() -> bool:
    raw = os.getenv("RATE_LIMIT_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# 受限路径前缀 → 环境变量名
_PATH_LIMITS: tuple[tuple[str, str, int], ...] = (
    ("/api/agent/run", "RATE_LIMIT_RUN_RPM", 10),
    ("/api/upload", "RATE_LIMIT_UPLOAD_RPM", 20),
    ("/api/agent/hitl/", "RATE_LIMIT_DEFAULT_RPM", 60),
    ("/api/agent/cancel/", "RATE_LIMIT_DEFAULT_RPM", 60),
    ("/api/cases", "RATE_LIMIT_DEFAULT_RPM", 60),
)

# 不限制的路径（健康检查、静态资源、GET 读取）
_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/livez", "/readyz", "/api/health", "/", "/docs", "/redoc", "/openapi.json",
})


class _SlidingWindowCounter:
    """简单的滑动窗口计数器（60 秒窗口）。"""

    __slots__ = ("_timestamps",)

    def __init__(self) -> None:
        self._timestamps: list[float] = []

    def is_allowed(self, limit: int, now: float | None = None) -> bool:
        now = now or time.monotonic()
        cutoff = now - 60.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= limit:
            return False
        self._timestamps.append(now)
        return True

    @property
    def count(self) -> int:
        now = time.monotonic()
        cutoff = now - 60.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        return len(self._timestamps)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于客户端 IP 的滑动窗口速率限制。

    仅限制 POST/PATCH/DELETE 写入操作；GET 请求和健康检查端点不限制。
    超限返回 429 Too Many Requests。
    """

    # GC：每 5 分钟清理过期的 IP 计数器，避免内存泄漏
    _GC_INTERVAL: float = 300.0

    def __init__(self, app: Any, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self._counters: dict[str, dict[str, _SlidingWindowCounter]] = defaultdict(
            lambda: defaultdict(_SlidingWindowCounter)
        )
        self._last_gc: float = time.monotonic()
        self._enabled = _is_enabled()
        if self._enabled:
            _logger.info("速率限制已启用")
        else:
            _logger.info("速率限制已禁用（RATE_LIMIT_ENABLED=false）")

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _get_limit(self, path: str) -> int | None:
        for prefix, env_name, default in _PATH_LIMITS:
            if path.startswith(prefix):
                return _get_int(env_name, default)
        return None

    def _gc(self) -> None:
        now = time.monotonic()
        if now - self._last_gc < self._GC_INTERVAL:
            return
        self._last_gc = now
        stale_paths: list[str] = []
        for path, ip_counters in self._counters.items():
            stale_ips = [ip for ip, c in ip_counters.items() if c.count == 0]
            for ip in stale_ips:
                del ip_counters[ip]
            if not ip_counters:
                stale_paths.append(path)
        for p in stale_paths:
            del self._counters[p]

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._enabled:
            return await call_next(request)

        if request.method in {"GET", "OPTIONS", "HEAD"}:
            return await call_next(request)

        path = request.url.path
        if path in _EXEMPT_PATHS:
            return await call_next(request)

        limit = self._get_limit(path)
        if limit is None:
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        counter = self._counters[path][client_ip]

        if not counter.is_allowed(limit):
            _logger.warning(
                "速率限制触发: %s %s (client=%s, limit=%d/min)",
                request.method, path, client_ip, limit,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"请求过于频繁，请稍后再试（上限 {limit} 次/分钟）",
                },
                headers={"Retry-After": "60"},
            )

        self._gc()
        return await call_next(request)
