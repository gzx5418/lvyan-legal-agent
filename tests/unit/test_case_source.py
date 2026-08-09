"""多来源案例检索的行为测试。"""

from __future__ import annotations

import json

import pytest

from lvyan.retrieval.case_source import (
    CaseResult,
    CaseSource,
    CuratedCaseSource,
    MultiSourceRetriever,
    OpenSearchCaseSource,
)


@pytest.mark.asyncio
async def test_curated_source_loads_searches_and_maps_result(tmp_path):
    (tmp_path / "labor.json").write_text(
        json.dumps(
            {
                "case_id": "case-labor-1",
                "title": "劳动报酬争议",
                "court": "北京市某区人民法院",
                "case_type": "劳动争议",
                "summary": "用人单位拖欠劳动报酬。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = CuratedCaseSource(str(tmp_path))

    assert await source.healthcheck() is True
    results = await source.search("劳动", top_k=5)

    assert len(results) == 1
    assert results[0].case_id == "case-labor-1"
    assert results[0].source == "curated"
    assert results[0].court == "北京市某区人民法院"


class _StaticSource(CaseSource):
    def __init__(self, name: str, results: list[CaseResult], healthy: bool = True) -> None:
        self._name = name
        self._results = results
        self._healthy = healthy

    @property
    def name(self) -> str:
        return self._name

    async def search(self, query: str, *, top_k: int = 10, filters=None) -> list[CaseResult]:
        return self._results[:top_k]

    async def healthcheck(self) -> bool:
        return self._healthy


@pytest.mark.asyncio
async def test_multi_source_retriever_deduplicates_weights_and_checks_health():
    first = _StaticSource(
        "first",
        [
            CaseResult(case_id="same", title="先命中的案例", score=0.5),
            CaseResult(case_id="only-first", title="案例一", score=0.3),
        ],
    )
    second = _StaticSource(
        "second",
        [
            CaseResult(case_id="same", title="重复案例", score=1.0),
            CaseResult(case_id="only-second", title="案例二", score=0.4),
        ],
        healthy=False,
    )
    retriever = MultiSourceRetriever([first, second], weights={"second": 3.0})

    results = await retriever.search("测试", top_k=3)

    assert [result.case_id for result in results] == ["only-second", "same", "only-first"]
    assert len([result for result in results if result.case_id == "same"]) == 1
    assert await retriever.healthcheck() == {"first": True, "second": False}


class _OpenSearchClient:
    def __init__(self) -> None:
        self.request: dict | None = None

    def search(self, **kwargs):
        self.request = kwargs
        return {
            "hits": {
                "hits": [
                    {
                        "_id": "fallback-id",
                        "_score": 1.5,
                        "_source": {
                            "title": "工伤认定案例",
                            "court": "某法院",
                            "case_type": "工伤认定",
                            "summary": "上下班途中事故责任认定。",
                            "region": "北京",
                        },
                    }
                ]
            }
        }

    def ping(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_opensearch_source_builds_filtered_query_and_maps_hits():
    client = _OpenSearchClient()
    source = OpenSearchCaseSource(index_name="cases", client=client)

    results = await source.search("工伤", top_k=3, filters={"region": "北京"})

    assert client.request is not None
    assert client.request["index"] == "cases"
    assert client.request["body"]["query"]["bool"]["filter"] == [{"term": {"region": "北京"}}]
    assert results[0].case_id == "fallback-id"
    assert results[0].source == "opensearch"
    assert results[0].metadata == {"region": "北京"}
    assert await source.healthcheck() is True
