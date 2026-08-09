"""多租户隔离测试：验证用户 A 无法访问用户 B 的数据。

覆盖场景：
1. API 层：用户 A 创建 thread → 用户 B GET/DELETE → 404
2. API 层：用户 A 创建 run → 用户 B HITL/cancel → 404
3. 租户上下文设置：空 user_id 拒绝
4. 对越权资源统一返回 404（不暴露资源是否存在）
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 租户上下文单元测试
# ---------------------------------------------------------------------------
class TestTenantContext:
    """测试租户上下文设置的安全约束。"""

    def test_empty_user_id_rejected(self):
        """空 user_id 应抛出 ValueError。"""
        from lvyan.db.tenant_context import set_tenant_context

        class FakeConn:
            def execute(self, *args, **kwargs):
                pass

        with pytest.raises(ValueError, match="user_id 不能为空"):
            set_tenant_context(FakeConn(), "")

        with pytest.raises(ValueError, match="user_id 不能为空"):
            set_tenant_context(FakeConn(), "   ")

    @pytest.mark.asyncio
    async def test_empty_user_id_rejected_async(self):
        """异步版本同样拒绝空 user_id。"""
        from lvyan.db.tenant_context import set_tenant_context_async

        class FakeAsyncConn:
            async def execute(self, *args, **kwargs):
                pass

        with pytest.raises(ValueError, match="user_id 不能为空"):
            await set_tenant_context_async(FakeAsyncConn(), "")


# ---------------------------------------------------------------------------
# API 层隔离测试（使用 TestClient）
# ---------------------------------------------------------------------------
class TestAPITenantIsolation:
    """验证 API 层的跨租户隔离。

    这些测试需要一个配置了认证的 test app。
    使用 mock 数据库和 RLS 策略。
    """

    @pytest.fixture
    def app_with_auth(self):
        """创建启用认证的测试 app。"""
        import os

        os.environ.setdefault("AUTH_ENABLED", "true")
        os.environ.setdefault("AUTH_MODE", "trusted_proxy")
        os.environ.setdefault("RUNTIME_MODE", "development")
        os.environ.setdefault("CASE_VAULT_ALLOW_INSECURE", "true")

        from lvyan.api.server import create_app

        app = create_app()
        return app

    def test_thread_cross_user_get_returns_404(self, app_with_auth):
        """用户 A 的 thread，用户 B GET → 404。"""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_auth)

        # 用户 A 创建
        resp = client.post(
            "/api/agent/run",
            json={"question": "测试问题"},
            headers={"X-User-ID": "user_a"},
        )
        if resp.status_code == 200:
            thread_id = resp.json().get("thread_id")
            if thread_id:
                # 用户 B 尝试获取
                resp_b = client.get(
                    f"/api/agent/state/{thread_id}",
                    headers={"X-User-ID": "user_b"},
                )
                assert resp_b.status_code in (403, 404), (
                    f"跨租户访问应返回 403/404，实际: {resp_b.status_code}"
                )

    def test_thread_cross_user_delete_returns_404(self, app_with_auth):
        """用户 A 的 thread，用户 B DELETE → 404。"""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_auth)

        resp = client.post(
            "/api/agent/run",
            json={"question": "测试"},
            headers={"X-User-ID": "user_a"},
        )
        if resp.status_code == 200:
            thread_id = resp.json().get("thread_id")
            if thread_id:
                resp_b = client.delete(
                    f"/api/agent/state/{thread_id}",
                    headers={"X-User-ID": "user_b"},
                )
                assert resp_b.status_code in (403, 404)

    def test_cross_tenant_hitl_rejected(self, app_with_auth):
        """用户 A 的 run 进入 HITL，用户 B 提交审批 → 拒绝。"""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_auth)

        # 创建 run（用户 A）
        resp = client.post(
            "/api/agent/run",
            json={"question": "测试 HITL"},
            headers={"X-User-ID": "user_a"},
        )
        if resp.status_code == 200:
            run_id = resp.json().get("run_id")
            if run_id:
                # 用户 B 尝试审批
                resp_b = client.post(
                    f"/api/agent/hitl/{run_id}",
                    json={"action": "approve", "payload": {}},
                    headers={"X-User-ID": "user_b"},
                )
                assert resp_b.status_code in (403, 404)


# ---------------------------------------------------------------------------
# 生产配置校验测试
# ---------------------------------------------------------------------------
class TestProductionConfigValidation:
    """验证生产模式强制安全配置。"""

    @pytest.fixture(autouse=True)
    def _base_production_env(self, monkeypatch):
        """所有 production 校验测试的基础环境变量。"""
        monkeypatch.setenv("RUNTIME_MODE", "production")
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        monkeypatch.setenv("CASE_VAULT_KEY", "a" * 64)
        monkeypatch.setenv("PERSISTENCE_REQUIRED", "true")
        # 满足全部 P2 要求的默认值
        monkeypatch.setenv("RLS_ENFORCED", "true")
        monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    def test_production_rls_not_enforced_fails(self, monkeypatch):
        """生产模式 + RLS_ENFORCED=false → 启动失败。"""
        monkeypatch.setenv("RLS_ENFORCED", "false")

        from lvyan.config import validate_runtime_config

        with pytest.raises(RuntimeError, match="RLS_ENFORCED"):
            validate_runtime_config()

    def test_production_memory_rate_limit_fails(self, monkeypatch):
        """生产模式 + RATE_LIMIT_BACKEND=memory → 启动失败。"""
        monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")

        from lvyan.config import validate_runtime_config

        with pytest.raises(RuntimeError, match="RATE_LIMIT_BACKEND"):
            validate_runtime_config()

    def test_production_no_redis_url_fails(self, monkeypatch):
        """生产模式 + redis 后端 + 无 REDIS_URL → 启动失败。"""
        monkeypatch.setenv("REDIS_URL", "")

        from lvyan.config import validate_runtime_config

        with pytest.raises(RuntimeError, match="REDIS_URL"):
            validate_runtime_config()

    def test_production_all_valid_passes(self, monkeypatch):
        """生产模式 + 所有必需配置 → 通过（不应抛异常）。"""
        from lvyan.config import validate_runtime_config

        validate_runtime_config()
