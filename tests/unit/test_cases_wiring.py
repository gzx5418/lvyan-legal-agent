"""U-04:MultiSourceRetriever 接入主链测试。

双路径验收(计划 A 方向):
1. OpenSearch 可用 → search_cases 返回真实类案(source=opensearch,含案号/法院);
2. 未配置/不可达/零命中 → curated 兜底(source=curated_knowledge,标注兼容);
3. 节点层 case_type 透传;
4. build_default_retriever 按健康状态组源。
"""

from __future__ import annotations

from lvyan.retrieval.case_source import (
    CaseResult,
    CuratedCaseSource,
    MultiSourceRetriever,
    build_default_retriever,
)
from lvyan.tools.cases import search_cases


def _os_result(case_id: str = "os-1") -> CaseResult:
    return CaseResult(
        case_id=case_id,
        title="张某诉某公司劳动争议案",
        court="北京市第一中级人民法院",
        date="2022-06-30",
        case_type="劳动争议",
        summary="用人单位举证不能,应支付赔偿金。",
        source="opensearch",
        score=8.5,
        metadata={
            "case_number": "(2022)京01民终123号",
            "brief_facts": "劳动者主张违法解除。",
            "effective_level": "reference",
        },
    )


def test_search_cases_prefers_opensearch_when_available(monkeypatch):
    """OpenSearch 可用:返回真实类案(案号/法院/来源标注)。"""
    from lvyan.tools import cases as cases_mod
    from lvyan.tools.cases import CaseHit

    captured: dict = {}

    def _fake_opensearch_hits(query, top_k, case_type):
        captured["query"] = query
        captured["case_type"] = case_type
        assert top_k == 5
        return [
            CaseHit(
                case_id="os-1",
                case_number="(2022)京01民终123号",
                court="北京市第一中级人民法院",
                case_type="劳动争议",
                brief_facts="劳动者主张违法解除。",
                ruling_summary="用人单位举证不能,应支付赔偿金。",
                similarity_score=8.5,
                source="opensearch",
            )
        ]

    monkeypatch.setattr(cases_mod, "_search_opensearch_hits", _fake_opensearch_hits)

    result = search_cases("违法解除赔偿", top_k=5, case_type="劳动争议")
    assert result.success is True
    assert result.total == 1
    hit = result.results[0]
    assert hit.source == "opensearch"
    assert hit.case_number == "(2022)京01民终123号"
    assert hit.court == "北京市第一中级人民法院"
    assert hit.brief_facts == "劳动者主张违法解除。"
    assert hit.ruling_summary.startswith("用人单位")
    assert captured["case_type"] == "劳动争议"


def test_search_cases_falls_back_to_curated_when_unavailable(monkeypatch):
    """OpenSearch 返回 None(未配置/不可用)→ curated 兜底,标注兼容。"""
    from lvyan.tools import cases as cases_mod

    monkeypatch.setattr(cases_mod, "_search_opensearch_hits", lambda *a, **k: None)
    result = search_cases("劳动仲裁 时效", top_k=3)
    assert result.success is True
    assert result.total > 0
    for hit in result.results:
        assert hit.source == "curated_knowledge"


def test_search_cases_opensearch_zero_hits_still_opensearch(monkeypatch):
    """空列表 = 检索成功但无结果:不回退 curated(返回空,由上层决策)。"""
    from lvyan.tools import cases as cases_mod

    monkeypatch.setattr(cases_mod, "_search_opensearch_hits", lambda *a, **k: [])
    result = search_cases("完全不相关的查询词组", top_k=3)
    assert result.success is True
    assert result.total == 0
    assert result.results == []


def test_node_passes_case_type_to_search_cases(monkeypatch):
    """节点层把 triage 的 case_type 透传给 search_cases。"""
    captured: dict = {}

    def _fake_search_cases(query, top_k=10, case_type=None):
        captured["case_type"] = case_type
        from lvyan.tools.cases import CaseSearchResult

        return CaseSearchResult(
            tool_name="search_cases",
            success=True,
            query=query,
            total=0,
            results=[],
        )

    from lvyan.nodes import retrieve_statutes as rs_mod

    monkeypatch.setattr(rs_mod, "search_cases", _fake_search_cases)

    state = {
        "user_goal": "公司违法解除怎么赔偿",
        "case_type": "劳动争议",
        "plan": [
            {
                "step_id": "s1",
                "step_type": "case_retrieval",
                "query_text": "违法解除赔偿",
                "done": False,
            }
        ],
        "retrieval_queries": [
            {"query_id": "q1", "query_text": "违法解除赔偿", "purpose": "case", "done": False}
        ],
    }
    rs_mod.parallel_retrieval(state)
    assert captured["case_type"] == "劳动争议"


def test_build_default_retriever_curated_only_when_unhealthy(monkeypatch):
    """OpenSearch 不健康 → 仅 curated 单源(现状行为)。"""
    from lvyan.retrieval import case_source as cs_mod

    class _UnhealthySource(CuratedCaseSource):
        @property
        def name(self) -> str:
            return "opensearch"

        async def healthcheck(self) -> bool:
            return False

    monkeypatch.setattr(cs_mod, "OpenSearchCaseSource", _UnhealthySource)
    retriever = build_default_retriever()
    assert [s.name for s in retriever._sources] == ["curated"]


def test_build_default_retriever_includes_opensearch_when_healthy(monkeypatch):
    """OpenSearch 健康 → 双源(OpenSearch + curated)。"""
    from lvyan.retrieval import case_source as cs_mod

    class _HealthySource(CuratedCaseSource):
        @property
        def name(self) -> str:
            return "opensearch"

        async def healthcheck(self) -> bool:
            return True

    monkeypatch.setattr(cs_mod, "OpenSearchCaseSource", _HealthySource)
    retriever = build_default_retriever()
    names = [s.name for s in retriever._sources]
    assert "opensearch" in names and "curated" in names
    # OpenSearch 权重更高
    assert retriever._weights["opensearch"] > retriever._weights["curated"]


def test_multi_source_fusion_dedup_and_weight():
    """多源融合:同 case_id 去重、加权排序(纯聚合逻辑回归)。"""
    retriever = MultiSourceRetriever()

    class _StaticSource(CuratedCaseSource):
        def __init__(self, name: str, results: list[CaseResult]) -> None:
            super().__init__()
            self._name = name
            self._results = results

        @property
        def name(self) -> str:
            return self._name

        async def search(self, query, *, top_k=10, filters=None):
            return self._results

        async def healthcheck(self) -> bool:
            return True

    retriever.add_source(_StaticSource("a", [_os_result("dup"), _os_result("only-a")]), weight=1.0)
    retriever.add_source(_StaticSource("b", [_os_result("dup"), _os_result("only-b")]), weight=2.0)
    import asyncio

    results = asyncio.run(retriever.search("q", top_k=10))
    ids = [r.case_id for r in results]
    assert ids.count("dup") == 1  # 跨源去重
    assert set(ids) == {"dup", "only-a", "only-b"}
    # only-b 来自权重 2.0 的源,score 翻倍应排在前
    assert results[0].case_id == "only-b"
