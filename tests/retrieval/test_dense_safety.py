"""Dense 降级路径的正确性与性能边界测试。"""

from __future__ import annotations

from lvyan.retrieval import dense


def test_dense_search_keeps_query_and_documents_in_hash_space(monkeypatch):
    """Dense 降级检索不能调用真实 query embedding 与 hash 文档向量混算。"""
    chunks = [
        {"chunk_id": "a", "title": "劳动合同法", "article_text": "经济补偿"},
        {"chunk_id": "b", "title": "民法典", "article_text": "租赁合同"},
    ]

    def unexpected_real_embedding(_: str):
        raise AssertionError("dense_search 不应调用跨空间的真实 embedding")

    monkeypatch.setattr(dense, "embed_text", unexpected_real_embedding)
    results = dense.dense_search("劳动合同经济补偿", chunks=chunks, top_k=1)

    assert results
    assert results[0].chunk_id == "a"


def test_dense_global_search_uses_bounded_bm25_candidates(monkeypatch):
    """全库检索只对 BM25 候选计算 hash 向量，避免预计算整个法规库。"""
    chunks = [
        {"chunk_id": f"chunk-{index}", "title": "劳动法", "article_text": "经济补偿"}
        for index in range(1_000)
    ]
    monkeypatch.setitem(dense._load_article_chunks.__globals__, "_GLOBAL_CHUNKS_CACHE", chunks)

    received: dict[str, int] = {}

    def fake_bm25_search(*, query, chunks, top_k):
        received["top_k"] = top_k
        return []

    monkeypatch.setattr(dense, "bm25_search", fake_bm25_search)
    results = dense.dense_search("经济补偿", chunks=chunks, top_k=5)

    assert received["top_k"] == 100
    assert len(results) <= 5
