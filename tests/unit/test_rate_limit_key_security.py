"""P0-4 / P0-5 修复：限流键解析的安全约束。

回归覆盖：
  P0-4: X-Forwarded-For 必须仅在请求来自可信代理时才被采信，
        否则一律使用直连 IP，避免客户端伪造头绕过限流。
  P0-5: 已认证用户的 user_id 应能从依赖注入写入的 request.state.user_id
        读取；认证中间件需负责把 user_id 写入 state，否则限流退化为 IP 维度。
"""

from __future__ import annotations

from starlette.requests import Request

from lvyan.api import rate_limit


def _make_request(
    client_host: str = "203.0.113.1",
    x_forwarded_for: str | None = None,
    user_id: str | None = None,
) -> Request:
    """构造带可控 headers / state 的 Starlette Request。"""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/agent/run",
        "headers": [],
        "client": (client_host, 12345),
        "query_params": {},
        "server": ("test", 80),
        "scheme": "http",
    }
    headers = []
    if x_forwarded_for is not None:
        headers.append((b"x-forwarded-for", x_forwarded_for.encode()))
    scope["headers"] = headers
    req = Request(scope)
    if user_id is not None:
        req.state.user_id = user_id
    return req


def _build_middleware(monkeypatch, trusted_proxies: str = "", auth_enabled: bool = False):
    """构造启用的 RateLimitMiddleware（绕过 backend 健康检查）。"""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("TRUSTED_PROXIES", trusted_proxies)
    if auth_enabled:
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    else:
        monkeypatch.setenv("AUTH_ENABLED", "false")
    # auth.is_auth_enabled 和 get_current_user_id 在模块加载时可能已缓存，
    # 这里依赖它们内部读取环境变量；重新导入确保生效。
    import importlib

    from lvyan.api import auth as auth_mod

    importlib.reload(auth_mod)
    mw = rate_limit.RateLimitMiddleware(app=lambda req: None)
    return mw


def test_client_ip_ignored_when_not_from_trusted_proxy(monkeypatch):
    """P0-4：非可信代理来源时，X-Forwarded-For 头必须被忽略。"""
    mw = _build_middleware(monkeypatch, trusted_proxies="")
    req = _make_request(
        client_host="203.0.113.1",
        x_forwarded_for="1.1.1.1",
    )
    key = mw._get_client_key(req)
    assert key == "ip:203.0.113.1", "非可信代理来源时必须使用直连 IP，忽略 XFF"


def test_client_ip_trusted_when_from_trusted_proxy(monkeypatch):
    """P0-4：来源是可信代理时，X-Forwarded-For 最左侧 IP 应被采用。"""
    mw = _build_middleware(monkeypatch, trusted_proxies="10.0.0.1,10.0.0.2")
    req = _make_request(
        client_host="10.0.0.1",
        x_forwarded_for="198.51.100.7, 10.0.0.1",
    )
    key = mw._get_client_key(req)
    assert key == "ip:198.51.100.7", "可信代理来源时应取 XFF 最左侧客户端 IP"


def test_user_id_takes_priority_over_ip(monkeypatch):
    """P0-5：已认证 user_id（通过 trusted_proxy 注入）应优先按 user 维度限流。"""
    mw = _build_middleware(monkeypatch, trusted_proxies="", auth_enabled=True)
    req = _make_request(
        client_host="203.0.113.1",
        x_forwarded_for=None,
        user_id=None,
    )
    # 注入 X-User-ID 头（trusted_proxy 模式从该头读取）
    req.scope["headers"].append((b"x-user-id", b"user-abc"))
    key = mw._get_client_key(req)
    assert key == "user:user-abc", "认证用户应按 user_id 限流"


def test_anonymous_user_id_not_used(monkeypatch):
    """P0-5：认证未启用时退化为 IP 维度，不应用 anonymous。"""
    mw = _build_middleware(monkeypatch, trusted_proxies="", auth_enabled=False)
    req = _make_request(
        client_host="203.0.113.1",
    )
    key = mw._get_client_key(req)
    assert key == "ip:203.0.113.1", "认证未启用时应按 IP 限流"
