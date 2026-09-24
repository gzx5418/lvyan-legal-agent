"""速率限制后端测试：内存后端 + Redis 后端行为验证。"""

from __future__ import annotations

import time


class TestInMemoryBackend:
    """内存限流后端的单元测试。"""

    def test_allows_under_limit(self):
        from lvyan.api.rate_limit import InMemoryBackend

        backend = InMemoryBackend()
        # 限制 5 次/分钟
        for _ in range(5):
            assert backend.is_allowed("test:user1", 5) is True

    def test_rejects_over_limit(self):
        from lvyan.api.rate_limit import InMemoryBackend

        backend = InMemoryBackend()
        for _ in range(10):
            backend.is_allowed("test:user1", 10)
        assert backend.is_allowed("test:user1", 10) is False

    def test_different_keys_independent(self):
        from lvyan.api.rate_limit import InMemoryBackend

        backend = InMemoryBackend()
        for _ in range(5):
            backend.is_allowed("path:/api/run:user:alice", 5)

        # alice 满了
        assert backend.is_allowed("path:/api/run:user:alice", 5) is False
        # bob 独立计数
        assert backend.is_allowed("path:/api/run:user:bob", 5) is True

    def test_is_always_healthy(self):
        from lvyan.api.rate_limit import InMemoryBackend

        backend = InMemoryBackend()
        assert backend.is_healthy() is True

    def test_gc_removes_stale_counters(self):
        from lvyan.api.rate_limit import InMemoryBackend

        backend = InMemoryBackend()
        backend.is_allowed("stale_key", 100)
        # 强制 GC 间隔到期
        backend._last_gc = time.monotonic() - 400
        # 清空时间戳使其 stale
        backend._counters["stale_key"]._timestamps = []
        backend._gc()
        assert "stale_key" not in backend._counters


class TestRedisBackendFallback:
    """Redis 后端不可用时的行为测试。"""

    def test_invalid_url_marks_unhealthy(self):
        """无效 Redis URL → 后端标记为不健康。"""
        from lvyan.api.rate_limit import RedisBackend

        backend = RedisBackend("redis://invalid-host-that-does-not-exist:9999")
        assert backend.is_healthy() is False

    def test_unhealthy_backend_allows_request(self):
        """不健康的后端 → fail-open，允许请求通过。"""
        from lvyan.api.rate_limit import RedisBackend

        backend = RedisBackend("redis://invalid-host:9999")
        # fail-open
        assert backend.is_allowed("test:key", 5) is True


class TestMiddlewareKeyStrategy:
    """验证限流键的用户/IP 策略。"""

    def test_authenticated_user_uses_user_id(self, monkeypatch):
        """已认证用户（trusted_proxy 模式 + X-User-ID）使用 user_id 作为限流键。"""
        from lvyan.api.rate_limit import RateLimitMiddleware
        from unittest.mock import MagicMock
        import importlib

        # 启用认证 + trusted_proxy 模式，让 _resolve_user_id 解析 X-User-ID
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
        # X-User-ID 仅在直连来源位于 TRUSTED_PROXIES 白名单时可信
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.1.1")
        from lvyan.api import auth as auth_mod

        importlib.reload(auth_mod)

        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)

        request = MagicMock()
        request.state.user_id = "anonymous"
        request.headers = {"x-user-id": "user_123"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        key = middleware._get_client_key(request)
        assert key == "user:user_123"

    def test_anonymous_uses_forwarded_ip_only_from_trusted_proxy(self, monkeypatch):
        """匿名用户仅在来源是可信代理时才使用 X-Forwarded-For 最右段 IP。"""
        from lvyan.api.rate_limit import RateLimitMiddleware
        from unittest.mock import MagicMock

        monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")

        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)

        request = MagicMock()
        # 客户端伪造最左段 10.0.0.5，可信代理追加真实来源 192.168.1.1
        request.headers = {"x-forwarded-for": "10.0.0.5, 192.168.1.1"}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        key = middleware._get_client_key(request)
        assert key == "ip:192.168.1.1"

    def test_anonymous_ignores_forwarded_ip_from_untrusted(self, monkeypatch):
        """P0-4：非可信代理来源时忽略 X-Forwarded-For，使用直连 IP。"""
        from lvyan.api.rate_limit import RateLimitMiddleware
        from unittest.mock import MagicMock

        monkeypatch.setenv("TRUSTED_PROXIES", "")

        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)

        request = MagicMock()
        request.headers = {"x-forwarded-for": "10.0.0.5"}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        key = middleware._get_client_key(request)
        assert key == "ip:127.0.0.1"

    def test_no_state_uses_client_ip(self):
        """无 state 属性时使用客户端直连 IP。"""
        from lvyan.api.rate_limit import RateLimitMiddleware
        from unittest.mock import MagicMock

        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)

        request = MagicMock(spec=["url", "method", "headers", "client"])
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "203.0.113.42"

        key = middleware._get_client_key(request)
        assert key == "ip:203.0.113.42"
