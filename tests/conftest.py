"""测试根 conftest。

在收集期（任何被测模块 import 之前）锁定开发默认环境，避免：

- 生产部署变量（RUNTIME_MODE=production / PERSISTENCE_REQUIRED=true）意外残留在
  shell 环境中，导致显式 ``create_app()`` 测试受外部环境污染。
- 单个测试内部仍可用 ``monkeypatch.setenv`` 临时切换到生产模式验证 P0-1。

本文件只在 collection 时执行一次 os.environ 清理，不改变运行时行为。
"""

from __future__ import annotations

import os

# 测试套件默认按「开发模式 + 不强制持久化」运行；PostgreSQL 不可达时回退
# MemorySaver，保证离线 CI / 本地无 PG 环境下用例可执行。
os.environ.setdefault("RUNTIME_MODE", "development")
os.environ.setdefault("PERSISTENCE_REQUIRED", "false")
# 若 shell 残留了生产变量，显式覆盖回开发默认（setdefault 不会覆盖已有值，
# 这里需要强制覆盖，故用直接赋值）。
os.environ["RUNTIME_MODE"] = os.environ.get("LVYAN_TEST_RUNTIME_MODE", "development")
os.environ["PERSISTENCE_REQUIRED"] = os.environ.get("LVYAN_TEST_PERSISTENCE_REQUIRED", "false")
# 测试环境不保存真实案件材料；显式允许开发期 base64 降级，避免显式
# ``create_app()`` 测试因缺少 CASE_VAULT_KEY 而失败。生产模式
# 始终忽略该开关，相关安全用例也会在各自测试中清理或覆盖该变量。
os.environ["CASE_VAULT_ALLOW_INSECURE"] = os.environ.get(
    "LVYAN_TEST_CASE_VAULT_ALLOW_INSECURE", "true"
)
# P2: 测试默认关闭认证 / 不强制 RLS / 使用内存限流，避免 shell 残留
# 生产配置导致 create_app() 模块级调用时 401 / 启动失败。
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RLS_ENFORCED", "false")
os.environ.setdefault("RATE_LIMIT_BACKEND", "memory")
os.environ["AUTH_ENABLED"] = os.environ.get("LVYAN_TEST_AUTH_ENABLED", "false")
os.environ["AUTH_MODE"] = os.environ.get("LVYAN_TEST_AUTH_MODE", "auto")
os.environ["RLS_ENFORCED"] = os.environ.get("LVYAN_TEST_RLS_ENFORCED", "false")
os.environ["RATE_LIMIT_BACKEND"] = os.environ.get("LVYAN_TEST_RATE_LIMIT_BACKEND", "memory")

# 关键：在「开发默认环境」下立即 import lvyan.config，使 ``settings`` 单例在此刻
# 冻结为 development / persistence_required=False。否则 ``settings`` 会在第一个
# 被测模块 import 时才惰性构建——若该测试用 monkeypatch 把 RUNTIME_MODE 临时改
# 成 production，单例就会被永久冻结为 production，污染后续所有依赖
# ``settings.runtime_mode`` 的判断（如 build_graph 的回退决策）。
import lvyan.config  # noqa: F401,E402
