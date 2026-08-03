"""状态图层：LangGraph 状态图构建、状态定义、条件路由与策略守卫。

公共导出：
- :func:`build_graph`：构建编译后的图（MemorySaver checkpoint）。
- :func:`build_graph_with_postgres`：构建编译后的图（PostgresSaver checkpoint，
  失败回退 MemorySaver）。
- :class:`GraphState`：与 ``CaseState`` 对齐的 LangGraph State schema。
- 条件路由函数 :func:`route_after_missing_fact` / :func:`route_after_citation` /
  :func:`route_after_critic` / :func:`route_by_complexity`。
- 策略守卫 :func:`enforce_policies` / :class:`PolicyViolationError` 等。
"""

from __future__ import annotations

from .builder import NODE_NAMES, build_graph, build_graph_with_postgres, build_graph_with_postgres_async
from .policies import (
    PolicyViolationError,
    check_cost_budget,
    check_retrieval_budget,
    detect_loop,
    enforce_policies,
)
from .routing import (
    route_after_citation,
    route_after_critic,
    route_after_missing_fact,
    route_by_complexity,
)
from .state import GraphState

__all__ = [
    # builder
    "build_graph",
    "build_graph_with_postgres",
    "build_graph_with_postgres_async",
    "NODE_NAMES",
    # state
    "GraphState",
    # routing
    "route_after_missing_fact",
    "route_after_citation",
    "route_after_critic",
    "route_by_complexity",
    # policies
    "PolicyViolationError",
    "check_retrieval_budget",
    "check_cost_budget",
    "detect_loop",
    "enforce_policies",
]
