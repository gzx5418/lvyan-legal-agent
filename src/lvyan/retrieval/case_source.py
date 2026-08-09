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
        import os
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
            searchable = " ".join([
                case.get("title", ""),
                case.get("summary", ""),
                case.get("case_type", ""),
                case.get("court", ""),
            ]).lower()

            # 简单 TF 评分
            for term in query_lower.split():
                if term in searchable:
                    score += 1.0

            if score > 0:
                scored.append((score, case))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for score, case in scored[:top_k]:
            results.append(CaseResult(
                case_id=case.get("case_id", case.get("id", "")),
                title=case.get("title", ""),
                court=case.get("court", ""),
                date=case.get("date", ""),
                case_type=case.get("case_type", ""),
                summary=case.get("summary", ""),
                source="curated",
                score=score,
                metadata=case.get("metadata", {}),
            ))

        return results

    async def healthcheck(self) -> bool:
        self._load()
        return True


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
        tasks = [
            source.search(query, top_k=top_k, filters=filters)
            for source in self._sources
        ]
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
        """所有源健康状态。"""
        import asyncio

        results = {}
        checks = [(s.name, s.healthcheck()) for s in self._sources]
        for name, coro in checks:
            try:
                results[name] = await asyncio.wait_for(coro, timeout=5.0)
            except Exception:  # noqa: BLE001 boundary-exception: 健康检查不阻断
                results[name] = False
        return results
