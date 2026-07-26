"""Reranker（SubTask 8.5）。

接入 Qwen3-Reranker（settings.reranker_model）：
    - 桩实现：用查询与 chunk 文本的 Jaccard 相似度作为 rerank score 占位。
    - 真实接入：通过 httpx 调用模型 API（保留骨架，不可用时降级到桩）。

公开接口：
    rerank(query, candidates, top_k=10) -> list[ScoredChunk]
"""

from __future__ import annotations

from typing import Any

from lvyan.config import settings
from lvyan.retrieval.lexical import ScoredChunk, _bm25_tokenize, log

# P3-20：拆分 HTTP client 与 CrossEncoder 的缓存，避免互相覆盖。
# 旧实现共用 _RERANK_MODEL_CACHE，网关失败后切到 CrossEncoder 时缓存被覆盖，
# 下次又把 CrossEncoder 当 httpx client 调 .post()。
_HTTP_CLIENT: Any = None
_CROSS_ENCODER: Any = None


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度 = |A ∩ B| / |A ∪ B|。"""
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _tokenize_set(text: str) -> set[str]:
    """对文本做 bigram 分词后转 set（用于 Jaccard）。"""
    return set(_bm25_tokenize(text))


def _try_real_rerank_score(query: str, candidate_texts: list[str]) -> list[float] | None:
    """尝试用真实 reranker（HTTP API / sentence-transformers）打分。

    返回 None 时由调用方降级到 Jaccard 桩。
    """
    global _HTTP_CLIENT, _CROSS_ENCODER
    gateway = settings.model_gateway_url

    # 1) 模型网关 HTTP API
    if gateway:
        try:
            import httpx  # type: ignore[import-untyped]

            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(timeout=15.0)

            headers: dict[str, str] = {}
            if settings.model_gateway_api_key:
                headers["Authorization"] = f"Bearer {settings.model_gateway_api_key}"

            resp = _HTTP_CLIENT.post(
                f"{gateway.rstrip('/')}/v1/rerank",
                json={
                    "model": settings.reranker_model,
                    "query": query,
                    "documents": candidate_texts,
                    "top_n": len(candidate_texts),
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            # 预期格式：{"results": [{"index": int, "relevance_score": float}, ...]}
            results = data.get("results", [])
            scores = [0.0] * len(candidate_texts)
            for r in results:
                idx = int(r["index"])
                if 0 <= idx < len(candidate_texts):
                    scores[idx] = float(r["relevance_score"])
            return scores
        except Exception as exc:  # noqa: BLE001
            log(f"[Rerank] 模型网关调用失败 ({exc})，降级到 Jaccard 桩")

    # 2) sentence-transformers CrossEncoder（独立缓存，不会被 HTTP 失败污染）
    try:
        from sentence_transformers import CrossEncoder  # type: ignore[import-untyped]

        if _CROSS_ENCODER is None or not isinstance(_CROSS_ENCODER, CrossEncoder):
            _CROSS_ENCODER = CrossEncoder(settings.reranker_model)
        pairs = [(query, t) for t in candidate_texts]
        scores = _CROSS_ENCODER.predict(pairs).tolist()
        return [float(s) for s in scores]
    except Exception as exc:  # noqa: BLE001
        log(f"[Rerank] CrossEncoder 不可用 ({exc})，降级到 Jaccard 桩")
        return None


def rerank(
    query: str,
    candidates: list[ScoredChunk],
    top_k: int = 10,
) -> list[ScoredChunk]:
    """对 candidates 重排序。

    Args:
        query: 用户查询
        candidates: 候选 ScoredChunk 列表（来自 hybrid_search）
        top_k: 返回前 K 条

    Returns:
        list[ScoredChunk]：按 rerank score 降序，最多 top_k 条。
        每条的 score 字段被更新为 rerank_score。
    """
    if not candidates:
        return []

    # 抽取 chunk 文本用于 rerank
    candidate_texts: list[str] = []
    for sc in candidates:
        chunk = sc.chunk
        if isinstance(chunk, dict):
            title = chunk.get("title", "") or ""
            article_number = chunk.get("article_number", "") or ""
            article_text = chunk.get("article_text", "") or ""
        else:
            title = getattr(chunk, "title", "") or ""
            article_number = getattr(chunk, "article_number", "") or ""
            article_text = getattr(chunk, "article_text", "") or ""
        # 标题 + 条号 + 正文拼成一个候选文本
        full = f"{title} {article_number} {article_text}".strip()
        candidate_texts.append(full)

    # TODO: 接入真实 Qwen3-Reranker
    real_scores = _try_real_rerank_score(query, candidate_texts)
    if real_scores is not None and len(real_scores) == len(candidates):
        scores = real_scores
    else:
        # Jaccard 桩
        query_tokens = _tokenize_set(query)
        scores = [
            _jaccard_similarity(query_tokens, _tokenize_set(text))
            for text in candidate_texts
        ]

    # 用 rerank score 更新 candidates
    reranked = [
        ScoredChunk(chunk_id=sc.chunk_id, score=round(float(s), 4), chunk=sc.chunk)
        for sc, s in zip(candidates, scores)
    ]
    reranked.sort(key=lambda x: x.score, reverse=True)
    return reranked[:top_k]


__all__ = ["rerank"]
