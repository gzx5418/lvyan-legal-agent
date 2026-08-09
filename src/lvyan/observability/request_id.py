"""请求 ID 注入中间件。

为每个入站请求生成唯一 request_id（或接受上游代理传入的 X-Request-ID），
注入到：
1. structlog 上下文变量（所有日志自动携带 request_id）
2. Response Header（X-Request-ID，便于客户端关联）
3. request.state（下游代码可通过 request.state.request_id 获取）
"""

from __future__ import annotations

import uuid
import logging
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_logger = logging.getLogger("lvyan.observability.request_id")

__all__ = ["RequestIDMiddleware"]

_HEADER_NAME = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/传播唯一标识符。"""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # 优先接受上游代理传入的 request_id（如 nginx/cloudflare）
        request_id = request.headers.get(_HEADER_NAME)
        if not request_id:
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
