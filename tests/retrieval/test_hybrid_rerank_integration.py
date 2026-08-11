"""P0-2 修复：hybrid_search 必须在 RRF 融合后真正调用 reranker 重排。

回归覆盖：
  1. hybrid_search 默认启用 rerank（with_rerank=True）
  2. 真实 reranker 可用时，结果按真实 rerank 分数排序，而非 RRF 分数
  3. rerank 抛 RuntimeError（生产强制模式拒绝桩）时降级到 RRF 排序
  4. with_rerank=False 时跳过 rerank，保留 RRF 分数
"""

from __future__ import annotations

from lvyan.retrieval import hybrid
from lvyan.retrieval.lexical import ScoredChunk
from lvyan.scripts.ingest_laws import ArticleChunk


def _make_chunks():
    return [
        ArticleChunk(
            chunk_id="labor-1",
            source_id="law-labor-contract",
            title="中华人民共和国劳动合同法",
            article_number="第四十七条",
            article_text="经济补偿按照劳动者在本单位工作的年限计算",
            authority_level="法律",
            status="effective",
            content_hash="hash-1",
        ),
        ArticleChunk(
            chunk_id="labor-2",
            source_id="law-labor-contract",
            title="中华人民共和国劳动合同法",
            article_number="第八十七条",
            article_text="用人单位违反本法规定解除劳动合同的应当支付赔偿金",
            authority_level="法律",
            status="effective",
            content_hash="hash-2",
        ),
    ]


def test_hybrid_search_invokes_rerank_by_default(monkeypatch):
    """hybrid_search 默认应调用 rerank 重排。"""
    rerank_calls: list[tuple[str, int]] = []

    def tracking_rerank(query, candidates, top_k=10):
        rerank_calls.append((query, len(candidates)))
        return candidates[:top_k]

    monkeypatch.setattr(hybrid, "rerank", tracking_rerank)

    hybrid.hybrid_search("劳动合同经济补偿", top_k=5, chunks=_make_chunks())

    assert rerank_calls, "hybrid_search 默认应调用 rerank"
    assert rerank_calls[0][0] == "劳动合同经济补偿"


def test_hybrid_search_uses_real_reranker_scores(monkeypatch):
    """真实 reranker 可用时，结果按真实 rerank 分数排序（而非 RRF 分数）。"""
    chunks = _make_chunks()

    def real_rerank(query, candidates, top_k=10):
        # 故意把 RRF 排第二的 labor-2 提到第一，证明走的是 rerank 分数
        order = {"labor-2": 0.99, "labor-1": 0.10}
        reranked = sorted(
            candidates,
            key=lambda sc: order.get(sc.chunk_id, 0.0),
            reverse=True,
        )
        return [
            ScoredChunk(chunk_id=sc.chunk_id, score=order.get(sc.chunk_id, 0.0), chunk=sc.chunk)
            for sc in reranked[:top_k]
        ]

    monkeypatch.setattr(hybrid, "rerank", real_rerank)

    results = hybrid.hybrid_search("劳动合同经济补偿", top_k=5, chunks=chunks)

    assert results, "应返回结果"
    assert results[0].chunk_id == "labor-2", "应按 rerank 分数排序，labor-2 排第一"


def test_hybrid_search_falls_back_to_rrf_when_rerank_raises(monkeypatch):
    """rerank 抛 RuntimeError 时应降级到 RRF 排序，不抛异常。"""
    chunks = _make_chunks()

    def failing_rerank(query, candidates, top_k=10):
        raise RuntimeError("真实 Reranker 不可用，拒绝桩")

    monkeypatch.setattr(hybrid, "rerank", failing_rerank)

    results = hybrid.hybrid_search("劳动合同经济补偿", top_k=5, chunks=chunks)

    assert results, "rerank 失败时仍应返回 RRF 结果"
    scores = [sc.score for sc in results]
    assert scores == sorted(scores, reverse=True), "降级结果应按 RRF 分数降序"


def test_hybrid_search_skips_rerank_when_disabled(monkeypatch):
    """with_rerank=False 时跳过 rerank，score 为 RRF 分数。"""
    rerank_called = {"n": 0}

    def tracking_rerank(query, candidates, top_k=10):
        rerank_called["n"] += 1
        return candidates[:top_k]

    monkeypatch.setattr(hybrid, "rerank", tracking_rerank)

    results = hybrid.hybrid_search(
        "劳动合同经济补偿",
        top_k=5,
        chunks=_make_chunks(),
        with_rerank=False,
    )

    assert results, "应返回 RRF 结果"
    assert rerank_called["n"] == 0, "with_rerank=False 时不应调用 rerank"
