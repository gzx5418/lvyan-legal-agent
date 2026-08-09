"""Prometheus 指标定义与 /metrics 端点。

指标类别
--------
1. HTTP 请求：request_duration_seconds, request_total
2. LLM 调用：llm_call_duration_seconds, llm_call_total, llm_token_usage
3. 检索：retrieval_duration_seconds, retrieval_total
4. Agent 运行：agent_run_total, agent_run_duration_seconds
5. 系统健康：active_connections, rate_limit_hits_total

保护
----
/metrics 端点在生产环境通过 Bearer token 保护（METRICS_AUTH_TOKEN）。
"""

from __future__ import annotations

import os
import time
import logging
from functools import wraps
from typing import Any, Callable

_logger = logging.getLogger("lvyan.observability.metrics")

# 尝试导入 prometheus_client
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )

    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    _logger.debug("prometheus_client 未安装，指标收集禁用")


# ---------------------------------------------------------------------------
# 指标定义（仅在 prometheus_client 可用时创建）
# ---------------------------------------------------------------------------
if _PROM_AVAILABLE:
    # HTTP
    HTTP_REQUEST_DURATION = Histogram(
        "lvyan_http_request_duration_seconds",
        "HTTP 请求延迟分布",
        labelnames=["method", "path", "status_code"],
        buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    HTTP_REQUEST_TOTAL = Counter(
        "lvyan_http_requests_total",
        "HTTP 请求总数",
        labelnames=["method", "path", "status_code"],
    )

    # LLM
    LLM_CALL_DURATION = Histogram(
        "lvyan_llm_call_duration_seconds",
        "LLM API 调用延迟",
        labelnames=["model", "operation"],
        buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0],
    )
    LLM_CALL_TOTAL = Counter(
        "lvyan_llm_calls_total",
        "LLM API 调用总数",
        labelnames=["model", "operation", "status"],
    )
    LLM_TOKEN_USAGE = Counter(
        "lvyan_llm_tokens_total",
        "LLM token 使用量",
        labelnames=["model", "direction"],
    )

    # 检索
    RETRIEVAL_DURATION = Histogram(
        "lvyan_retrieval_duration_seconds",
        "检索操作延迟",
        labelnames=["source"],
        buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
    )
    RETRIEVAL_TOTAL = Counter(
        "lvyan_retrieval_operations_total",
        "检索操作总数",
        labelnames=["source", "status"],
    )

    # Agent
    AGENT_RUN_TOTAL = Counter(
        "lvyan_agent_runs_total",
        "Agent 运行总数",
        labelnames=["status"],
    )
    AGENT_RUN_DURATION = Histogram(
        "lvyan_agent_run_duration_seconds",
        "Agent 单次运行耗时",
        buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    )

    # 系统
    ACTIVE_CONNECTIONS = Gauge(
        "lvyan_active_connections",
        "当前活跃连接数",
    )
    RATE_LIMIT_HITS = Counter(
        "lvyan_rate_limit_hits_total",
        "速率限制触发次数",
        labelnames=["path"],
    )


# ---------------------------------------------------------------------------
# 指标辅助装饰器
# ---------------------------------------------------------------------------
def track_llm_call(model: str, operation: str) -> Callable:
    """LLM 调用跟踪装饰器。"""

    def decorator(func: Callable) -> Callable:
        if not _PROM_AVAILABLE:
            return func

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            status = "success"
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                duration = time.perf_counter() - start
                LLM_CALL_DURATION.labels(model=model, operation=operation).observe(duration)
                LLM_CALL_TOTAL.labels(
                    model=model, operation=operation, status=status
                ).inc()

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            status = "success"
            try:
                result = func(*args, **kwargs)
                return result
            except Exception:
                status = "error"
                raise
            finally:
                duration = time.perf_counter() - start
                LLM_CALL_DURATION.labels(model=model, operation=operation).observe(duration)
                LLM_CALL_TOTAL.labels(
                    model=model, operation=operation, status=status
                ).inc()

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def record_token_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """记录 LLM token 使用量。"""
    if not _PROM_AVAILABLE:
        return
    LLM_TOKEN_USAGE.labels(model=model, direction="input").inc(input_tokens)
    LLM_TOKEN_USAGE.labels(model=model, direction="output").inc(output_tokens)


# ---------------------------------------------------------------------------
# FastAPI 路由注册
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# MetricsRecorder: 轻量级 Agent 内部指标收集器（兼容旧接口）
# ---------------------------------------------------------------------------
class MetricsRecorder:
    """运行级别的节点/工具调用指标收集器。

    用于单次 Agent run 内部的性能追踪，与 Prometheus 全局指标互补。
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, float]] = {}
        self._tools: dict[str, dict[str, Any]] = {}

    def record_node(self, name: str, *, duration_ms: float) -> None:
        if name not in self._nodes:
            self._nodes[name] = {"count": 0, "total_ms": 0.0}
        self._nodes[name]["count"] += 1
        self._nodes[name]["total_ms"] += duration_ms

    def record_tool_call(
        self, name: str, *, duration_ms: float, success: bool
    ) -> None:
        if name not in self._tools:
            self._tools[name] = {"count": 0, "total_ms": 0.0, "errors": 0}
        self._tools[name]["count"] += 1
        self._tools[name]["total_ms"] += duration_ms
        if not success:
            self._tools[name]["errors"] += 1

    def snapshot(self) -> dict[str, Any]:
        return {"nodes": dict(self._nodes), "tools": dict(self._tools)}


def register_metrics_endpoint(app: Any) -> None:
    """注册 /metrics 端点到 FastAPI app。"""
    if not _PROM_AVAILABLE:
        _logger.info("prometheus_client 未安装，/metrics 端点未注册")
        return

    metrics_enabled = os.getenv("METRICS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not metrics_enabled:
        _logger.info("METRICS_ENABLED=false，/metrics 端点未注册")
        return

    from fastapi import Request
    from fastapi.responses import Response

    auth_token = os.getenv("METRICS_AUTH_TOKEN", "").strip()

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        # Token 保护（生产环境）
        if auth_token:
            auth_header = request.headers.get("authorization", "")
            if auth_header != f"Bearer {auth_token}":
                return Response(status_code=403, content="Forbidden")

        body = generate_latest()
        return Response(
            content=body,
            media_type=CONTENT_TYPE_LATEST,
        )

    _logger.info("/metrics 端点已注册")
