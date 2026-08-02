"""条件路由函数。

五个路由函数供 ``builder.py`` 的 ``add_conditional_edges`` 使用，也可独立调用：
- ``route_after_missing_fact``：缺失事实评估后，决定中断提问还是继续规划。
- ``route_after_citation``：引用校验后，决定重检索还是进入组装。
- ``route_after_critic``：Critic 评审后，决定回退 legal_reasoner 还是进入引用校验。
- ``route_after_output_guardrail``：输出守卫后，决定回退 composer 还是结束。
- ``route_by_complexity``：按案件复杂度返回输出模式，决定 composer 行为与
  是否跳过深度节点。

兼容性
------
LangGraph 用 TypedDict state 时，路由函数运行时收到的是 dict；但单元测试与
节点内部可能传入 :class:`CaseState`（Pydantic 模型）实例。本模块用 ``_get``
统一兼容 dict 与对象访问，子项（如 ``MissingFact`` / ``CitationAudit`` /
``CriticReport``）同样兼容 dict 与 Pydantic 实例，以保证在反序列化 checkpoint
后仍可工作。
"""

from __future__ import annotations

from typing import Any

from lvyan.config import settings

__all__ = [
    "route_after_missing_fact",
    "route_after_citation",
    "route_after_critic",
    "route_after_output_guardrail",
    "route_by_complexity",
]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def route_after_missing_fact(state: Any) -> str:
    """缺失事实评估后的路由。

    - 若 ``state.missing_facts`` 中存在 ``is_blocking=True`` 的项 → 返回 ``"ask_user"``，
      在 builder 中路由到 END（中断提问，等待用户补充后重启）。
    - 否则 → 返回 ``"continue"``，进入 ``planner``。
    """
    missing_facts = _get(state, "missing_facts", []) or []
    for item in missing_facts:
        if bool(_get(item, "is_blocking", False)):
            return "ask_user"
    return "continue"


def route_after_citation(state: Any) -> str:
    """引用校验后的路由。

    P1-9b 修复：引用校验通过后进入 ``output_guardrail``（而非 composer，
    因为 composer 已在 citation_verifier 之前执行）。

    - 若 ``state.citation_audit.passed`` 为 False 且
      ``state.iteration <`` 内部重检索上限 → 返回 ``"reretrieve"``，
      回到 ``parallel_retrieval`` 重检索。
    - 否则（通过 / 已达迭代上限）→ 返回 ``"output_guardrail"``，进入输出守卫。

    内部重检索上限与 ``citation_verifier`` 节点保持一致，取
    ``min(settings.max_retrieval_iterations, 2)``，避免节点已强制通过但
    路由仍回退重检索的不一致。
    """
    audit = _get(state, "citation_audit", None)
    if audit is None:
        return "output_guardrail"
    passed = _get(audit, "passed", True)
    iteration = _get(state, "iteration", 0)
    # 与 citation_verifier 节点内部限制保持一致：min(settings.max_retrieval_iterations, 2)
    max_iterations = min(settings.max_retrieval_iterations, 2)
    if not passed and iteration < max_iterations:
        return "reretrieve"
    return "output_guardrail"


def route_after_critic(state: Any) -> str:
    """Critic 评审后的路由。

    P1-9b 修复：评审通过后进入 ``composer`` 组装初稿（而非直接进
    citation_verifier），确保引用校验作用于最终输出文本。

    - 若 ``state.critic_report.passed`` 为 True → 返回 ``"composer"``，
      进入文书组装节点。
    - 若 ``state.critic_report.passed`` 为 False → 返回 ``"legal_reasoner"``，
      回退法律推理节点重试（iteration 已在 critic 节点中 +1）。

    注意：critic 节点在达到 ``MAX_LEGAL_REASONER_ITERATIONS`` 时会强制将
    ``passed`` 设为 True 并标记 ``risk_level="high"``，因此本路由函数无需
    重复判断迭代上限。
    """
    report = _get(state, "critic_report", None)
    if report is None:
        # 无 critic_report 时默认通过（首次运行或 critic 未执行）
        return "composer"
    passed = _get(report, "passed", True)
    if passed:
        return "composer"
    return "legal_reasoner"


def route_by_complexity(state: Any) -> str:
    """按案件复杂度返回输出模式。

    返回 ``state.complexity`` 本身（``"light"`` / ``"deep"`` / ``"document"``），
    供 composer 选择模板，也可用于决定是否跳过深度推理节点。
    缺省值为 ``"light"``。
    """
    complexity = _get(state, "complexity", "light")
    if complexity in ("light", "deep", "document"):
        return complexity
    return "light"


def route_after_output_guardrail(state: Any) -> str:
    """输出守卫后的路由。

    - 若 ``state.output_retry_needed`` 为 True → 返回 ``"composer"``，
      回退 ``composer`` 重新生成（受 ``MAX_OUTPUT_ITERATIONS`` 约束，
      上限判断由 ``output_guardrail`` 节点内部完成）。
    - 否则 → 返回 ``"legal_answer_finalizer"``，重建结构化输出后结束。

    ``output_retry_needed`` 缺省为 False（字段未初始化时按不回退处理）。
    """
    retry_needed = _get(state, "output_retry_needed", False)
    if bool(retry_needed):
        return "composer"
    return "legal_answer_finalizer"
