"""多来源案例库抽象层。

支持从多个数据源检索案例/判例：
1. 本地精编案例库（curated）
2. OpenSearch 全文检索
3. 外部 API（裁判文书网等）
4. 向量数据库（语义检索）

设计
----
- 统一 `CaseSource` 协议：每个数据源实现 `search()` 方法
- `MultiSourceRetriever` 聚合多个源，支持权重、去重、融合排序
- 可插拔：通过配置启用/禁用数据源
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

_logger = logging.getLogger("lvyan.retrieval.case_source")

__all__ = [
    "CaseResult",
    "CaseSource",
    "MultiSourceRetriever",
    "CuratedCaseSource",
    "OpenSearchCaseSource",
]


@dataclass
class CaseResult:
    """案例检索结果。"""

    case_id: str
    title: str
    court: str = ""
    date: str = ""
    case_type: str = ""
    summary: str = ""
    full_text: str = ""
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class CaseSource(ABC):
    """案例数据源协议。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称。"""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[CaseResult]:
        """检索案例。

        Args:
            query: 搜索查询（自然语言）。
            top_k: 返回最多 top_k 个结果。
            filters: 过滤条件（法院、时间范围、案由等）。
        """
        ...

    @abstractmethod
    async def healthcheck(self) -> bool:
        """数据源健康检查。"""
        ...


class CuratedCaseSource(CaseSource):
    """精编案例库（本地 JSON）。

    从 knowledge/curated/cases/ 加载预编制的典型案例。
    适用于常见法律问题的高质量参考。
    """

    def __init__(self, cases_dir: str | None = None) -> None:
        from pathlib import Path

        if cases_dir:
            self._dir = Path(cases_dir)
        else:
            from lvyan.config import AGENT_DIR

            self._dir = AGENT_DIR / "knowledge" / "curated" / "cases"

        self._cases: list[dict[str, Any]] = []
        self._loaded = False

    @property
    def name(self) -> str:
        return "curated"

    def _load(self) -> None:
        """惰性加载案例。"""
        if self._loaded:
            return

        import json

        if not self._dir.is_dir():
            _logger.warning("精编案例目录不存在: %s", self._dir)
            self._loaded = True
            return

        for f in sorted(self._dir.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        self._cases.extend(data)
                    elif isinstance(data, dict):
                        self._cases.append(data)
            except Exception as exc:  # noqa: BLE001 boundary-exception: 单文件加载失败不阻断
                _logger.warning("加载案例文件失败 %s: %s", f.name, exc)

        self._loaded = True
        _logger.info("精编案例库加载完成: %d 条案例", len(self._cases))

    async def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[CaseResult]:
        """简单关键词匹配搜索（精编库数量少，无需向量化）。"""
        self._load()

        query_lower = query.lower()
        scored: list[tuple[float, dict[str, Any]]] = []

        for case in self._cases:
            score = 0.0
            searchable = " ".join(
                [
                    case.get("title", ""),
                    case.get("summary", ""),
                    case.get("case_type", ""),
                    case.get("court", ""),
                ]
            ).lower()

            # 简单 TF 评分
            for term in query_lower.split():
                if term in searchable:
                    score += 1.0

            if score > 0:
                scored.append((score, case))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for score, case in scored[:top_k]:
            results.append(
                CaseResult(
                    case_id=case.get("case_id", case.get("id", "")),
                    title=case.get("title", ""),
                    court=case.get("court", ""),
                    date=case.get("date", ""),
                    case_type=case.get("case_type", ""),
                    summary=case.get("summary", ""),
                    source="curated",
                    score=score,
                    metadata=case.get("metadata", {}),
                )
            )

        return results

    async def healthcheck(self) -> bool:
        self._load()
        return True


class OpenSearchCaseSource(CaseSource):
    """OpenSearch 案例库数据源。

    索引文档使用 ``case_id/title/court/date/case_type/summary/full_text`` 字段；
    其他字段会保留在 :attr:`CaseResult.metadata`。客户端允许注入，便于测试和
    私有 OpenSearch 部署复用既有认证/TLS 配置。
    """

    def __init__(
        self,
        *,
        index_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        import os

        self._index_name = index_name or os.getenv("CASE_OPENSEARCH_INDEX", "legal_cases")
        self._client = client

    @property
    def name(self) -> str:
        return "opensearch"

    def _get_client(self) -> Any:
        import os

        if self._client is not None:
            return self._client

        from opensearchpy import OpenSearch
        from lvyan.config import settings

        # 证书校验默认开启；仅当显式配置 OPENSEARCH_VERIFY_CERTS=false 时禁用
        # （此前硬编码 verify_certs=False 会在生产静默关闭 TLS 校验）
        verify_certs = os.getenv("OPENSEARCH_VERIFY_CERTS", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        if not verify_certs:
            _logger.warning(
                "OPENSEARCH_VERIFY_CERTS 已显式禁用，OpenSearch TLS 证书校验被关闭（仅限测试/内网自签场景）"
            )

        self._client = OpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=(settings.opensearch_user, settings.opensearch_password),
            use_ssl=settings.opensearch_url.startswith("https://"),
            verify_certs=verify_certs,
        )
        return self._client

    def _search_sync(
        self,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None,
    ) -> list[CaseResult]:
        filter_clauses = [{"term": {key: value}} for key, value in (filters or {}).items()]
        response = self._get_client().search(
            index=self._index_name,
            body={
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": [
                                        "title^3",
                                        "summary^2",
                                        "full_text",
                                        "case_type",
                                        "court",
                                    ],
                                }
                            }
                        ],
                        "filter": filter_clauses,
                    }
                },
            },
        )
        hits = response.get("hits", {}).get("hits", [])
        results: list[CaseResult] = []
        for hit in hits:
            source = hit.get("_source", {})
            if not isinstance(source, dict):
                continue
            metadata = {key: value for key, value in source.items() if key not in _CASE_FIELDS}
            results.append(
                CaseResult(
                    case_id=str(source.get("case_id") or hit.get("_id", "")),
                    title=str(source.get("title", "")),
                    court=str(source.get("court", "")),
                    date=str(source.get("date", "")),
                    case_type=str(source.get("case_type", "")),
                    summary=str(source.get("summary", "")),
                    full_text=str(source.get("full_text", "")),
                    source="opensearch",
                    score=float(hit.get("_score") or 0.0),
                    metadata=metadata,
                )
            )
        return results

    async def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[CaseResult]:
        import asyncio

        try:
            return await asyncio.to_thread(self._search_sync, query, top_k, filters)
        except Exception as exc:  # noqa: BLE001 boundary-exception: optional data source
            _logger.warning("OpenSearch 案例检索失败: %s", exc)
            return []

    async def healthcheck(self) -> bool:
        import asyncio

        try:
            return bool(await asyncio.to_thread(self._get_client().ping))
        except Exception as exc:  # noqa: BLE001 boundary-exception: optional data source
            _logger.warning("OpenSearch 案例库健康检查失败: %s", exc)
            return False


_CASE_FIELDS = frozenset({"case_id", "title", "court", "date", "case_type", "summary", "full_text"})


class MultiSourceRetriever:
    """多源聚合检索器。

    聚合多个 CaseSource 的结果，支持：
    - 并发检索所有源
    - 按权重调整得分
    - 跨源去重（基于 case_id）
    - 融合排序
    """

    def __init__(
        self,
        sources: Sequence[CaseSource] | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._sources: list[CaseSource] = list(sources) if sources else []
        self._weights = weights or {}

    def add_source(self, source: CaseSource, weight: float = 1.0) -> None:
        """添加数据源。"""
        self._sources.append(source)
        self._weights[source.name] = weight

    async def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[CaseResult]:
        """并发检索所有源，融合排序返回。"""
        import asyncio

        if not self._sources:
            return []

        # 并发检索所有源
        tasks = [source.search(query, top_k=top_k, filters=filters) for source in self._sources]
        results_per_source = await asyncio.gather(*tasks, return_exceptions=True)

        # 聚合、加权、去重
        seen_ids: set[str] = set()
        all_results: list[CaseResult] = []

        for source, results in zip(self._sources, results_per_source):
            if isinstance(results, Exception):
                _logger.warning("数据源 %s 检索失败: %s", source.name, results)
                continue

            weight = self._weights.get(source.name, 1.0)
            for r in results:
                if r.case_id and r.case_id in seen_ids:
                    continue
                if r.case_id:
                    seen_ids.add(r.case_id)
                r.score *= weight
                all_results.append(r)

        # 按加权得分排序
        all_results.sort(key=lambda r: r.score, reverse=True)
        return all_results[:top_k]

    async def healthcheck(self) -> dict[str, bool]:
        """所有源健康状态（并发检查，各源保留独立 5s 超时）。"""
        import asyncio

        if not self._sources:
            return {}

        async def _check_one(coro):
            try:
                return await asyncio.wait_for(coro, timeout=5.0)
            except Exception:  # noqa: BLE001 boundary-exception: 健康检查不阻断
                return False

        outcomes = await asyncio.gather(
            *(_check_one(source.healthcheck()) for source in self._sources)
        )
        return {source.name: ok for source, ok in zip(self._sources, outcomes)}
