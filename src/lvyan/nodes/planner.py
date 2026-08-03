"""缺失事实评估与执行计划节点。

本模块容纳两个节点函数：
- ``missing_fact_assessor``：评估关键事实是否缺失，决定是否中断向用户追问。
- ``planner``：基于完整事实生成执行计划（检索 / 推理 / 证据补全步骤）。

PR2 升级：planner 优先用 LLM 生成 ReAct 风格计划（JSON 模式 + 规则校验），
失败时降级到规则+模板。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from lvyan.schemas import CaseState, PlanStep, RetrievalQuery

# 复用 triage 的案由识别与 fact_extractor 的缺失事实评估
from lvyan.nodes.fact_extractor import _assess_missing_facts, _get, _short_id
from lvyan.nodes.triage import _detect_case_type

__all__ = ["missing_fact_assessor", "planner"]

_logger = logging.getLogger("lvyan.nodes.planner")

# LLM 计划允许的 route 值
_ALLOWED_ROUTES = {"hybrid", "bm25", "case_rule", "article_no", "dense"}


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


def _try_llm_plan(
    user_goal: str,
    case_type: str | None,
    facts: list[Any],
    attachment_context: str = "",
    conversation_summary: str = "",
) -> tuple[list[RetrievalQuery], list[PlanStep]] | None:
    """尝试用 LLM 生成 ReAct 风格检索计划。

    Returns:
        ``(queries, plan_steps)`` 或 ``None``（LLM 不可用/输出无效时）。
    """
    from lvyan.llm import chat_json, llm_available

    if not llm_available() or not user_goal.strip():
        return None

    facts_summary = "; ".join(
        str(_get(f, "content", "")) for f in (facts or [])[:8]
    ) or "暂无结构化事实"
    case_hint = f"案由：{case_type}" if case_type else "案由待定"
    context_block = (
        f"\n相关材料摘要：\n{attachment_context}\n" if attachment_context.strip() else ""
    )
    history_block = (
        f"\n此前对话摘要：\n{conversation_summary}\n" if conversation_summary.strip() else ""
    )

    system_prompt = (
        "你是法律检索计划生成助手。根据用户案情生成检索查询与执行步骤。"
        "只输出 JSON，不要解释。"
    )
    user_prompt = (
        f"{case_hint}\n用户目标：{user_goal}\n已知事实：{facts_summary}\n{context_block}{history_block}\n"
        "请生成检索计划，输出 JSON 格式：\n"
        '{"retrieval_queries": [{"query_text": "检索词", "route": "hybrid|bm25|case_rule|article_no"}], '
        '"plan_steps": [{"action": "步骤描述", "tool": "statute_retrieval|case_retrieval|evidence_analyzer"}]}\n\n'
        "要求：\n"
        "1. 生成 2-5 条针对性检索查询，覆盖法规与类案\n"
        "2. route 必须是 hybrid/bm25/case_rule/article_no 之一\n"
        "3. plan_steps 包含 2-5 个执行步骤\n"
        "4. 查询词应包含法律术语，提升检索精度\n"
        "5. 不要编造事实"
    )

    result = chat_json(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1000,
    )
    if result is None:
        return None

    # 解析检索查询
    raw_queries = result.get("retrieval_queries", [])
    if not isinstance(raw_queries, list) or not raw_queries:
        return None

    queries: list[RetrievalQuery] = []
    seen_texts: set[str] = set()
    for rq in raw_queries:
        if not isinstance(rq, dict):
            continue
        text = str(rq.get("query_text", "")).strip()
        if not text or text in seen_texts:
            continue
        route = str(rq.get("route", "hybrid")).strip()
        if route not in _ALLOWED_ROUTES:
            route = "hybrid"
        seen_texts.add(text)
        queries.append(
            RetrievalQuery(query_id=_short_id(), query_text=text, route=route)
        )

    # 解析执行步骤
    raw_steps = result.get("plan_steps", [])
    plan_steps: list[PlanStep] = []
    if isinstance(raw_steps, list):
        for rs in raw_steps:
            if not isinstance(rs, dict):
                continue
            action = str(rs.get("action", "")).strip()
            if not action:
                continue
            tool = str(rs.get("tool", "statute_retrieval")).strip()
            plan_steps.append(
                PlanStep(
                    step_id=_short_id(),
                    action=action,
                    tool=tool,
                    status="pending",
                )
            )

    if not queries:
        return None

    # 确保至少有基本执行步骤
    if not plan_steps:
        plan_steps = [
            PlanStep(step_id=_short_id(), action="检索相关法规", tool="statute_retrieval", status="pending"),
            PlanStep(step_id=_short_id(), action="检索类案", tool="case_retrieval", status="pending"),
            PlanStep(step_id=_short_id(), action="证据缺口分析", tool="evidence_analyzer", status="pending"),
        ]

    _logger.info("LLM 计划生成成功: %d queries, %d steps", len(queries), len(plan_steps))
    return queries, plan_steps


def planner(state: CaseState) -> dict[str, Any]:
    """执行计划节点。

    PR2：优先用 LLM 生成 ReAct 风格检索计划（JSON 模式 + 规则校验），
    失败时降级到规则+模板。

    职责
    ----
    - 根据 ``case_type`` 和 ``complexity`` 创建检索计划。
    - 生成 ``RetrievalQuery`` 列表。
    - 创建 ``PlanStep`` 列表。
    - 设置 ``iteration=0``。

    返回更新字典：
        - ``plan``: PlanStep 列表（追加语义）
        - ``retrieval_queries``: RetrievalQuery 列表（追加语义）
        - ``iteration``: 0（覆盖语义，重置迭代计数）
    """
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []
    attachment_context = _get(state, "relevant_attachment_context", "") or ""
    conversation_summary = _get(state, "conversation_summary", "") or ""

    # --- 优先 LLM 计划生成 ---
    llm_result = _try_llm_plan(
        user_goal, case_type, facts, attachment_context, conversation_summary
    )
    if llm_result is not None:
        queries, plan = llm_result
        return {
            "plan": plan,
            "retrieval_queries": queries,
            "iteration": 0,
        }

    # --- 降级：规则+模板 ---
    queries: list[RetrievalQuery] = []
    main_query_text = _extract_keywords(user_goal) or user_goal[:20]
    queries.append(
        RetrievalQuery(query_id=_short_id(), query_text=main_query_text, route="hybrid")
    )

    if case_type:
        law_keywords = _CASE_TYPE_LAW_KEYWORDS.get(case_type, [])
        action_keywords = _extract_action_keywords(user_goal)
        law_parts = [case_type]
        if law_keywords:
            law_parts.append(law_keywords[0])
        law_parts.extend(action_keywords[:2])
        queries.append(
            RetrievalQuery(query_id=_short_id(), query_text=" ".join(law_parts), route="bm25")
        )
        queries.append(
            RetrievalQuery(query_id=_short_id(), query_text=f"{case_type} 裁判规则", route="case_rule")
        )

    plan: list[PlanStep] = [
        PlanStep(step_id=_short_id(), action="检索相关法规", tool="statute_retrieval", status="pending"),
        PlanStep(step_id=_short_id(), action="检索类案", tool="case_retrieval", status="pending"),
        PlanStep(step_id=_short_id(), action="证据缺口分析", tool="evidence_analyzer", status="pending"),
    ]

    return {
        "plan": plan,
        "retrieval_queries": queries,
        "iteration": 0,
    }
