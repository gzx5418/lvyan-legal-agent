"""请求 ID 注入中间件。

为每个入站请求生成唯一 request_id（或接受上游代理传入的 X-Request-ID），
注入到：
1. structlog 上下文变量（所有日志自动携带 request_id）
2. Response Header（X-Request-ID，便于客户端关联）
3. request.state（下游代码可通过 request.state.request_id 获取）
"""

from __future__ import annotations

import re
import uuid
import logging
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_logger = logging.getLogger("lvyan.observability.request_id")

__all__ = ["RequestIDMiddleware"]

_HEADER_NAME = "X-Request-ID"

# 客户端传入的 request_id 合法格式：长度 ≤ 128，字符白名单 [A-Za-z0-9_\-.]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/传播唯一标识符。"""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # 优先接受上游代理传入的 request_id（如 nginx/cloudflare）；
        # 不合法（超长 / 含白名单外字符）则忽略并生成新的，防止日志注入与高基数
        request_id = request.headers.get(_HEADER_NAME)
        if not request_id or not _REQUEST_ID_PATTERN.match(request_id):
            if request_id:
                _logger.warning(
                    "忽略不合法的 %s（长度 %d，含白名单外字符或超限），已生成新 ID",
                    _HEADER_NAME,
                    len(request_id),
                )
            request_id = uuid.uuid4().hex[:16]

        # 注入到 request.state
        request.state.request_id = request_id

        # 注入到 structlog 上下文变量（若 structlog 可用）
        try:
            import structlog

            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(
                request_id=request_id,
                path=request.url.path,
                method=request.method,
            )
            # 同时绑定 user_id（如果 auth 中间件已设置）
            user_id = getattr(request.state, "user_id", None)
            if user_id:
                structlog.contextvars.bind_contextvars(user_id=user_id)
        except ImportError:
            pass

        response = await call_next(request)
        response.headers[_HEADER_NAME] = request_id
        return response
