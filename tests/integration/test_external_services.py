"""Real-service integration tests used by the CI infrastructure job."""

from __future__ import annotations

import os
import uuid

import pytest


def test_redis_rate_limit_is_shared_and_enforced() -> None:
    redis_url = os.getenv("LVYAN_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("LVYAN_TEST_REDIS_URL is not configured")

    from lvyan.api.rate_limit import RedisBackend

    backend = RedisBackend(redis_url)
    assert backend.is_healthy()
    key = f"integration:{uuid.uuid4().hex}"
    assert backend.is_allowed(key, 2)
    assert backend.is_allowed(key, 2)
    assert not backend.is_allowed(key, 2)


def test_opensearch_law_article_bulk_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    opensearch_url = os.getenv("LVYAN_TEST_OPENSEARCH_URL")
    if not opensearch_url:
        pytest.skip("LVYAN_TEST_OPENSEARCH_URL is not configured")

    from opensearchpy import OpenSearch

    from lvyan.config import settings
    from lvyan.scripts.ingest_laws import ArticleChunk, write_to_opensearch

    monkeypatch.setattr(settings, "opensearch_url", opensearch_url)
    monkeypatch.setattr(settings, "opensearch_user", "")
    monkeypatch.setattr(settings, "opensearch_password", "")
    chunk_id = f"integration-{uuid.uuid4().hex}"
    chunk = ArticleChunk(
        chunk_id=chunk_id,
        source_id="integration-law",
        title="集成测试法",
        article_number="第一条",
        article_text="这是仅用于验证法规索引写入链路的测试条文。",
        authority_level="law",
        status="effective",
        content_hash=uuid.uuid4().hex,
    )

    assert write_to_opensearch([chunk]) == 1
    client = OpenSearch(hosts=[opensearch_url], use_ssl=False)
    stored = client.get(index="law_articles_v2", id=chunk_id)
    assert stored["_source"]["title"] == "集成测试法"
