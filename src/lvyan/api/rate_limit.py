"""速率限制中间件（支持内存 / Redis 后端）。

为关键写入端点提供 per-user / per-IP 速率限制，防止资源滥用。

后端选择
--------
- ``RATE_LIMIT_BACKEND=memory``：进程内滑动窗口（单实例，开发/测试）
- ``RATE_LIMIT_BACKEND=redis``：Redis sorted set（多实例生产环境）

配置
----
通过环境变量控制：
  - ``RATE_LIMIT_ENABLED``：是否启用（默认 true）
  - ``RATE_LIMIT_BACKEND``：后端类型 memory|redis（默认 memory）
  - ``REDIS_URL``：Redis 连接地址（redis backend 必需）
  - ``RATE_LIMIT_RUN_RPM``：/api/agent/run 每分钟请求上限（默认 10）
  - ``RATE_LIMIT_UPLOAD_RPM``：/api/upload 每分钟请求上限（默认 20）
  - ``RATE_LIMIT_DEFAULT_RPM``：其他写入端点每分钟请求上限（默认 60）

限流键策略
----------
- 已认证用户：按 user_id 限流（独立于 IP）
- 匿名请求：按 X-Forwarded-For 可信代理解析后的 IP 限流
"""

from __future__ import annotations

import os
import time
import logging
from collections import defaultdict
from typing import Any, Protocol

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


def _get_backend_type() -> str:
    raw = os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower()
    return raw if raw in {"memory", "redis"} else "memory"


def _get_trusted_proxies() -> frozenset[str]:
    """解析 TRUSTED_PROXIES 环境变量（逗号分隔的 IP 白名单）。

    仅当请求来源 IP 在此白名单内时，才采信 X-Forwarded-For；否则一律使用
    直连 IP，避免客户端伪造 XFF 头绕过限流。
    """
    raw = os.getenv("TRUSTED_PROXIES", "").strip()
    if not raw:
        return frozenset()
    return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())


# 受限路径前缀 → 环境变量名
_PATH_LIMITS: tuple[tuple[str, str, int], ...] = (
    ("/api/agent/run", "RATE_LIMIT_RUN_RPM", 10),
    ("/api/upload", "RATE_LIMIT_UPLOAD_RPM", 20),
    ("/api/agent/hitl/", "RATE_LIMIT_DEFAULT_RPM", 60),
    ("/api/agent/cancel/", "RATE_LIMIT_DEFAULT_RPM", 60),
    ("/api/cases", "RATE_LIMIT_DEFAULT_RPM", 60),
)

# 高成本写路径前缀（Redis 不可用时返回 503）
_HIGH_COST_PREFIXES: tuple[str, ...] = (
    "/api/agent/run",
    "/api/upload",
    "/api/agent/hitl/",
)

# 不限制的路径（健康检查、静态资源、GET 读取）
_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/livez",
        "/readyz",
        "/api/health",
        "/metrics",
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


# ---------------------------------------------------------------------------
# 后端协议
# ---------------------------------------------------------------------------
class RateLimitBackend(Protocol):
    """速率限制后端协议。"""

    def is_allowed(self, key: str, limit: int) -> bool:
        """检查是否允许请求。返回 True 允许，False 拒绝。"""
        ...

    def is_healthy(self) -> bool:
        """后端是否健康可用。"""
        ...


# ---------------------------------------------------------------------------
# 内存后端
# ---------------------------------------------------------------------------
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


class InMemoryBackend:
    """进程内滑动窗口限流后端（单实例用）。"""

    _GC_INTERVAL: float = 300.0

    def __init__(self) -> None:
        self._counters: dict[str, _SlidingWindowCounter] = defaultdict(_SlidingWindowCounter)
        self._last_gc: float = time.monotonic()

    def is_allowed(self, key: str, limit: int) -> bool:
        self._gc()
        return self._counters[key].is_allowed(limit)

    def is_healthy(self) -> bool:
        return True

    def _gc(self) -> None:
        now = time.monotonic()
        if now - self._last_gc < self._GC_INTERVAL:
            return
        self._last_gc = now
        stale = [k for k, c in self._counters.items() if c.count == 0]
        for k in stale:
            del self._counters[k]


# ---------------------------------------------------------------------------
# Redis 后端
# ---------------------------------------------------------------------------
class RedisBackend:
    """基于 Redis sorted set 的滑动窗口限流后端（多实例生产用）。

    算法：对每个 key 维护一个 sorted set，score = 请求时间戳。
    检查时先清理 60s 前的条目，再判断集合大小是否超限。
    """

    _WINDOW_SECONDS: int = 60

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any = None
        self._healthy = False
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._client = redis.Redis.from_url(
                self._redis_url,
                socket_connect_timeout=2.0,
                socket_timeout=1.0,
                decode_responses=False,
            )
            self._client.ping()
            self._healthy = True
            _logger.info("Redis 限流后端连接成功: %s", self._redis_url[:30] + "...")
        except Exception as exc:  # noqa: BLE001 boundary-exception: Redis连接失败降级
            _logger.error("Redis 限流后端连接失败: %s", exc)
            self._healthy = False

    def is_allowed(self, key: str, limit: int) -> bool:
        if not self._healthy or self._client is None:
            return self._reconnect_and_check(key, limit)

        try:
            now = time.time()
            cutoff = now - self._WINDOW_SECONDS
            rkey = f"rl:{key}"
            # 阶段 1：清理过期条目并获取当前计数
            pipe = self._client.pipeline(transaction=True)
            pipe.zremrangebyscore(rkey, 0, cutoff)
            pipe.zcard(rkey)
            results = pipe.execute()
            current_count = results[1]
            if current_count >= limit:
                return False
            # 阶段 2：在限额内才添加新条目
            pipe2 = self._client.pipeline(transaction=True)
            pipe2.zadd(rkey, {f"{now}": now})
            pipe2.expire(rkey, self._WINDOW_SECONDS + 5)
            pipe2.execute()
            return True
        except Exception as exc:  # noqa: BLE001 boundary-exception: Redis操作失败
            _logger.warning("Redis 限流操作失败: %s", exc)
            self._healthy = False
            return True  # Redis 故障时不阻塞（fail-open for reads）

    def is_healthy(self) -> bool:
        if self._healthy:
            return True
        self._reconnect_and_check("__health__", 999)
        return self._healthy

    def _reconnect_and_check(self, key: str, limit: int) -> bool:
        try:
            self._connect()
            if self._healthy:
                return self.is_allowed(key, limit)
        except Exception:  # noqa: BLE001 boundary-exception: 重连失败
            pass
        return True  # 不可用时 fail-open


