"""缺失事实评估与执行计划节点。

本模块容纳两个节点函数：
- ``missing_fact_assessor``：评估关键事实是否缺失，决定是否中断向用户追问。
- ``planner``：基于完整事实生成执行计划（检索 / 推理 / 证据补全步骤）。

规则+模板实现，后续接入 LLM 增强判断。
"""

from __future__ import annotations

import re
from typing import Any

from lvyan.schemas import CaseState, PlanStep, RetrievalQuery

# 复用 triage 的案由识别与 fact_extractor 的缺失事实评估
from lvyan.nodes.fact_extractor import _assess_missing_facts, _get, _short_id
from lvyan.nodes.triage import _detect_case_type

__all__ = ["missing_fact_assessor", "planner"]


# ---------------------------------------------------------------------------
# 案由 → 推荐法规关键词
# ---------------------------------------------------------------------------
_CASE_TYPE_LAW_KEYWORDS: dict[str, list[str]] = {
    "劳动争议": ["劳动法", "劳动合同法", "劳动争议调解仲裁法"],
    "合同纠纷": ["民法典合同编", "合同法"],
    "侵权纠纷": ["民法典侵权责任编", "侵权责任法"],
    "婚姻家庭": ["民法典婚姻家庭编", "婚姻法"],
    "知识产权": ["专利法", "商标法", "著作权法"],
}

# 行为关键词（用于法条查询拼接）
_ACTION_KEYWORDS_FOR_QUERY: tuple[str, ...] = (
    "辞退", "解除", "违约", "赔偿", "受伤", "签订",
    "入职", "离职", "欠款", "拖欠", "解雇",
)

# 简单停用词
_STOP_WORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "我", "有", "和", "与", "怎么办",
    "怎么", "如何", "吗", "呢", "啊", "吧", "呀",
})


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _extract_keywords(text: str) -> str:
    """从文本中提取关键词（简单分词 + 停用词过滤）。"""
    if not text:
        return ""
    # 按标点和空格分词
    tokens = re.split(r"[，。、,.\s!?；;：:]+", text)
    keywords = [
        t for t in tokens
        if t and t not in _STOP_WORDS and len(t) >= 2
    ]
    return " ".join(keywords[:5])


def _extract_action_keywords(text: str) -> list[str]:
    """提取文本中出现的行为关键词。"""
    if not text:
        return []
    return [a for a in _ACTION_KEYWORDS_FOR_QUERY if a in text]


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def missing_fact_assessor(state: CaseState) -> dict[str, Any]:
    """缺失事实评估节点。

    规则+模板实现，后续接入 LLM 增强判断。

    职责
    ----
    - 检查 ``state.missing_facts`` 中是否有 ``is_blocking=True`` 的项。
    - 若有 blocking：返回 ``{}``（不重复添加，由 ``route_after_missing_fact``
      路由到 ``ask_user`` 中断提问）。
    - 若无 blocking：
        * 若 ``case_type`` 未识别，这里再尝试一次（轻量补充）。
        * 按 ``case_type`` 评估缺失事实，追加 blocking 项。
        * 若仍无 blocking 缺失，返回 ``{}``，路由进入 ``planner``。

    注意
    ----
    路由逻辑在 :mod:`lvyan.graph.routing` 的 :func:`route_after_missing_fact`
    中实现，本节点只需确保 ``missing_facts`` 正确传递/补充。
    """
    # TODO: 接入 LLM 增强抽取/判断
    missing_facts = _get(state, "missing_facts", []) or []
    # 已有缺失事实评估时不重复追加，避免 missing_facts 列表出现重复项
    if missing_facts:
        return {}

    # 无 blocking，尝试补充评估
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)

    # 若 fact_extractor 未识别 case_type，这里再尝试一次
    case_type_changed = False
    if not case_type:
        case_type = _detect_case_type(user_goal)
        if case_type:
            case_type_changed = True

    if not case_type:
        return {}

    existing_facts = _get(state, "facts", []) or []
    new_missing = _assess_missing_facts(case_type, existing_facts)

    if not new_missing:
        return {}

    update: dict[str, Any] = {"missing_facts": new_missing}
    if case_type_changed:
        update["case_type"] = case_type
    return update


def planner(state: CaseState) -> dict[str, Any]:
    """执行计划节点。

    规则+模板实现，后续接入 LLM 增强判断。

    职责
    ----
    - 根据 ``case_type`` 和 ``complexity`` 创建检索计划。
    - 生成 ``RetrievalQuery`` 列表：
        * 主查询：``user_goal`` 的关键词提取（hybrid 路由）。
        * 法条查询：``case_type`` + 关键行为词（bm25 路由）。
        * 案例查询：``case_type`` + "裁判规则"（case_rule 路由）。
    - 创建 ``PlanStep`` 列表（检索法规 / 检索类案 / 证据缺口分析）。
    - 设置 ``iteration=0``。

    返回更新字典：
        - ``plan``: PlanStep 列表（追加语义）
        - ``retrieval_queries``: RetrievalQuery 列表（追加语义）
        - ``iteration``: 0（覆盖语义，重置迭代计数）
    """
    # TODO: 接入 LLM 增强抽取/判断
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)

    # --- 生成检索查询 ---
    queries: list[RetrievalQuery] = []

    # 主查询：从 user_goal 提取关键词
    main_query_text = _extract_keywords(user_goal) or user_goal[:20]
    queries.append(
        RetrievalQuery(
            query_id=_short_id(),
            query_text=main_query_text,
            route="hybrid",
        )
    )

    # 法条查询 + 案例查询（依赖 case_type）
    if case_type:
        law_keywords = _CASE_TYPE_LAW_KEYWORDS.get(case_type, [])
        action_keywords = _extract_action_keywords(user_goal)
        # 法条查询：case_type + 法规名 + 关键行为词
        law_parts = [case_type]
        if law_keywords:
            law_parts.append(law_keywords[0])
        law_parts.extend(action_keywords[:2])
        law_query_text = " ".join(law_parts)
        queries.append(
            RetrievalQuery(
                query_id=_short_id(),
                query_text=law_query_text,
                route="bm25",
            )
        )

        # 案例查询：case_type + 裁判规则
        case_query_text = f"{case_type} 裁判规则"
        queries.append(
            RetrievalQuery(
                query_id=_short_id(),
                query_text=case_query_text,
                route="case_rule",
            )
        )

    # --- 创建执行计划 ---
    plan: list[PlanStep] = [
        PlanStep(
            step_id=_short_id(),
            action="检索相关法规",
            tool="statute_retrieval",
            status="pending",
        ),
        PlanStep(
            step_id=_short_id(),
            action="检索类案",
            tool="case_retrieval",
            status="pending",
        ),
        PlanStep(
            step_id=_short_id(),
            action="证据缺口分析",
            tool="evidence_analyzer",
            status="pending",
        ),
    ]

    return {
        "plan": plan,
        "retrieval_queries": queries,
        "iteration": 0,
    }
