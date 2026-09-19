"""LangGraph checkpoint 表的行级安全策略。

单一事实源：策略 SQL 统一维护在 ``migrations/010_checkpoint_rls.sql``。
- Docker 首次初始化：postgres 的 ``docker-entrypoint-initdb.d`` 直接执行该文件；
- 应用运行时：本模块在 ``AsyncPostgresSaver.setup()`` 建表后**加载同一文件**执行。

两处执行的是字节级相同的 SQL，杜绝"迁移文件与运行时代码各自维护一份策略
文本"的漂移风险（策略语义变更只需改 010 一个文件）。
"""

from __future__ import annotations

from typing import Any

from lvyan.config import AGENT_DIR

__all__ = ["ensure_checkpoint_rls", "ensure_checkpoint_rls_sync"]

_CHECKPOINT_RLS_MIGRATION = AGENT_DIR / "migrations" / "010_checkpoint_rls.sql"


def _load_checkpoint_rls_sql() -> str:
    """读取 checkpoint RLS 的唯一策略 SQL（DO 块，单语句）。

    文件缺失属于部署损坏：fail-closed 抛错，禁止静默以未隔离状态运行。
    """
    if not _CHECKPOINT_RLS_MIGRATION.is_file():
        raise RuntimeError(
            f"checkpoint RLS 迁移文件缺失：{_CHECKPOINT_RLS_MIGRATION}，"
            "拒绝在无租户隔离的情况下安装 checkpoint 访问"
        )
    return _CHECKPOINT_RLS_MIGRATION.read_text(encoding="utf-8")


async def ensure_checkpoint_rls(conn: Any) -> None:
    """在 checkpointer 建表后为全部 checkpoint 表安装租户 RLS 策略。

    LangGraph 在应用首次启动时才创建 checkpoint 表，因此这一步不能只依赖
    PostgreSQL 初始化目录中的静态迁移；必须在 ``AsyncPostgresSaver.setup()``
    成功后执行同一份策略 SQL（见模块 docstring）。缺少 ``agent_threads`` 或
    无法安装策略时让异常向上传播，避免 ``RLS_ENFORCED=true`` 的生产实例
    静默以未隔离状态运行。
    """
    await conn.execute(_load_checkpoint_rls_sql())


def ensure_checkpoint_rls_sync(conn: Any) -> None:
    """同步 PostgresSaver 使用的 RLS 安装入口。"""
    conn.execute(_load_checkpoint_rls_sql())
