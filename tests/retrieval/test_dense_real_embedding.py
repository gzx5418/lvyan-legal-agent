"""P0-1 修复：dense_search 在真实 embedding 可用时必须走真实向量空间。

回归覆盖：
  1. 真实可用 → 查询与文档都在真实向量空间，hash 桩不被调用
  2. 真实不可用 → 降级到 hash 路径（保持既有安全约束，不跨空间混算）
  3. 文档向量缓存 → 同一 chunk 不被重复 embed
  4. 批量 embedding → 候选集一次性提交，而非逐条
"""

from __future__ import annotations

import pytest

from lvyan.retrieval import dense


@pytest.fixture(autouse=True)
def _reset_dense_state(monkeypatch):
    """每个测试前后重置 dense 模块的探测与缓存状态。"""
    monkeypatch.setattr(dense, "_REAL_EMBEDDING_PROBED", None)
    monkeypatch.setattr(dense, "_ST_MODEL_CACHE", None)
    dense._DOC_VEC_CACHE.clear()
    yield
    dense._DOC_VEC_CACHE.clear()


def _make_chunks():
    return [
        {
            "chunk_id": "relevant",
            "title": "劳动合同法",
            "article_text": "经济补偿 工作年限",
        },
        {
            "chunk_id": "irrelevant",
            "title": "民法典",
            "article_text": "租赁合同 租金",
        },
    ]


def test_dense_search_uses_real_embedding_when_available(monkeypatch):
    """真实 embedding 可用时，查询与文档都走真实向量空间。"""
    monkeypatch.setattr(dense, "_probe_real_embedding", lambda: True)

    def fake_query_embedding(text: str):
        return [1.0, 0.0, 0.0]

    def fake_batch_embedding(texts):
        return [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]

    monkeypatch.setattr(dense, "_try_real_embedding", fake_query_embedding)
    monkeypatch.setattr(dense, "_try_real_embedding_batch", fake_batch_embedding)

    def forbid_hash(*args, **kwargs):
        raise AssertionError("真实路径不应调用 hash 桩")

    monkeypatch.setattr(dense, "_hash_embed", forbid_hash)

    results = dense.dense_search("劳动合同经济补偿", chunks=_make_chunks(), top_k=2)

    assert results, "真实 embedding 路径应返回结果"
    assert results[0].chunk_id == "relevant", "与查询同向的 chunk 应排第一"


def test_dense_search_falls_back_to_hash_when_real_unavailable(monkeypatch):
    """真实 embedding 不可用时降级到 hash 路径，保持既有安全约束。"""
    monkeypatch.setattr(dense, "_probe_real_embedding", lambda: False)

    def forbid_real(text: str):
        raise AssertionError("降级路径不应调用真实 embedding")

    monkeypatch.setattr(dense, "_try_real_embedding", forbid_real)
    monkeypatch.setattr(dense, "_try_real_embedding_batch", lambda texts: None)

    results = dense.dense_search("劳动合同经济补偿", chunks=_make_chunks(), top_k=2)

    assert results, "hash 降级路径仍应返回结果"
    assert results[0].chunk_id == "relevant"


def test_dense_search_caches_document_vectors(monkeypatch):
    """同一 chunk 的文档向量应被缓存，第二次检索不再调用 batch embedding。"""
    monkeypatch.setattr(dense, "_probe_real_embedding", lambda: True)
    monkeypatch.setattr(dense, "_try_real_embedding", lambda text: [1.0, 0.0, 0.0])

    call_count = {"n": 0}

    def counting_batch(texts):
        call_count["n"] += 1
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(dense, "_try_real_embedding_batch", counting_batch)

    chunks = _make_chunks()
    dense.dense_search("查询一", chunks=chunks, top_k=2)
    first_calls = call_count["n"]
    assert first_calls == 1, "首次应调用一次批量 embedding"

    dense.dense_search("查询二", chunks=chunks, top_k=2)
    assert call_count["n"] == first_calls, "缓存命中后不应再次批量 embed"


def test_dense_search_skips_batch_when_query_embedding_unavailable(monkeypatch):
    """查询向量获取失败时降级到 hash，避免查询与文档跨空间。"""
    monkeypatch.setattr(dense, "_probe_real_embedding", lambda: True)
    monkeypatch.setattr(dense, "_try_real_embedding", lambda text: None)

    batch_called = {"n": 0}

    def tracking_batch(texts):
        batch_called["n"] += 1
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(dense, "_try_real_embedding_batch", tracking_batch)

    results = dense.dense_search("经济补偿", chunks=_make_chunks(), top_k=2)

    assert results, "应通过 hash 降级返回结果"
    assert batch_called["n"] == 0, "查询向量不可用时不应再 batch embed 文档"
