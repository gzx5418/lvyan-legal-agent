"""法规版本感知检索接口（Task 9）。

封装 Task 8 的 :func:`hybrid_search`，提供 ``as_of`` 历史时间点查询能力，
并将检索结果转换为 :class:`Authority` 模型，便于上层（Citation Verifier、
工具层）直接消费。

与 ``lvyan.tools.statutes`` 的关系：
  - 本模块是 retrieval 层核心接口，返回 ``Authority`` / ``StatuteVerification``
    领域模型，不含工具层 ``ToolResult`` 包装。
  - ``tools/statutes.py`` 中的同名函数是工具层包装，可后续改为调用本接口。

公开接口：
    search_statutes(query, jurisdiction="中国大陆", as_of=None,
                    only_effective=True, top_k=10) -> list[Authority]
    verify_statute_status(source_id, as_of=None) -> StatuteVerification
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from lvyan.retrieval.hybrid import hybrid_search
from lvyan.retrieval.lexical import ScoredChunk
from lvyan.retrieval.version_resolver import (
    AuthorityStatus,
    LawMetadata,
    VersionGroup,
    build_version_groups,
    scan_all_laws,
)
from lvyan.schemas.authority import Authority


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class StatuteVerification(BaseModel):
    """法规有效性核验结果（retrieval 层领域模型）。

    与 ``tools/statutes.StatuteStatusResult`` 字段对齐但不含 ``ToolResult``
    包装，便于在 retrieval 层与上层共享。
    """

    source_id: str
    title: str
    current_status: AuthorityStatus
    effective_date: date | None = None
    expiry_date: date | None = None
    is_effective_as_of: bool = False
    superseded_by: str | None = None
    official_source: str | None = None
    checked_at: datetime


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _parse_as_of(as_of: str | date | None) -> date | None:
    """解析 ``as_of`` 参数为 ``date``，支持 str / date / None。

    - ``None`` → ``None``（不按时间过滤）
    - ``date`` 对象 → 原样返回
    - ``str`` → 尝试 ``YYYY-MM-DD`` 与 ``YYYY/MM/DD`` 两种格式，解析失败返回 ``None``
    """
    if as_of is None:
        return None
    if isinstance(as_of, date):
        return as_of
    if isinstance(as_of, str):
        s = as_of.strip()
        if not s:
            return None
        # 截取前 10 位以兼容 datetime 字符串（如 "2024-01-01T00:00:00"）
        head = s[:10]
        try:
            return date.fromisoformat(head)
        except ValueError:
            try:
                return date.fromisoformat(head.replace("/", "-"))
            except ValueError:
                return None
    return None


def _chunk_attr(chunk: Any, name: str, default: Any = None) -> Any:
    """从 chunk（ArticleChunk 实例或 dict）读取字段，兼容两种形态。"""
    if isinstance(chunk, dict):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _passes_version_filter(
    authority: Authority,
    as_of_date: date | None,
    only_effective: bool,
) -> bool:
    """``as_of`` + ``only_effective`` 二次校验。

    ``hybrid_search`` 已做一次过滤；此处兜底防止边角情况漏过：
      - ``only_effective=True`` 时要求 ``status == "effective"``
      - ``as_of`` 给定时要求 ``effective_date <= as_of``（None 视为未知，保留）
    """
    if only_effective and authority.status != "effective":
        return False
    if as_of_date is not None and authority.effective_date is not None:
        if authority.effective_date > as_of_date:
            return False
    return True


# ---------------------------------------------------------------------------
# 元数据缓存（避免每次 verify_statute_status 都重扫全库）
# ---------------------------------------------------------------------------
_metadata_cache: dict[str, LawMetadata] | None = None
_groups_cache: list[VersionGroup] | None = None


def _load_metadata_map() -> dict[str, LawMetadata]:
    """扫描全库法规元数据并构建 ``source_id -> LawMetadata`` 映射。

    同时构建 ``VersionGroup`` 列表，把 ``superseded`` 标记写回各 ``LawMetadata``
    （``build_version_groups`` 原地修改 versions），供 :func:`verify_statute_status`
    判断「是否被取代」使用。结果全局缓存，仅扫描一次。
    """
    global _metadata_cache, _groups_cache
    if _metadata_cache is not None:
        return _metadata_cache

    metas = scan_all_laws()
    _metadata_cache = {m.source_id: m for m in metas}
    try:
        _groups_cache = build_version_groups(metas)
    except Exception:
        _groups_cache = []
    return _metadata_cache


def _reset_metadata_cache() -> None:
    """重置元数据缓存（仅测试使用）。"""
    global _metadata_cache, _groups_cache
    _metadata_cache = None
    _groups_cache = None


# ---------------------------------------------------------------------------
# SubTask 9.1: search_statutes 版本感知接口
# ---------------------------------------------------------------------------
def search_statutes(
    query: str,
    jurisdiction: str = "中国大陆",
    as_of: str | date | None = None,
    only_effective: bool = True,
    top_k: int = 10,
) -> list[Authority]:
    """版本感知法规检索：封装 :func:`hybrid_search` 并转换为 :class:`Authority`。

    Args:
        query: 用户查询（自然语言）
        jurisdiction: 管辖区域，默认「中国大陆」。当前仅支持中国大陆，参数
            保留供未来扩展。
        as_of: 历史时间点（``"YYYY-MM-DD"`` 字符串或 ``date`` 对象）。仅保留
            ``effective_date <= as_of`` 的版本；``effective_date`` 为 ``None``
            的版本视为未知生效日期，予以保留。``None`` 时不按时间过滤。
        only_effective: ``True`` 时仅保留 ``status == "effective"`` 的结果。
        top_k: 返回结果数上限。

    Returns:
        list[Authority]：按 RRF 融合分数降序，最多 ``top_k`` 条。每条
        ``Authority`` 的 ``lexical_score`` 填入融合分数，``dense_score`` /
        ``rerank_score`` 暂置 ``0.0``（因当前 ``hybrid_search`` 已融合四路分数）。

    性能：
        复用 :func:`hybrid_search` 的全局 chunks / BM25 索引缓存，单次 < 10s。
    """
    if not query or not query.strip():
        return []

    as_of_date = _parse_as_of(as_of)

    # 多取一倍用于过滤后仍有足够结果；下限 20 防止 top_k 过小时召回不足
    fetch_k = max(top_k * 2, 20)
    scored_chunks: list[ScoredChunk] = hybrid_search(
        query=query,
        top_k=fetch_k,
        only_effective=only_effective,
        as_of=as_of_date,
    )

    results: list[Authority] = []
    for sc in scored_chunks:
        chunk = sc.chunk
        article_text = _chunk_attr(chunk, "article_text", "") or ""
        if not article_text:
            # 跳过无正文的异常 chunk
            continue

        # 处理 article_number：ArticleChunk 用空串，Authority 用 None
        article_number = _chunk_attr(chunk, "article_number", "") or ""
        article_number = article_number if article_number else None

        authority = Authority(
            source_id=_chunk_attr(chunk, "source_id", "") or "",
            title=_chunk_attr(chunk, "title", "") or "",
            article_number=article_number,
            article_text=article_text,
            authority_level=_chunk_attr(chunk, "authority_level", "") or "其他",
            publication_date=_chunk_attr(chunk, "publication_date", None),
            effective_date=_chunk_attr(chunk, "effective_date", None),
            status=_chunk_attr(chunk, "status", "unknown") or "unknown",
            jurisdiction=_chunk_attr(chunk, "jurisdiction", jurisdiction) or jurisdiction,
            official_source=_chunk_attr(chunk, "official_source", None),
            content_hash=_chunk_attr(chunk, "content_hash", None),
            retrieved_at=datetime.now(),
            # hybrid_search 的 score 是 RRF 融合分数，统一填到 lexical_score；
            # dense / reranker 路已融合进 RRF，单独分数不再可分，置 0.0
            lexical_score=float(sc.score) if sc.score is not None else 0.0,
            dense_score=0.0,
            rerank_score=0.0,
        )

        # 二次校验：hybrid_search 已做过滤，这里防止任何边角情况漏过
        if not _passes_version_filter(authority, as_of_date, only_effective):
            continue

        results.append(authority)

        if len(results) >= top_k:
            break

    return results


# ---------------------------------------------------------------------------
# SubTask 9.2: verify_statute_status 接口
# ---------------------------------------------------------------------------
def verify_statute_status(
    source_id: str,
    as_of: str | date | None = None,
) -> StatuteVerification:
    """核验指定法规在 ``as_of`` 时间点的有效性。

    Args:
        source_id: 法规标识（官方法律库文件的 stem，即 ``LawMetadata.source_id``）
        as_of: 时间点（``"YYYY-MM-DD"`` 或 ``date`` 对象）。``None`` 时按当前
            有效性判断。

    Returns:
        StatuteVerification：含 ``current_status`` / ``effective_date`` /
        ``expiry_date`` / ``is_effective_as_of`` / ``superseded_by`` 等。

    有效性判定规则：
      - ``as_of`` 给定：``effective_date <= as_of`` 且（``expiry_date`` 为
        ``None`` 或 ``expiry_date > as_of``）且 ``status == "effective"``
      - ``as_of`` 为 ``None``：``status == "effective"`` 且未被取代
        （``superseded == False``）

    说明：当前 ``LawMetadata`` 未从 front matter 解析 ``expiry_date`` 字段，
    故 ``expiry_date`` 统一返回 ``None``，待后续补全解析逻辑。
    """
    checked_at = datetime.now()

    if not source_id:
        return StatuteVerification(
            source_id="",
            title="",
            current_status="unknown",
            checked_at=checked_at,
        )

    meta_map = _load_metadata_map()
    meta = meta_map.get(source_id)

    if meta is None:
        return StatuteVerification(
            source_id=source_id,
            title="",
            current_status="unknown",
            is_effective_as_of=False,
            checked_at=checked_at,
        )

    as_of_date = _parse_as_of(as_of)

    # expiry_date：LawMetadata 暂未解析该字段，统一 None
    expiry_date: date | None = None

    # 计算有效性
    if as_of_date is not None:
        # 时间点查询：三条件全满足才有效
        if meta.status != "effective":
            is_effective = False
        elif meta.effective_date is not None and meta.effective_date > as_of_date:
            is_effective = False
        elif expiry_date is not None and expiry_date <= as_of_date:
            is_effective = False
        else:
            is_effective = True
    else:
        # 当前有效性：status=effective 且未被 superseded
        is_effective = meta.status == "effective" and not meta.superseded

    # 查找取代关系：从已构建的 VersionGroup 中查同标题的 current_effective
    superseded_by: str | None = None
    groups = _groups_cache or []
    for group in groups:
        if group.title == meta.title:
            current = group.current_effective
            if (
                current is not None
                and current.source_id != source_id
                and meta.status == "effective"
            ):
                superseded_by = current.source_id
            break

    official_source = meta.official_urls[0] if meta.official_urls else None

    return StatuteVerification(
        source_id=meta.source_id,
        title=meta.title,
        current_status=meta.status,
        effective_date=meta.effective_date,
        expiry_date=expiry_date,
        is_effective_as_of=is_effective,
        superseded_by=superseded_by,
        official_source=official_source,
        checked_at=checked_at,
    )


__all__ = [
    "StatuteVerification",
    "search_statutes",
    "verify_statute_status",
]
