"""并行检索节点：法规与类案检索。

当前实现为顺序执行的法规检索 + 类案检索，后续可改为 LangGraph 并行 fan-out /
fan-in 子图，进一步提升吞吐。

职责
----
- 根据 ``retrieval_queries`` 调用 ``search_statutes``（version_aware）检索法规。
- 调用 ``search_cases`` 检索类案。
- 合并去重法规结果（按 ``source_id + article_number``），保留最高分。
- 更新 ``plan`` 中对应步骤的 ``status`` 为 ``"done"``。
- 异常不中断流程，失败时返回空列表。
"""

from __future__ import annotations

from typing import Any

from lvyan.retrieval.version_aware import search_statutes
from lvyan.schemas import Authority, CaseAuthority, CaseState
from lvyan.tools.cases import search_cases

__all__ = ["parallel_retrieval"]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _authority_score(auth: Authority) -> float:
    """取 Authority 的最高分（lexical / dense / rerank）。"""
    return max(
        float(_get(auth, "lexical_score", 0.0) or 0.0),
        float(_get(auth, "dense_score", 0.0) or 0.0),
        float(_get(auth, "rerank_score", 0.0) or 0.0),
    )


def _dedup_authorities(authorities: list[Authority]) -> list[Authority]:
    """按 ``source_id + article_number`` 去重，保留最高分版本。

    - key 缺失 ``article_number`` 时，仅用 ``source_id`` 作为键。
    - 同键多条时，保留分数最高者；分数相同保留先入者。
    """
    bucket: dict[str, Authority] = {}
    order: list[str] = []
    for auth in authorities:
        source_id = str(_get(auth, "source_id", "") or "")
        article_number = _get(auth, "article_number", None)
        article_key = str(article_number) if article_number else ""
        key = f"{source_id}::{article_key}"
        if key not in bucket:
            bucket[key] = auth
            order.append(key)
            continue
        existing = bucket[key]
        if _authority_score(auth) > _authority_score(existing):
            bucket[key] = auth
    return [bucket[k] for k in order]


def _mark_plan_done(plan: list[Any], tools_to_complete: tuple[str, ...]) -> list[Any]:
    """将 plan 中匹配 ``tool`` 的 ``pending``/``running`` 步骤标记为 ``done``。

    返回一个新的 plan 列表（保留原顺序），其中匹配步骤的 ``status`` 改为
    ``"done"``，``result_summary`` 写入简短摘要。其余步骤原样返回。
    """
    updated: list[Any] = []
    for step in plan or []:
        tool = _get(step, "tool", "")
        status = _get(step, "status", "")
        if tool in tools_to_complete and status in ("pending", "running", ""):
            # 兼容 PlanStep 对象与 dict
            try:
                if isinstance(step, dict):
                    new_step = dict(step)
                    new_step["status"] = "done"
                    new_step["result_summary"] = "检索完成"
                    updated.append(new_step)
                else:
                    step.status = "done"  # type: ignore[attr-defined]
                    step.result_summary = "检索完成"  # type: ignore[attr-defined]
                    updated.append(step)
            except Exception:  # noqa: BLE001  标记失败不影响检索结果
                updated.append(step)
        else:
            updated.append(step)
    return updated


def _to_case_authority(hit: Any) -> CaseAuthority:
    """将 ``CaseHit``（pydantic 模型或 dict）转换为 ``CaseAuthority``。"""
    case_id = str(_get(hit, "case_id", "") or "")
    court = _get(hit, "court", None)
    court_str = str(court) if court is not None else "未知法院"
    return CaseAuthority(
        case_id=case_id,
        case_number=_get(hit, "case_number", None),
        court=court_str,
        case_type=str(_get(hit, "case_type", "") or ""),
        brief_facts=str(_get(hit, "brief_facts", "") or ""),
        ruling_summary=str(_get(hit, "ruling_summary", "") or ""),
        similarity_score=float(_get(hit, "similarity_score", 0.0) or 0.0),
        source_url=_get(hit, "source_url", None),
    )


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def parallel_retrieval(state: CaseState) -> dict[str, Any]:
    """并行检索节点：法规检索 + 类案检索。

    规则+模板实现，后续接入 LLM 增强查询理解。

    职责
    ----
    - 读取 ``retrieval_queries``（planner 生成）。
    - 对每个 query 调用 ``search_statutes(query_text, top_k=10)``。
    - 合并去重结果（按 ``source_id + article_number``，保留最高分），
      追加写入 ``statutes``。
    - 调用 ``search_cases`` 检索类案，转换为 ``CaseAuthority``，追加写入
      ``cases``。
    - 更新 ``plan`` 中 ``statute_retrieval`` / ``case_retrieval`` 步骤的
      ``status`` 为 ``"done"``。

    异常处理
    --------
    检索失败时返回空列表，不中断流程；plan 仍标记为 ``done``（避免后续节点
    卡在 pending）。

    返回更新字典（追加语义）：
        - ``statutes``: list[Authority]
        - ``cases``: list[CaseAuthority]
        - ``plan``: list[PlanStep]（含状态更新）
    """
    # TODO: 接入 LLM 增强查询理解
    queries = _get(state, "retrieval_queries", []) or []

    # --- 法规检索 ---
    raw_statutes: list[Authority] = []
    for q in queries:
        query_text = _get(q, "query_text", "") or ""
        if not query_text.strip():
            continue
        try:
            results = search_statutes(query_text, top_k=10)
        except Exception:  # noqa: BLE001  检索失败不中断流程
            results = []
        raw_statutes.extend(results or [])

    statutes = _dedup_authorities(raw_statutes)

    # --- 类案检索 ---
    # 用第一个非空 query 或 user_goal 作为类案查询文本
    case_query_text = ""
    for q in queries:
        qt = _get(q, "query_text", "") or ""
        if qt.strip():
            case_query_text = qt
            break
    if not case_query_text:
        case_query_text = _get(state, "user_goal", "") or ""

    cases: list[CaseAuthority] = []
    if case_query_text.strip():
        try:
            case_search_result = search_cases(case_query_text, top_k=10)
        except Exception:  # noqa: BLE001  检索失败不中断流程
            case_search_result = None
        if case_search_result is not None:
            hits = _get(case_search_result, "results", []) or []
            for hit in hits:
                try:
                    cases.append(_to_case_authority(hit))
                except Exception:  # noqa: BLE001  转换失败跳过单条
                    continue

    # --- 更新 plan ---
    plan = _get(state, "plan", []) or []
    updated_plan = _mark_plan_done(
        plan, tools_to_complete=("statute_retrieval", "case_retrieval")
    )

    return {
        "statutes": statutes,
        "cases": cases,
        "plan": updated_plan,
    }
