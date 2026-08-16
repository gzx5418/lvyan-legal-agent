"""策略守卫：迭代预算、成本预算与循环失控检测。

接线说明（勿再回到"纸面防线"状态）：
- ``parallel_retrieval``（retrieve_statutes.py）入口调用
  :func:`enforce_retrieval_guards`（循环失控 + 成本预算），违反时该节点
  降级返回空检索结果并记 warning，不中断 run。
- 检索**迭代**预算由独立的 ``retrieval_iteration`` 控制，不再与
  ``reasoner_iteration`` 共享。
- :func:`enforce_policies` 保留为完整检查入口，供测试与未来接入
  composer 输出前的场景使用。

成本估算说明
------------
P0-10 后：``check_cost_budget`` 优先读取 CostTracker 中该 thread 的真实累计
成本（LLM 成功调用的 token 用量按单价表折算 USD）；无 thread_id 或尚无成本
记录时回退到 ``iteration * 0.5`` 占位估算（多数 run 的 iteration 为 0，
该回退仅作兜底，实际约束依赖真实成本记录）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from lvyan.common.helpers import get_compat_counter, get_value as _get
from lvyan.config import settings

__all__ = [
    "PolicyViolationError",
    "check_retrieval_budget",
    "check_cost_budget",
    "detect_loop",
    "enforce_policies",
    "enforce_retrieval_guards",
]


class PolicyViolationError(RuntimeError):
    """策略守卫违反异常。

    ``kind`` 标识违反的策略类型（``retrieval_budget`` / ``cost_budget`` / ``loop``），
    便于上层捕获后做差异化处理（如回退到降级输出 vs 直接终止）。
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(f"[{kind}] {message}")


def check_retrieval_budget(state: Any) -> bool:
    """检查是否仍有检索预算。

    返回 ``state.retrieval_iteration < settings.MAX_RETRIEVAL_ITERATIONS``。
    ``True`` 表示可继续检索，``False`` 表示已达上限。
    """
    retrieval_iteration = get_compat_counter(state, "retrieval_iteration")
    return retrieval_iteration < settings.max_retrieval_iterations


def check_cost_budget(state: Any) -> bool:
    """检查累计成本是否超出预算。

    P0-10：优先使用 CostTracker 中该 thread 的真实累计成本（LLM 成功调用
    的 token 用量按单价表折算 USD）；无 ``thread_id`` 或尚无成本记录时回退
    到按迭代次数的占位估算（``iteration * 0.5``）。返回 ``True`` 表示在
    预算内，``False`` 表示超支。
    """
    thread_id = _get(state, "thread_id", None)
    if thread_id:
        try:
            from lvyan.observability.tracing import get_cost_summary

            real_cost = get_cost_summary(thread_id).total_cost
            if real_cost > 0:
                return real_cost <= settings.max_cost_budget_usd
        except Exception:  # noqa: BLE001 - 成本读取失败回退占位估算
            pass
    reasoner_iteration = int(_get(state, "reasoner_iteration", 0) or 0)
    retrieval_iteration = int(_get(state, "retrieval_iteration", 0) or 0)
    # 旧 checkpoint 只有 iteration；仅在两个新字段都缺失/为零时回退旧值。
    legacy_iteration = int(_get(state, "iteration", 0) or 0)
    total_iterations = reasoner_iteration + retrieval_iteration
    if total_iterations == 0:
        total_iterations = legacy_iteration
    estimated_cost = total_iterations * 0.5
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


def enforce_retrieval_guards(state: Any) -> None:
    """检索入口守卫：循环失控 + 成本预算。

    与 :func:`enforce_policies` 的区别：**不**检查检索迭代预算——该预算
    由 ``routing.route_after_citation`` 使用独立的 ``retrieval_iteration``
    控制，避免同一次重检索在路由与入口重复扣减。

    违反时抛 :class:`PolicyViolationError`（``loop`` / ``cost_budget``），
    调用方（parallel_retrieval）捕获后降级返回空结果，不中断 run。
    """
    if detect_loop(state):
        raise PolicyViolationError("loop", "检测到检索循环失控：相同 query_text 重复 >= 3 次")
    if not check_cost_budget(state):
        raise PolicyViolationError(
            "cost_budget",
            f"估算成本超出预算 {settings.max_cost_budget_usd} USD",
        )


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
