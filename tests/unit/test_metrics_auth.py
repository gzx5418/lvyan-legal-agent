"""P0-12 修复：/metrics Bearer 校验必须使用常量时间比较，避免时序侧信道。

回归覆盖：
  正确 token 通过；错误 token 被拒；空 token 被拒。
  核心断言：校验函数存在且使用 hmac.compare_digest（而非 !=）。
"""

from __future__ import annotations

import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lvyan.observability import metrics


def _setup_metrics_app(monkeypatch, token: str = "secret-token-123") -> TestClient:
    """注册 /metrics 端点（mock prometheus 可用），返回 TestClient。

    prometheus_client 在测试环境通常未安装，故 _PROM_AVAILABLE=False 且
    generate_latest / CONTENT_TYPE_LATEST 未定义。这里同时 mock 这两项，
    使端点在 token 校验通过后能正常返回 200 而非 NameError。
    """
    monkeypatch.setattr(metrics, "_PROM_AVAILABLE", True)
    monkeypatch.setattr(
        metrics, "generate_latest", lambda: b"# mock metrics\n", raising=False
    )
    monkeypatch.setattr(
        metrics, "CONTENT_TYPE_LATEST", "text/plain; version=0.0.4", raising=False
    )
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("METRICS_AUTH_TOKEN", token)

    app = FastAPI()
    metrics.register_metrics_endpoint(app)
    return TestClient(app)


def test_metrics_bearer_check_uses_constant_time_compare():
    """register_metrics_endpoint 内的 token 校验必须使用 hmac.compare_digest。"""
    from lvyan.observability import metrics

    source = inspect.getsource(metrics.register_metrics_endpoint)
    assert "compare_digest" in source, (
        "/metrics Bearer 校验应使用 hmac.compare_digest 防止时序侧信道攻击，"
        "当前实现仍用 != 或 startswith 做非常量时间比较"
    )
    assert "import hmac" in source or "hmac.compare_digest" in source, (
        "应导入 hmac 模块并调用 compare_digest"
    )


def test_metrics_correct_token_passes(monkeypatch):
    """正确 Bearer token 应返回 200。"""
    client = _setup_metrics_app(monkeypatch, "secret-token-123")

    resp = client.get("/metrics", headers={"Authorization": "Bearer secret-token-123"})
    assert resp.status_code != 403, "正确 token 不应被拒绝"


def test_metrics_wrong_token_rejected(monkeypatch):
    """错误 Bearer token 应返回 403。"""
    client = _setup_metrics_app(monkeypatch, "secret-token-123")

    resp = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 403, "错误 token 应被拒绝"


def test_metrics_missing_token_rejected(monkeypatch):
    """无 Authorization 头应返回 403。"""
    client = _setup_metrics_app(monkeypatch, "secret-token-123")

    resp = client.get("/metrics")
    assert resp.status_code == 403, "无 token 应被拒绝"
