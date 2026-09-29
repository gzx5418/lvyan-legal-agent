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


def test_opensearch_case_documents_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """U-02:类案文档灌入 legal_cases 并可经 OpenSearchCaseSource 检索。"""
    opensearch_url = os.getenv("LVYAN_TEST_OPENSEARCH_URL")
    if not opensearch_url:
        pytest.skip("LVYAN_TEST_OPENSEARCH_URL is not configured")

    import asyncio
    from datetime import date

    from opensearchpy import OpenSearch

    from lvyan.config import settings
    from lvyan.retrieval.case_source import OpenSearchCaseSource
    from lvyan.schemas import CaseDocument
    from lvyan.scripts.ingest_cases import ingest_case_documents

    monkeypatch.setattr(settings, "opensearch_url", opensearch_url)
    monkeypatch.setattr(settings, "opensearch_user", "")
    monkeypatch.setattr(settings, "opensearch_password", "")

    case_id = f"integration-case-{uuid.uuid4().hex}"
    doc = CaseDocument(
        case_id=case_id,
        title="集成测试类案",
        case_number=f"(2026)京01民终{uuid.uuid4().hex[:6]}号",
        court="北京市第一中级人民法院",
        case_type="劳动争议",
        effective_level="reference",
        brief_facts="劳动者主张违法解除劳动合同。",
        ruling_summary="用人单位举证不能,应支付赔偿金。",
        judgment_date=date(2026, 1, 15),
        legal_elements=["劳动关系", "解除事实"],
    )
    succeeded, failed = ingest_case_documents([doc])
    assert (succeeded, failed) == (1, 0)

    client = OpenSearch(hosts=[opensearch_url], use_ssl=False)
    stored = client.get(index="legal_cases", id=case_id)
    assert stored["_source"]["title"] == "集成测试类案"
    assert stored["_source"]["legal_elements"] == ["劳动关系", "解除事实"]

    # 经数据源抽象检索(与主链接入 U-04 同一路径)
    source = OpenSearchCaseSource(client=client)
    results = asyncio.run(
        source.search("违法解除 赔偿金", top_k=5, filters={"case_type": "劳动争议"})
    )
    assert any(r.case_id == case_id for r in results)