# ---------------------------------------------------------------------------
# 后端工厂
# ---------------------------------------------------------------------------
def _create_backend() -> RateLimitBackend:
    """根据配置创建限流后端实例。"""
    backend_type = _get_backend_type()
    if backend_type == "redis":
        redis_url = os.getenv("REDIS_URL", "").strip()
        if redis_url:
            return RedisBackend(redis_url)
        _logger.warning("RATE_LIMIT_BACKEND=redis 但 REDIS_URL 未配置，降级为内存后端")
    return InMemoryBackend()


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------
class RateLimitMiddleware(BaseHTTPMiddleware):
    """速率限制中间件。

    支持 per-user（认证用户）和 per-IP（匿名用户）限流。
    后端可选内存或 Redis。Redis 不可用时高成本写路径返回 503。
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self._enabled = _is_enabled()
        self._backend: RateLimitBackend = _create_backend()
        self._is_redis = _get_backend_type() == "redis"
        if self._enabled:
            backend_name = "redis" if self._is_redis else "memory"
            _logger.info("速率限制已启用（后端: %s）", backend_name)
        else:
            _logger.info("速率限制已禁用（RATE_LIMIT_ENABLED=false）")

    def _get_client_key(self, request: Request) -> str:
        """获取限流键：已认证用户用 user_id，否则用 IP。

        P0-5：从请求头解析已认证 user_id（捕获异常，限流层不抛错）并写入
        ``request.state.user_id``，使后续路由依赖可复用，避免重复解析。
        解析失败时退化为 IP 维度（后续路由层仍会做真正的认证拒绝）。
        """
        user_id = self._resolve_user_id(request)
        if user_id and user_id != "anonymous":
            return f"user:{user_id}"

        # 匿名请求：仅当来源是可信代理时才采信 X-Forwarded-For，否则用直连 IP，
        # 防止客户端伪造 XFF 头绕过限流。
        direct_ip = request.client.host if request.client else None
        trusted_proxies = _get_trusted_proxies()
        if direct_ip and direct_ip in trusted_proxies:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
                if client_ip:
                    return f"ip:{client_ip}"
        if direct_ip:
            return f"ip:{direct_ip}"
        return "ip:unknown"

    @staticmethod
    def _resolve_user_id(request: Request) -> str | None:
        """轻量解析 user_id 仅供限流维度使用（不抛异常）。

        认证未启用时返回 None；JWT/trusted_proxy 模式会真正调用认证解析，
        但任何异常都被吞掉——失败时退化为 IP 维度，真正的认证拒绝由后续
        路由依赖 :func:`get_current_user_id` 完成。
        """
        try:
            from lvyan.api.auth import get_current_user_id, is_auth_enabled

            if not is_auth_enabled():
                return None
            x_user_id = request.headers.get("x-user-id")
            authorization = request.headers.get("authorization")
            uid = get_current_user_id(request, x_user_id, authorization)
            if hasattr(request, "state"):
                request.state.user_id = uid
            return uid
        except Exception:  # noqa: BLE001 - 限流层不抛认证错误
            return None

    def _get_limit(self, path: str) -> int | None:
        for prefix, env_name, default in _PATH_LIMITS:
            if path.startswith(prefix):
                return _get_int(env_name, default)
        return None

    def _is_high_cost_path(self, path: str) -> bool:
        """路径是否为高成本写操作（Redis 不可用时应拒绝）。"""
        return any(path.startswith(p) for p in _HIGH_COST_PREFIXES)

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

        # Redis 后端不可用时：高成本写路径返回 503，读取不受影响
        if self._is_redis and not self._backend.is_healthy():
            if self._is_high_cost_path(path):
                _logger.warning("Redis 不可用，拒绝高成本写请求: %s %s", request.method, path)
                return JSONResponse(
                    status_code=503,
                    content={"detail": "服务暂时不可用，请稍后重试"},
                    headers={"Retry-After": "30"},
                )

        client_key = self._get_client_key(request)
        rate_key = f"{path}:{client_key}"

        if not self._backend.is_allowed(rate_key, limit):
            _logger.warning(
                "速率限制触发: %s %s (key=%s, limit=%d/min)",
                request.method,
                path,
                client_key,
                limit,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"请求过于频繁，请稍后再试（上限 {limit} 次/分钟）",
                },
                headers={"Retry-After": "60"},
            )

        return await call_next(request)
