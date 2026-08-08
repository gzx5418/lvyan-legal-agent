"""四路混合检索与 RRF 融合（SubTask 8.4）。

调用 BM25 / Dense / 精确法条号 / 案由规则 四路召回，用 Reciprocal Rank
Fusion（RRF）融合排序，按 ``only_effective`` 和 ``as_of`` 过滤版本。

公开接口：
    hybrid_search(query, top_k=20, only_effective=True, as_of=None) -> list[ScoredChunk]
"""

from __future__ import annotations

from datetime import date
from typing import Any

from lvyan.retrieval.case_rule import case_rule_search
from lvyan.retrieval.dense import dense_search
from lvyan.retrieval.exact_match import article_no_search
from lvyan.retrieval.lexical import ScoredChunk, bm25_search, _load_article_chunks

# RRF 超参
_RRF_K = 60


def _filter_chunk(chunk: Any, only_effective: bool, as_of: date | None) -> bool:
    """根据 status / effective_date / expiry_date 过滤 chunk。

    P0-4 修复：当 as_of 给定时，按时间窗口判断，不再要求 status=="effective"，
    否则会错误排除「现已废止但在 as_of 时间点仍有效」的历史法规。

    规则：
      - as_of 给定：
        - effective_date 已知且晚于 as_of → 尚未生效 → 排除
        - expiry_date 已知且不晚于 as_of → 已失效 → 排除
        - status=="repealed" 且 expiry_date 未知 → 保守排除
        - 其余保留（包括 status=="repealed" 但 expiry_date > as_of 的情况）
      - as_of 为 None 且 only_effective=True：仅保留 status=="effective"
      - as_of 为 None 且 only_effective=False：不过滤
    """
    if isinstance(chunk, dict):
        status = chunk.get("status", "unknown")
        effective_date = chunk.get("effective_date")
        expiry_date = chunk.get("expiry_date")
    else:
        status = getattr(chunk, "status", "unknown")
        effective_date = getattr(chunk, "effective_date", None)
        expiry_date = getattr(chunk, "expiry_date", None)

    if as_of is not None:
        # 时间点查询：按 effective_date / expiry_date 窗口判断
        if effective_date is not None:
            try:
                if isinstance(effective_date, str):
                    eff = date.fromisoformat(effective_date[:10])
                elif isinstance(effective_date, date):
                    eff = effective_date
                else:
                    eff = None
                if eff is not None and eff > as_of:
                    return False
            except (ValueError, TypeError):
                pass
        if expiry_date is not None:
            try:
                if isinstance(expiry_date, str):
                    exp = date.fromisoformat(expiry_date[:10])
                elif isinstance(expiry_date, date):
                    exp = expiry_date
                else:
                    exp = None
                if exp is not None and exp <= as_of:
                    return False
            except (ValueError, TypeError):
                pass
        if status == "repealed" and expiry_date is None:
            return False
        return True

    if only_effective and status != "effective":
        return False

    return True


def _rrf_fuse(
    routes: list[list[ScoredChunk]],
    k: int = _RRF_K,
) -> dict[str, float]:
    """RRF 融合：score = sum(1/(k + rank_i)) for each route。

    返回 {chunk_id: fused_score}。
    """
    fused: dict[str, float] = {}
    for route in routes:
        for rank, sc in enumerate(route):
            # rank 从 0 开始；RRF 公式常用 1-based rank
            contribution = 1.0 / (k + rank + 1)
            fused[sc.chunk_id] = fused.get(sc.chunk_id, 0.0) + contribution
    return fused


def hybrid_search(
    query: str,
    top_k: int = 20,
    only_effective: bool = True,
    as_of: date | None = None,
    chunks: list[Any] | None = None,
) -> list[ScoredChunk]:
    """四路混合检索 + RRF 融合。

    Args:
        query: 用户查询
        top_k: 返回前 K 条
        only_effective: True 时仅保留 status="effective" 的 chunk
        as_of: 给定日期时仅保留 effective_date <= as_of 的 chunk
        chunks: 候选 ArticleChunk；None 时从全库加载

    Returns:
        list[ScoredChunk]：按 RRF 分数降序，最多 top_k 条。
        每条 score 字段为 RRF 融合分数，chunk 字段为对应 ArticleChunk。
    """
    # 加载 chunks 一次，传给各路避免重复加载
    if chunks is None:
        chunks = _load_article_chunks()
    if not chunks:
        return []

    # 构建一个 chunk_id → chunk 的索引便于最后聚合
    chunk_by_id: dict[str, Any] = {}
    for c in chunks:
        if isinstance(c, dict):
            cid = c.get("chunk_id", "")
        else:
            cid = getattr(c, "chunk_id", "")
        if cid and cid not in chunk_by_id:
            chunk_by_id[cid] = c

    # 先按过滤条件预筛 chunks（用于 article_no / case_rule 线性扫描路）
    filtered_chunks = [c for c in chunks if _filter_chunk(c, only_effective, as_of)]

    # BM25 / Dense：传原始 chunks 以命中全局索引/向量缓存。filtered_chunks 是
    # 新列表，``is _GLOBAL_CHUNKS_CACHE`` 判断会失败导致每次重建索引（性能从
    # <1s 退化到 ~13s）。改为对结果做版本过滤。
    fetch_k = max(top_k * 4, 100)
    bm25_results = bm25_search(query=query, chunks=chunks, top_k=fetch_k)
    dense_results = dense_search(query=query, top_k=fetch_k, chunks=chunks)
    if only_effective or as_of is not None:
        bm25_results = [sc for sc in bm25_results if _filter_chunk(sc.chunk, only_effective, as_of)]
        dense_results = [
            sc for sc in dense_results if _filter_chunk(sc.chunk, only_effective, as_of)
        ]

    # article_no / case_rule：线性扫描，传 filtered_chunks 减少遍历量
    article_no_results = article_no_search(query=query, chunks=filtered_chunks)
    case_rule_results = case_rule_search(query=query, chunks=filtered_chunks)

    routes = [bm25_results, dense_results, article_no_results, case_rule_results]

    # RRF 融合
    fused = _rrf_fuse(routes, k=_RRF_K)
    if not fused:
        return []

    # 排序并取 top_k
    sorted_ids = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]

    results: list[ScoredChunk] = []
    for chunk_id, score in sorted_ids:
        chunk = chunk_by_id.get(chunk_id)
        if chunk is None:
            # 兜底：在 filtered_chunks 中查找（理论上 chunk_by_id 应已覆盖）
            for c in filtered_chunks:
                if isinstance(c, dict):
                    if c.get("chunk_id", "") == chunk_id:
                        chunk = c
                        break
                else:
                    if getattr(c, "chunk_id", "") == chunk_id:
                        chunk = c
                        break
        if chunk is None:
            continue
        results.append(ScoredChunk(chunk_id=chunk_id, score=round(score, 4), chunk=chunk))

    return results


__all__ = ["hybrid_search"]
