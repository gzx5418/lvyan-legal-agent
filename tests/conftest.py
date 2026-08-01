"""测试根 conftest。

在收集期（任何被测模块 import 之前）锁定开发默认环境，避免：

- 生产部署变量（RUNTIME_MODE=production / PERSISTENCE_REQUIRED=true）意外残留在
  shell 环境中，导致 ``lvyan.api.server`` 模块级 ``app = create_app()`` 在
  import 时因持久化强制而抛 PersistenceUnavailable，使大批测试无法 import。
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
os.environ["PERSISTENCE_REQUIRED"] = os.environ.get(
    "LVYAN_TEST_PERSISTENCE_REQUIRED", "false"
)

# 关键：在「开发默认环境」下立即 import lvyan.config，使 ``settings`` 单例在此刻
# 冻结为 development / persistence_required=False。否则 ``settings`` 会在第一个
# 被测模块 import 时才惰性构建——若该测试用 monkeypatch 把 RUNTIME_MODE 临时改
# 成 production，单例就会被永久冻结为 production，污染后续所有依赖
# ``settings.runtime_mode`` 的判断（如 build_graph 的回退决策）。
import lvyan.config  # noqa: F401,E402
