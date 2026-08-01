"""M3：API 认证与租户隔离框架（移除未验签 JWT 信任路径）。

认证模式
--------
1. ``AUTH_ENABLED=false``（默认）：
   - 匿名本地开发模式；:func:`get_current_user_id` 直接返回 ``"anonymous"``，
     不读取任何请求头。

2. ``AUTH_ENABLED=true`` 且请求携带 ``X-User-ID``：
   - **仅用于受信任的 API Gateway / OIDC Proxy 模式**。
   - 部署时网关必须先剥离客户端传入的原始 ``X-User-ID``，再注入可信身份；
     服务不得在未经过网关的情况下直接暴露此端口（见部署文档）。

3. ``AUTH_ENABLED=true`` 且请求携带 ``Authorization: Bearer <jwt>``：
   - 仅当 ``JWT_VERIFY_IN_PROCESS=true`` 时才会被接受。
   - 必须验证签名、``exp``、``nbf``、``iss``、``aud``；任一校验失败返回 401。
   - ``JWT_VERIFY_IN_PROCESS=false`` 时，本服务**不再信任未验签 JWT**，
     直接返回 401，并提示当前部署只接受可信网关注入的身份头。

设计要点
--------
- 不再保留「解码 JWT payload 后直接读取 sub」的旁路。所有信任都必须通过
  显式开启的进程内验签或可信网关注入。
- ``PyJWT`` 缺失时，进程内验签路径直接 503，不会回退到不安全的解析。
- 与 :class:`CaseMemory.register` 协议：``register`` 在写入索引时同步记录
  ``user_id`` 字段，:func:`assert_thread_owner` 据此判定归属。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Header, HTTPException, Request

_logger = logging.getLogger("lvyan.api.auth")

__all__ = [
    "is_auth_enabled",
    "get_current_user_id",
    "assert_thread_owner",
    "assert_run_owner",
    "ANONYMOUS_USER",
]

ANONYMOUS_USER = "anonymous"


def is_auth_enabled() -> bool:
    """是否启用认证（默认 false，单租户本地开发零依赖）。

    读取顺序：环境变量 ``AUTH_ENABLED`` 实时读取 → ``settings.auth_enabled``。
    环境变量优先使测试可通过 ``monkeypatch.setenv`` 注入。
    """
    import os

    raw = os.getenv("AUTH_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    from lvyan.config import settings

    return bool(settings.auth_enabled)


def _auth_settings():
    """获取认证相关配置（环境变量优先于 settings 单例，便于测试注入）。

    返回 dict，包含 ``jwt_verify_in_process`` / ``jwt_issuer`` / ``jwt_audience``
    / ``jwt_jwks_url`` / ``jwt_algorithms``。
    """
    import os

    def _get(name: str, default: str) -> str:
        return os.getenv(name, default)

    def _get_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    from lvyan.config import settings

    return {
        "jwt_verify_in_process": _get_bool("JWT_VERIFY_IN_PROCESS", settings.jwt_verify_in_process),
        "jwt_issuer": _get("JWT_ISSUER", settings.jwt_issuer),
        "jwt_audience": _get("JWT_AUDIENCE", settings.jwt_audience),
        "jwt_jwks_url": _get("JWT_JWKS_URL", settings.jwt_jwks_url),
        "jwt_algorithms": _get("JWT_ALGORITHMS", settings.jwt_algorithms),
        # 三-1：认证模式。jwt=只接受验签 JWT；trusted_proxy=只接受网关注入的
        # X-User-ID；auto（默认）=兼容旧逻辑（X-User-ID 优先），仅用于开发。
        "auth_mode": _get("AUTH_MODE", "auto").strip().lower(),
    }


def _verify_jwt_and_extract_sub(authorization: str) -> str:
    """验证 Bearer JWT 并返回 ``sub``。

    校验项：签名、``exp``、``nbf``、``iss``（若配置）、``aud``（若配置）、
    算法白名单（禁止 ``none``）。任一失败抛 :class:`HTTPException(401)`。

    ``PyJWT`` 未安装或 ``JWT_VERIFY_IN_PROCESS=false`` 时抛
    :class:`HTTPException(401)` —— 不会回退到未验签的 payload 解析。
    """
    cfg = _auth_settings()

    if not cfg["jwt_verify_in_process"]:
        raise HTTPException(
            status_code=401,
            detail=(
                "Bearer JWT 未被信任：本部署未开启进程内验签"
                "（JWT_VERIFY_IN_PROCESS=false）。"
                "请通过可信 API Gateway 注入 X-User-ID，"
                "或让运维开启 JWT_VERIFY_IN_PROCESS=true 并配置 JWKS。"
            ),
        )

    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token 为空")

    try:
        import jwt  # type: ignore[import-untyped]
    except ImportError as exc:
        _logger.error("PyJWT 未安装但 JWT_VERIFY_IN_PROCESS=true：%s", exc)
        raise HTTPException(
            status_code=503,
            detail="服务端未安装 JWT 验签依赖（PyJWT）",
        ) from exc

    algorithms = [a.strip() for a in cfg["jwt_algorithms"].split(",") if a.strip()]
    if not algorithms or any(a.lower() == "none" for a in algorithms):
        raise HTTPException(
            status_code=500,
            detail="JWT_ALGORITHMS 配置非法：禁止使用 none",
        )

    if not cfg["jwt_jwks_url"]:
        raise HTTPException(
            status_code=500,
            detail="JWT_VERIFY_IN_PROCESS=true 但未配置 JWT_JWKS_URL",
        )
    if not cfg["jwt_issuer"] or not cfg["jwt_audience"]:
        raise HTTPException(
            status_code=500,
            detail=("JWT_VERIFY_IN_PROCESS=true 时必须同时配置 JWT_ISSUER " "和 JWT_AUDIENCE"),
        )

    try:
        signing_key = _fetch_signing_key(token, cfg["jwt_jwks_url"], algorithms)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 JWKS 解析/网络失败统一 401
        # P1-5：不向客户端泄露 JWKS 内部异常（可能暴露 URL/网络拓扑），
        # 统一返回 invalid_token，详情记录服务端日志。
        _logger.warning("JWKS 解析失败：%s", exc)
        raise HTTPException(
            status_code=401,
            detail="invalid_token",
        ) from exc

    decode_options: dict[str, Any] = {
        "verify_exp": True,
        "verify_nbf": True,
        "verify_signature": True,
        # PyJWT 仅在 claim 存在时才校验 exp / nbf；认证令牌必须携带这些
        # claim，不能允许省略后绕过时效控制。
        "require": ["exp", "nbf", "iss", "aud"],
    }
    decode_kwargs: dict[str, Any] = {
        "algorithms": algorithms,
        "issuer": cfg["jwt_issuer"],
        "audience": cfg["jwt_audience"],
    }

    try:
        payload = jwt.decode(
            token,
            signing_key,
            options=decode_options,
            **decode_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 PyJWT 抛出各种验签异常
        # 三-1：不向客户端泄露 PyJWT 内部异常原文（可能暴露密钥/算法细节），
        # 仅记录服务端日志，对外返回标准化错误码。
        _logger.info("JWT 校验失败：%s", exc)
        raise HTTPException(
            status_code=401,
            detail="invalid_token",
        ) from exc

    uid = payload.get("sub") or payload.get("uid") or payload.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="invalid_token")
    return str(uid)


def _fetch_signing_key(token: str, jwks_url: str, algorithms: list[str]) -> Any:
    """从 JWKS 获取用于验签的密钥。

    支持 RS256 / ES256 等非对称算法；HS256 这种对称算法需要 JWT_JWKS_URL
    返回共享密钥（生产不推荐）。优先使用 ``PyJWKClient``；失败回退直接读取
    JWKS JSON。
    """
    try:
        from jwt import PyJWKClient  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("PyJWT 缺少 PyJWKClient（请升级到 PyJWT>=2.6）") from exc

    jwk_client = PyJWKClient(jwks_url)
    return jwk_client.get_signing_key_from_jwt(token).key


def get_current_user_id(
    request: Request,
    x_user_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency：提取当前 user_id。

    三-1 改进：
    - 依据 ``AUTH_MODE`` 显式选择身份来源：
      * ``jwt``：只接受验签 Bearer JWT；
      * ``trusted_proxy``：只接受可信网关注入的 ``X-User-ID``；
      * ``auto``（默认，仅开发）：``X-User-ID`` 优先，其次 Bearer JWT。
    - 拒绝同时携带 ``X-User-ID`` 与 ``Bearer`` 的身份冲突请求（401），
      避免攻击者用 JWT 旁路覆盖网关身份或反之。
    - 本方法**不会**信任未验签的 JWT payload。
    """
    if not is_auth_enabled():
        return ANONYMOUS_USER

    cfg = _auth_settings()
    mode = cfg["auth_mode"]
    if mode not in {"jwt", "trusted_proxy", "auto"}:
        mode = "auto"

    # P1-5：生产模式禁止 AUTH_MODE=auto（仍会信任客户端 X-User-ID）
    from lvyan.config import is_production

    if is_production() and mode == "auto":
        raise HTTPException(
            status_code=500,
            detail=(
                "AUTH_MODE=auto is forbidden in production; "
                "set AUTH_MODE=jwt or AUTH_MODE=trusted_proxy"
            ),
        )

    has_xid = bool(x_user_id and x_user_id.strip())
    has_bearer = bool(authorization and authorization.lower().startswith("bearer "))

    # 身份冲突：同时出现两种身份来源 → 拒绝（防止互相覆盖）
    if has_xid and has_bearer:
        raise HTTPException(
            status_code=401,
            detail="identity_conflict",
        )

    if mode == "jwt":
        if not has_bearer:
            raise HTTPException(status_code=401, detail="missing_bearer_token")
        return _verify_jwt_and_extract_sub(authorization)

    if mode == "trusted_proxy":
        if not has_xid:
            raise HTTPException(
                status_code=401,
                detail="missing_trusted_identity",
            )
        return x_user_id.strip()

    # auto：X-User-ID 优先
    if has_xid:
        return x_user_id.strip()
    if has_bearer:
        return _verify_jwt_and_extract_sub(authorization)

    raise HTTPException(
        status_code=401,
        detail="missing_credentials",
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
        raise HTTPException(status_code=404, detail=f"thread {thread_id} 无记录")

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
