"""并行检索节点：法规与类案检索。

PR2 升级：RRF 融合后接入 Qwen3-Reranker 重排序，提升检索精度。

职责
----
- 根据 ``retrieval_queries`` 调用 ``search_statutes``（version_aware）检索法规。
- 调用 ``search_cases`` 检索类案。
- 合并去重法规结果（按 ``source_id + article_number``），保留最高分。
- **PR2**：去重后调用 ``rerank`` 对法规结果重排序（RRF → Reranker → Authority）。
- 更新 ``plan`` 中对应步骤的 ``status`` 为 ``"done"``。
- 异常不中断流程，失败时返回空列表。
"""

from __future__ import annotations

import logging
from typing import Any

from lvyan.retrieval.reranker import rerank
from lvyan.retrieval.version_aware import search_statutes
from lvyan.schemas import Authority, CaseAuthority, CaseState
from lvyan.tools.cases import search_cases

__all__ = ["parallel_retrieval"]

_logger = logging.getLogger("lvyan.nodes.retrieve_statutes")

# Reranker 候选池倍数：先召回 top_k * RERANK_POOL_MULTIPLIER 条，再 rerank 到 top_k
_RERANK_POOL_MULTIPLIER = 2


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


def _rerank_authorities(
    query: str,
    authorities: list[Authority],
    top_k: int = 10,
) -> list[Authority]:
    """对 Authority 列表调用 reranker 重排序（PR2）。

    将 Authority 包装为 ScoredChunk（chunk 字段为 dict），调用 ``rerank``，
    然后按 rerank 结果顺序返回原 Authority 对象，并更新 ``rerank_score``。

    Args:
        query: 重排序查询文本（通常为 user_goal）。
        authorities: RRF 融合后去重的 Authority 列表。
        top_k: 返回前 K 条。

    Returns:
        重排序后的 Authority 列表；reranker 不可用时返回原列表前 top_k 条。
    """
    if not authorities:
        return []

    from lvyan.retrieval.lexical import ScoredChunk

    # 构造 ScoredChunk 包装（chunk 为 dict，reranker 从中提取 title/article_text）
    scored_chunks: list[ScoredChunk] = []
    auth_by_idx: list[Authority] = []
    for i, auth in enumerate(authorities):
        chunk_dict = {
            "title": _get(auth, "title", "") or "",
            "article_number": _get(auth, "article_number", "") or "",
            "article_text": _get(auth, "article_text", "") or "",
        }
        scored_chunks.append(
            ScoredChunk(
                chunk_id=str(i),
                score=float(_get(auth, "lexical_score", 0.0) or 0.0),
                chunk=chunk_dict,
            )
        )
        auth_by_idx.append(auth)

    try:
        reranked = rerank(query=query, candidates=scored_chunks, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("reranker 调用失败，返回原序: %s", exc)
        return authorities[:top_k]

    if not reranked:
        return authorities[:top_k]

    # 按 rerank 结果顺序返回原 Authority，并更新 rerank_score
    result: list[Authority] = []
    for sc in reranked:
        idx = int(sc.chunk_id)
        if 0 <= idx < len(auth_by_idx):
            auth = auth_by_idx[idx]
            # 更新 rerank_score 字段
            try:
                auth.rerank_score = sc.score  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            result.append(auth)
    return result if result else authorities[:top_k]


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------

# P3-22 / P1-22：线程池并行检索
_CONCURRENT_EXECUTOR: Any = None


def _parallel_search_statutes(queries: list[Any]) -> list[Authority]:
    """并行执行 search_statutes。

    P1-22 修复：用 ``concurrent.futures.ThreadPoolExecutor`` 实现真正的并行检索，
    替代旧版有缺陷的 asyncio 路径（在已有 running loop 中调
    ``loop.run_until_complete()`` 会报错）。

    线程池方案：
      - 不依赖 asyncio 事件循环状态（同步节点函数的最佳选择）；
      - 每个 query 在独立线程中执行同步 ``search_statutes``；
      - 默认线程数 = min(len(queries), 4)，避免过度并发压垮检索后端。
    """
    global _CONCURRENT_EXECUTOR

    valid_queries: list[str] = []
    for q in queries:
        qt = _get(q, "query_text", "") or ""
        if qt.strip():
            valid_queries.append(qt)

    if not valid_queries:
        return []

    def _safe_search(qt: str) -> list[Authority]:
        try:
            return search_statutes(qt, top_k=10) or []
        except Exception:  # noqa: BLE001
            return []

    if len(valid_queries) == 1:
        return _safe_search(valid_queries[0])

    # 多个查询：用线程池并行
    if _CONCURRENT_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor
        _CONCURRENT_EXECUTOR = ThreadPoolExecutor(
            max_workers=min(len(valid_queries), 4),
            thread_name_prefix="lvyan-search",
        )

    try:
        futures = [
            _CONCURRENT_EXECUTOR.submit(_safe_search, qt)
            for qt in valid_queries
        ]
        results_nested: list[list[Authority]] = []
        for fut in futures:
            try:
                results_nested.append(fut.result(timeout=30.0))
            except Exception:  # noqa: BLE001 单个超时不影响其他
                results_nested.append([])
    except Exception:  # noqa: BLE001 线程池失败回退顺序
        results_nested = [_safe_search(qt) for qt in valid_queries]

    flattened: list[Authority] = []
    for sub in results_nested:
        flattened.extend(sub)
    return flattened


def parallel_retrieval(state: CaseState) -> dict[str, Any]:
    """并行检索节点：法规检索 + 类案检索。

    PR2 升级：RRF 融合去重后接入 Qwen3-Reranker 重排序。
    P1-22 升级：用 ``concurrent.futures.ThreadPoolExecutor`` 真正并行执行
    同步检索调用（替代旧版有缺陷的 asyncio 路径），多查询场景下延迟
    从累加降为最慢单次。

    职责
    ----
    - 读取 ``retrieval_queries``（planner 生成）。
    - 对每个 query 并行调用 ``search_statutes(query_text, top_k=10)``。
    - 合并去重结果（按 ``source_id + article_number``，保留最高分）。
    - **PR2**：用 user_goal 作为查询，对去重后的法规结果调用 reranker 重排序。
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
    queries = _get(state, "retrieval_queries", []) or []

    # --- 法规检索（P3-22：真正的并行）---
    raw_statutes = _parallel_search_statutes(queries)

    statutes = _dedup_authorities(raw_statutes)

    # --- PR2: Reranker 重排序（RRF → Reranker）---
    # 用 user_goal 作为 rerank 查询（最能反映用户真实意图）
    rerank_query = _get(state, "user_goal", "") or ""
    if rerank_query.strip() and statutes:
        # 先召回更大候选池，再 rerank 到 top_k
        pool_k = min(len(statutes), 10 * _RERANK_POOL_MULTIPLIER)
        statutes = _rerank_authorities(
            query=rerank_query,
            authorities=statutes[:pool_k],
            top_k=10,
        )

    # --- 类案检索 ---
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
