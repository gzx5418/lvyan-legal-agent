"""策略守卫：迭代预算、成本预算与循环失控检测。

在关键节点（如 ``parallel_retrieval`` 重检索前、``composer`` 输出前）调用
``enforce_policies(state)``，若违反任何策略则抛出 :class:`PolicyViolationError`
中断图执行，避免无界迭代 / 成本失控。

成本估算说明
------------
当前无真实成本追踪，``check_cost_budget`` 用 ``state.iteration * 0.5`` 作为
占位估算（每次迭代约 0.5 USD）。待接入模型网关的 token 计费后，应替换为
真实累计成本字段。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from lvyan.config import settings

__all__ = [
    "PolicyViolationError",
    "check_retrieval_budget",
    "check_cost_budget",
    "detect_loop",
    "enforce_policies",
]


class PolicyViolationError(RuntimeError):
    """策略守卫违反异常。

    ``kind`` 标识违反的策略类型（``retrieval_budget`` / ``cost_budget`` / ``loop``），
    便于上层捕获后做差异化处理（如回退到降级输出 vs 直接终止）。
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(f"[{kind}] {message}")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def check_retrieval_budget(state: Any) -> bool:
    """检查是否仍有检索预算。

    返回 ``state.iteration < settings.MAX_RETRIEVAL_ITERATIONS``。
    ``True`` 表示可继续检索，``False`` 表示已达上限。
    """
    iteration = _get(state, "iteration", 0)
    return iteration < settings.max_retrieval_iterations


def check_cost_budget(state: Any) -> bool:
    """检查累计成本是否超出预算。

    用 ``state.iteration * 0.5`` 估算成本（占位）。返回 ``True`` 表示在预算内，
    ``False`` 表示超支。真实成本追踪接入后应替换此估算。
    """
    iteration = _get(state, "iteration", 0)
    estimated_cost = iteration * 0.5
    return estimated_cost <= settings.max_cost_budget_usd


def detect_loop(state: Any) -> bool:
    """检测检索是否循环失控。

    若 ``retrieval_queries`` 中存在相同 ``query_text`` 重复出现 >= 3 次，返回 ``True``
    （检测到循环），否则 ``False``。
    """
    queries = _get(state, "retrieval_queries", []) or []
    counter: Counter[str] = Counter()
    for q in queries:
        text = _get(q, "query_text", None)
        if text:
            counter[text] += 1
    return any(count >= 3 for count in counter.values())


def enforce_policies(state: Any) -> None:
    """组合策略检查，违反时抛出 :class:`PolicyViolationError`。

    检查顺序：循环失控 → 检索预算 → 成本预算。任一违反即抛出，后续检查不再执行。
    """
    if detect_loop(state):
        raise PolicyViolationError("loop", "检测到检索循环失控：相同 query_text 重复 >= 3 次")
    if not check_retrieval_budget(state):
        raise PolicyViolationError(
            "retrieval_budget",
            f"已达最大检索迭代次数 {settings.max_retrieval_iterations}",
        )
    if not check_cost_budget(state):
        raise PolicyViolationError(
            "cost_budget",
            f"估算成本超出预算 {settings.max_cost_budget_usd} USD",
        )
