"""法规工具：法规检索、条文查询、有效性核验。

本模块是 SubTask 15.1 的实现，对外提供三个标准工具：

  - ``search_statutes(query, ...)``：基于 ``retrieval.lexical.search`` 的词汇检索，
    支持 ``as_of`` 时间点过滤与 ``only_effective`` 状态过滤。
  - ``get_statute_article(source_id, article_number)``：从条文级索引中查询
    指定法规的指定条文。
  - ``verify_statute_status(source_id, as_of=...)``：用 ``version_resolver`` 查询
    法规当前有效性，含被取代关系。

注：``search_statutes`` 后端可切换到 ``retrieval.hybrid_search``，
当前保留 lexical 搜索作为稳定后端。
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lvyan.config import AGENT_DIR, LAWTEXT_DIR
from lvyan.retrieval import lexical
from lvyan.retrieval.version_resolver import (
    LawMetadata,
    build_version_groups,
    parse_law_metadata,
    scan_all_laws,
)
from lvyan.scripts.ingest_laws import ArticleChunk, build_article_index
from lvyan.tools.base import ToolResult

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_ARTICLE_RE = re.compile(r"^(?:-\s*\*\*)?(第[一二三四五六七八九十百千零0-9]+条)(?:\*\*)?")
"""从检索命中行中提取「第X条」标记的正则，与 ingest_laws 保持一致。"""

_EXCERPT_LEN = 200
"""StatuteHit.article_text 截断长度（字符数）。"""


# ---------------------------------------------------------------------------
# 返回模型
# ---------------------------------------------------------------------------
class StatuteHit(BaseModel):
    """单条法规命中结果。"""

    source_id: str
    title: str
    article_number: str = ""
    article_text: str  # excerpt 前 200 字
    status: str = "unknown"  # effective / repealed / not_yet_effective / unknown
    effective_date: date | None = None
    official_source: str | None = None
    score: float = 0.0


class StatuteSearchResult(ToolResult):
    """法规检索结果。"""

    query: str
    total: int = 0
    results: list[StatuteHit] = Field(default_factory=list)
    filtered_out_count: int = 0


class StatuteArticleResult(ToolResult):
    """单条法规条文查询结果。"""

    source_id: str
    title: str = ""
    article_number: str = ""
    article_text: str = ""
    chapter: str | None = None
    section: str | None = None
    status: str = "unknown"
    effective_date: date | None = None
    official_source: str | None = None
    found: bool = False


class StatuteStatusResult(ToolResult):
    """法规有效性核验结果。"""

    source_id: str
    title: str = ""
    current_status: str = "unknown"
    effective_date: date | None = None
    expiry_date: date | None = None
    is_effective_as_of: bool = False
    superseded_by: str | None = None
    official_source: str | None = None


# ---------------------------------------------------------------------------
# 内部缓存
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _cached_article_index() -> tuple[ArticleChunk, ...]:
    """构建并缓存全库条文级索引（仅扫描一次 LAWTEXT_DIR）。

    返回 tuple 以保持 hashable 与 LRU 兼容；调用方按 source_id/article_number
    线性扫描或自建 dict 索引。
    """
    if not LAWTEXT_DIR.is_dir():
        return ()
    return tuple(build_article_index())


@lru_cache(maxsize=1)
def _cached_metadata_by_source() -> dict[str, LawMetadata]:
    """构建 source_id -> LawMetadata 的映射缓存。"""
    if not LAWTEXT_DIR.is_dir():
        return {}
    return {m.source_id: m for m in scan_all_laws(LAWTEXT_DIR)}


def _parse_as_of(as_of: str | None) -> date | None:
    """解析 'YYYY-MM-DD' 字符串为 date，非法时返回 None。"""
    if not as_of:
        return None
    try:
        return date.fromisoformat(as_of)
    except ValueError:
        return None


def _extract_article_number(line: str) -> str:
    """从一行文本中提取「第X条」标记，未命中返回空串。"""
    m = _ARTICLE_RE.match(line.strip())
    return m.group(1) if m else ""


def _build_hit_from_lexical_match(file_entry: dict[str, Any]) -> StatuteHit | None:
    """将 lexical.search 的单条结果转为 StatuteHit。

    - 官方库文件：解析 front matter 取真实 source_id / title / status / effective_date
    - 知识库文件：用文件名作 title、source_id，status 默认 effective
    """
    rel_path = file_entry.get("path", "")
    file_name = file_entry.get("file", "")
    source_tag = file_entry.get("source", "")
    max_score = float(file_entry.get("max_score", 0.0))

    top_matches = file_entry.get("top_matches") or []
    top_line = top_matches[0]["content"] if top_matches else ""
    article_number = _extract_article_number(top_line)
    article_text = top_line[:_EXCERPT_LEN] if top_line else ""

    # 官方库：尝试解析 front matter 拿真实元数据
    if source_tag == "official" and rel_path:
        candidate = Path(rel_path)
        if not candidate.is_absolute():
            candidate = AGENT_DIR / candidate
        if not candidate.is_file():
            # lexical.path 是相对 AGENT_DIR 的；也可能相对仓库根
            candidate = AGENT_DIR.parent / rel_path
        if candidate.is_file():
            try:
                meta = parse_law_metadata(candidate)
                return StatuteHit(
                    source_id=meta.source_id,
                    title=meta.title,
                    article_number=article_number,
                    article_text=article_text,
                    status=meta.status,
                    effective_date=meta.effective_date,
                    official_source=meta.official_urls[0] if meta.official_urls else None,
                    score=max_score,
                )
            except Exception:
                pass

    # 知识库 / 元数据解析失败：回退到文件名兜底
    stem = Path(file_name).stem if file_name else rel_path
    return StatuteHit(
        source_id=stem,
        title=Path(file_name).stem if file_name else stem,
        article_number=article_number,
        article_text=article_text,
        status="effective",  # 精编知识库默认视为现行有效
        effective_date=None,
        official_source=None,
        score=max_score,
    )


# ---------------------------------------------------------------------------
# 公开工具
# ---------------------------------------------------------------------------
def search_statutes(
    query: str,
    jurisdiction: str = "中国大陆",
    as_of: str | None = None,
    only_effective: bool = True,
    top_k: int = 10,
) -> StatuteSearchResult:
    """法规检索：基于词汇检索返回最相关的法规条文命中。

    Args:
        query: 自然语言查询（如「被公司辞退怎么赔偿」）
        jurisdiction: 管辖区域，默认「中国大陆」。当前仅支持中国大陆。
        as_of: 时间点过滤（"YYYY-MM-DD"），仅保留 effective_date <= as_of 的版本。
            未指定时不按时间过滤。
        only_effective: True 时仅保留 status == "effective" 的结果。
        top_k: 返回结果数上限。

    Returns:
        StatuteSearchResult：含 query / total / results / filtered_out_count。
    """
    if not query or not query.strip():
        return StatuteSearchResult(
            tool_name="search_statutes",
            success=False,
            error="查询不能为空",
            query=query,
            total=0,
            results=[],
            filtered_out_count=0,
        )

    try:
        # 当前使用 lexical.search 作为后端；可切换到 hybrid_search 提升召回
        raw_hits = lexical.search(query, search_type="all", top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        return StatuteSearchResult(
            tool_name="search_statutes",
            success=False,
            error=f"检索后端异常：{exc}",
            query=query,
            total=0,
            results=[],
            filtered_out_count=0,
        )

    as_of_date = _parse_as_of(as_of)
    all_hits: list[StatuteHit] = []
    filtered_out = 0

    for file_entry in raw_hits:
        hit = _build_hit_from_lexical_match(file_entry)
        if hit is None:
            continue
        all_hits.append(hit)

    # 过滤
    kept: list[StatuteHit] = []
    for hit in all_hits:
        if only_effective and hit.status != "effective":
            filtered_out += 1
            continue
        if as_of_date is not None and hit.effective_date is not None:
            if hit.effective_date > as_of_date:
                filtered_out += 1
                continue
        kept.append(hit)

    kept = kept[:top_k]

    return StatuteSearchResult(
        tool_name="search_statutes",
        success=True,
        query=query,
        total=len(kept),
        results=kept,
        filtered_out_count=filtered_out,
    )


def get_statute_article(source_id: str, article_number: str) -> StatuteArticleResult:
    """查询指定法规的指定条文全文。

    Args:
        source_id: 法规标识（官方法律库的文件 stem，或精编知识库的文件名 stem）
        article_number: 条文标记，如「第十条」「第一条」。空串时返回法规首条或整文。

    Returns:
        StatuteArticleResult：found=True 表示命中，article_text 含条文全文。
    """
    if not source_id:
        return StatuteArticleResult(
            tool_name="get_statute_article",
            success=False,
            error="source_id 不能为空",
            source_id=source_id,
            article_number=article_number,
        )

    try:
        chunks = _cached_article_index()
    except Exception as exc:  # noqa: BLE001
        return StatuteArticleResult(
            tool_name="get_statute_article",
            success=False,
            error=f"条文索引构建失败：{exc}",
            source_id=source_id,
            article_number=article_number,
        )

    target = article_number.strip()
    for chunk in chunks:
        if chunk.source_id != source_id:
            continue
        if target and chunk.article_number != target:
            continue
        return StatuteArticleResult(
            tool_name="get_statute_article",
            success=True,
            source_id=chunk.source_id,
            title=chunk.title,
            article_number=chunk.article_number,
            article_text=chunk.article_text,
            chapter=chunk.chapter,
            section=chunk.section,
            status=chunk.status,
            effective_date=chunk.effective_date,
            official_source=chunk.official_source,
            found=True,
        )

    return StatuteArticleResult(
        tool_name="get_statute_article",
        success=True,  # 调用本身成功，只是未命中
        source_id=source_id,
        article_number=article_number,
        found=False,
        error=None,
    )


def verify_statute_status(source_id: str, as_of: str | None = None) -> StatuteStatusResult:
    """核验指定法规的当前有效性。

    Args:
        source_id: 法规标识（官方法律库的文件 stem）
        as_of: 时间点（"YYYY-MM-DD"），用于判断「在该日期是否有效」。

    Returns:
        StatuteStatusResult：含 current_status / effective_date / is_effective_as_of /
        superseded_by 等。
    """
    if not source_id:
        return StatuteStatusResult(
            tool_name="verify_statute_status",
            success=False,
            error="source_id 不能为空",
            source_id=source_id,
        )

    try:
        meta_map = _cached_metadata_by_source()
    except Exception as exc:  # noqa: BLE001
        return StatuteStatusResult(
            tool_name="verify_statute_status",
            success=False,
            error=f"元数据扫描失败：{exc}",
            source_id=source_id,
        )

    meta = meta_map.get(source_id)
    if meta is None:
        return StatuteStatusResult(
            tool_name="verify_statute_status",
            success=True,
            source_id=source_id,
            current_status="unknown",
            is_effective_as_of=False,
            error=None,
        )

    as_of_date = _parse_as_of(as_of)

    # 判断 as_of 时间点是否有效：status=effective 且 effective_date <= as_of（若提供）
    if as_of_date is not None:
        if meta.effective_date is not None and meta.effective_date > as_of_date:
            is_effective = False
        else:
            is_effective = meta.status == "effective"
    else:
        is_effective = meta.status == "effective"

    # 查找取代关系：同标题下是否有更新的 effective 版本
    superseded_by: str | None = None
    try:
        same_title_metas = [m for m in meta_map.values() if m.title == meta.title]
        if len(same_title_metas) > 1:
            groups = build_version_groups(same_title_metas)
            if groups:
                current = groups[0].current_effective
                if (
                    current is not None
                    and current.source_id != source_id
                    and meta.status == "effective"
                ):
                    superseded_by = current.source_id
    except Exception:
        pass

    official_source = meta.official_urls[0] if meta.official_urls else None

    return StatuteStatusResult(
        tool_name="verify_statute_status",
        success=True,
        source_id=meta.source_id,
        title=meta.title,
        current_status=meta.status,
        effective_date=meta.effective_date,
        expiry_date=None,  # 当前未从 front matter 解析 expiry
        is_effective_as_of=is_effective,
        superseded_by=superseded_by,
        official_source=official_source,
    )


__all__ = [
    "StatuteHit",
    "StatuteSearchResult",
    "StatuteArticleResult",
    "StatuteStatusResult",
    "search_statutes",
    "get_statute_article",
    "verify_statute_status",
]
