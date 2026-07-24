"""指标采集桩：内存级计数器，记录节点/工具调用次数与耗时。

当前为轻量内存实现，满足可观测性最小可用需求；后续可替换为
OpenTelemetry Metrics / Prometheus exporter 而无需改动调用方。
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["MetricsRecorder"]


class MetricsRecorder:
    """线程安全的内存指标记录器。

    记录节点执行次数/耗时、工具调用次数/耗时/成功率。``snapshot()`` 返回
    当前累计指标的浅拷贝，便于上报或断言。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, dict[str, float]] = {}
        self._tools: dict[str, dict[str, float]] = {}

    def record_node(self, node_name: str, duration_ms: float) -> None:
        """记录一次节点执行。"""
        with self._lock:
            entry = self._nodes.setdefault(node_name, {"count": 0, "total_ms": 0.0})
            entry["count"] += 1
            entry["total_ms"] += float(duration_ms)

    def record_tool_call(
        self, tool_name: str, duration_ms: float, success: bool = True
    ) -> None:
        """记录一次工具调用。"""
        with self._lock:
            entry = self._tools.setdefault(
                tool_name, {"count": 0, "total_ms": 0.0, "success": 0, "failure": 0}
            )
            entry["count"] += 1
            entry["total_ms"] += float(duration_ms)
            if success:
                entry["success"] += 1
            else:
                entry["failure"] += 1

    def snapshot(self) -> dict[str, Any]:
        """返回当前指标的浅拷贝。"""
        with self._lock:
            return {
                "nodes": {k: dict(v) for k, v in self._nodes.items()},
                "tools": {k: dict(v) for k, v in self._tools.items()},
            }

    def reset(self) -> None:
        """清空全部指标。"""
        with self._lock:
            self._nodes.clear()
            self._tools.clear()
