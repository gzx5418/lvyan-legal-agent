"""记忆层：短期 checkpoint、案件加密空间、用户长期偏好。

- :class:`ShortTermMemory`：按 ``thread_id`` 保存会话状态（CaseState）。
- :class:`CaseVault`：案件材料加密存储 + 跨 thread 隔离 + TTL。
- :class:`UserPreferences` / :class:`UserPreference`：跨会话用户偏好（仅偏好，不含敏感信息）。
"""

from __future__ import annotations

from .case_vault import DEFAULT_TTL_SECONDS, CaseVault
from .case_workspace import (
    CaseWorkspaceStore,
    InMemoryCaseWorkspaceStore,
    PostgresCaseWorkspaceStore,
)
from .checkpoints import ShortTermMemory
from .user_preferences import UserPreference, UserPreferences, sanitize_preference

__all__ = [
    "ShortTermMemory",
    "CaseVault",
    "CaseWorkspaceStore",
    "InMemoryCaseWorkspaceStore",
    "PostgresCaseWorkspaceStore",
    "DEFAULT_TTL_SECONDS",
    "UserPreferences",
    "UserPreference",
    "sanitize_preference",
]
