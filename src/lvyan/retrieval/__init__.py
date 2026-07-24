"""检索层：词法召回、稠密向量、混合检索、重排序、查询改写、法规版本解析。"""

from __future__ import annotations

from .case_rule import case_rule_search, detect_case_keywords
from .dense import dense_search, dense_search_bge_m3, embed_text
from .exact_match import article_no_search, extract_article_refs
from .hybrid import hybrid_search
from .lexical import ScoredChunk, bm25_search, search
from .query_rewriter import rewrite_for_reretrieval, rewrite_query
from .reranker import rerank
from .version_aware import (
    StatuteVerification,
    search_statutes,
    verify_statute_status,
)
from .version_resolver import (
    AuthorityStatus,
    LawMetadata,
    VersionGroup,
    build_version_groups,
    compute_content_hash,
    find_law_files_by_title,
    mark_current_effective,
    parse_law_metadata,
    scan_all_laws,
    status_distribution,
)

__all__ = [
    # 法规版本解析
    "AuthorityStatus",
    "LawMetadata",
    "VersionGroup",
    "parse_law_metadata",
    "build_version_groups",
    "mark_current_effective",
    "scan_all_laws",
    "find_law_files_by_title",
    "compute_content_hash",
    "status_distribution",
    # Task 8：四路混合检索
    "ScoredChunk",
    "bm25_search",
    "search",
    "dense_search",
    "dense_search_bge_m3",
    "embed_text",
    "article_no_search",
    "extract_article_refs",
    "case_rule_search",
    "detect_case_keywords",
    "hybrid_search",
    "rerank",
    "rewrite_query",
    "rewrite_for_reretrieval",
    # Task 9：版本感知检索接口（retrieval 层核心接口）
    # 注意：tools/statutes.py 中的同名函数是工具层包装，
    # 从 retrieval 层导出的版本返回 Authority / StatuteVerification 领域模型。
    "search_statutes",
    "verify_statute_status",
    "StatuteVerification",
]
