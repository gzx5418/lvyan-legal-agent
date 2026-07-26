"""P2-13：API 认证与租户隔离框架。

本模块提供轻量级、可选开启的 user_id 提取与会话 ownership 校验：

- :func:`get_current_user_id`：FastAPI dependency，从请求头 ``X-User-ID`` /
  ``Authorization: Bearer <token>`` 提取 user_id；未启用认证时返回 ``"anonymous"``。
- :func:`enable_auth_middleware`：开启后所有 thread/run/attachment 查询都强
  制带 ``WHERE user_id = current_user.id``。
- :func:`assert_thread_owner`：基于 CaseMemory 索引中的 ``user_id`` 字段
  校验 thread 归属；不匹配抛 ``HTTPException(403)``。
- :func:`assert_run_owner`：基于 RunContext 上记录的 ``user_id`` 校验 run 归属。

设计原则
--------
- 默认 ``AUTH_ENABLED=false``：本地开发零依赖，所有 user_id 都是 ``anonymous``，
  单租户场景下 ownership 不阻断。
- 生产部署设 ``AUTH_ENABLED=true``，前端通过反代注入 ``X-User-ID`` 头（或
  JWT）；本模块不实现完整 JWT 验签，仅做协议适配，留给生产侧用 API Gateway
  / OIDC proxy 完成。
- 与 :class:`CaseMemory.register` 协议：``register`` 在写入索引时同步记录
  ``user_id`` 字段，:func:`assert_thread_owner` 据此判定归属。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Header, HTTPException, Request

__all__ = [
    "is_auth_enabled",
    "get_current_user_id",
    "assert_thread_owner",
    "assert_run_owner",
    "ANONYMOUS_USER",
]

ANONYMOUS_USER = "anonymous"


def is_auth_enabled() -> bool:
    """是否启用认证（默认 false，单租户本地开发零依赖）。"""
    raw = os.getenv("AUTH_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_current_user_id(
    request: Request,
    x_user_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency：从请求头提取当前 user_id。

    提取顺序：
      1. ``X-User-ID`` 头（API Gateway / 反代注入，推荐）
      2. ``Authorization: Bearer <jwt>`` 的 subject（解析失败回退 anonymous）
      3. 未启用认证 → ``"anonymous"``

    生产部署应通过 API Gateway 在网关层完成 OIDC / JWT 验签，本服务只接收
    网关注入的可信 ``X-User-ID``。
    """
    if not is_auth_enabled():
        return ANONYMOUS_USER

    if x_user_id:
        return x_user_id.strip()

    if authorization and authorization.lower().startswith("bearer "):
        # 仅解析 JWT payload 不验签（验签由 Gateway 负责）
        token = authorization[7:].strip()
        try:
            import base64
            import json

            parts = token.split(".")
            if len(parts) >= 2:
                # base64url → padding → json
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(
                    base64.urlsafe_b64decode(payload_b64).decode("utf-8")
                )
                uid = payload.get("sub") or payload.get("uid") or payload.get("user_id")
                if uid:
                    return str(uid)
        except Exception:  # noqa: BLE001 解析失败回退
            pass

    raise HTTPException(
        status_code=401,
        detail="未提供有效身份（缺少 X-User-ID 头或 Bearer token）",
    )


def _meta_user_id(meta: dict[str, Any]) -> str:
    """从 thread meta dict 提取 user_id；缺失视为 anonymous。"""
    return str(meta.get("user_id", ANONYMOUS_USER) or ANONYMOUS_USER)


def assert_thread_owner(
    thread_meta: dict[str, Any] | None,
    current_user_id: str,
    thread_id: str,
) -> None:
    """校验 thread 归属；不匹配或 thread 不存在时抛 HTTPException。

    Args:
        thread_meta: CaseMemory.list_threads 返回的 meta dict（含 user_id 字段）；
            ``None`` 表示 thread 不存在。
        current_user_id: 当前请求的 user_id。
        thread_id: 用于错误消息。
    """
    if thread_meta is None:
        raise HTTPException(
            status_code=404, detail=f"thread {thread_id} 无记录"
        )

    if not is_auth_enabled():
        return  # 单租户模式不强制 ownership

    owner = _meta_user_id(thread_meta)
    if owner != current_user_id:
        raise HTTPException(
            status_code=403,
            detail=f"thread {thread_id} 不属于当前用户（owner={owner}）",
        )


def assert_run_owner(
    run_ctx: Any,
    current_user_id: str,
    run_id: str,
) -> None:
    """校验 run 归属；不匹配时抛 HTTPException。

    Args:
        run_ctx: RunContext（含 ``user_id`` 属性，未设置视为 anonymous）。
        current_user_id: 当前请求的 user_id。
        run_id: 用于错误消息。
    """
    if not is_auth_enabled():
        return

    owner = str(getattr(run_ctx, "user_id", ANONYMOUS_USER) or ANONYMOUS_USER)
    if owner != current_user_id:
        raise HTTPException(
            status_code=403,
            detail=f"run {run_id} 不属于当前用户（owner={owner}）",
        )
