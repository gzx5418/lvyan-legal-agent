"""工具层统一返回模型基类。

所有标准工具的返回模型均继承 ``ToolResult``，确保可序列化为 JSON、
携带执行时间戳与错误信息，便于上层节点统一处理与日志记录。
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


class ToolResult(BaseModel):
    """所有工具返回模型的统一基类。

    子类继承本基类后只需声明自身业务字段，``tool_name`` / ``success`` /
    ``error`` / ``executed_at`` 由基类提供，避免在每个子模型重复声明。
    """

    tool_name: str = Field(description="工具名称，便于日志追溯")
    success: bool = Field(default=True, description="工具调用是否成功")
    error: str | None = Field(default=None, description="失败时的错误信息")
    executed_at: datetime = Field(default_factory=_now_utc, description="调用时间（UTC）")


__all__ = ["ToolResult"]
