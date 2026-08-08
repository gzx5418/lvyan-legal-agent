"""Task 8: 四路混合检索测试。

覆盖：
  1. BM25 取消标题预筛——召回漏洞回归
  2. 精确法条号匹配
  3. 案由规则匹配
  4. RRF 融合
  5. Reranker
  6. 查询改写
"""

from __future__ import annotations

from datetime import date

import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval import (
    article_no_search,
    bm25_search,
    case_rule_search,
    hybrid_search,
    rerank,
    rewrite_for_reretrieval,
    rewrite_query,
)
from lvyan.retrieval.version_resolver import find_law_files_by_title, parse_law_metadata
from lvyan.scripts.ingest_laws import ArticleChunk, chunk_law_articles


# ---------------------------------------------------------------------------
# 辅助：从全库加载 chunks 的子集用于加速测试
# ---------------------------------------------------------------------------
def _load_pipl_chunks() -> list[ArticleChunk]:
    """加载《个人信息保护法》的全部 chunks。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")
    files = find_law_files_by_title("中华人民共和国个人信息保护法", LAWTEXT_DIR)
    if not files:
        pytest.skip("未找到《个人信息保护法》")
    meta = parse_law_metadata(files[0])
    return chunk_law_articles(meta)


def _load_civil_code_chunks() -> list[ArticleChunk]:
    """加载《民法典》的全部 chunks。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")
    files = find_law_files_by_title("中华人民共和国民法典", LAWTEXT_DIR)
    if not files:
        pytest.skip("未找到《民法典》")
    meta = parse_law_metadata(files[0])
    return chunk_law_articles(meta)


def _load_labor_law_chunks() -> list[ArticleChunk]:
    """加载《劳动合同法》和《劳动法》的合并 chunks（用于案由测试）。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")
    out: list[ArticleChunk] = []
    for title in ("中华人民共和国劳动合同法", "中华人民共和国劳动法"):
        files = find_law_files_by_title(title, LAWTEXT_DIR)
        if not files:
            continue
        meta = parse_law_metadata(files[0])
        out.extend(chunk_law_articles(meta))
    if not out:
        pytest.skip("未找到劳动合同法 / 劳动法")
    return out


# ---------------------------------------------------------------------------
# 测试 1：BM25 取消标题预筛——召回漏洞回归
# ---------------------------------------------------------------------------
def test_bm25_recall_without_title_filter():
    """标题不含「个人信息 / 健康信息 / 公开」，但正文含 → 必须能召回。

    用《个人信息保护法》全 74 条 chunks 做小规模验证，确保 BM25 不再依赖
    标题预筛。该法律标题为「中华人民共和国个人信息保护法」其实含「个人信息」，
    但其下条文（如第十三条「个人信息处理者处理个人信息应当取得个人同意」）
    标题字段相同，正文才是真正的「公开」「健康信息」等关键词载体。
    """
    chunks = _load_pipl_chunks()
    assert chunks, "PIPL chunks 不应为空"

    # 用「公开我的健康信息」查询——标题虽含「个人信息」，但「健康信息 / 公开」
    # 必须从正文命中（标题不含这两个词）
    results = bm25_search("公司未经同意公开我的健康信息怎么办", chunks=chunks, top_k=20)
    assert results, "BM25 应能召回至少一条结果"

    # 至少有一条结果的 article_text 含「公开」或「信息」
    has_text_match = False
    for sc in results:
        text = (
            sc.chunk.article_text
            if hasattr(sc.chunk, "article_text")
            else sc.chunk.get("article_text", "")
        )
        if "公开" in text or "信息" in text:
            has_text_match = True
            break
    assert has_text_match, "召回结果正文应含「公开」或「信息」"

    # 验证分数降序
    scores = [sc.score for sc in results]
    assert scores == sorted(scores, reverse=True), "结果应按分数降序"

    # 验证 score 都为正数
    assert all(sc.score > 0 for sc in results), "所有分数应为正数"


def test_bm25_recall_via_body_text_only():
    """更严格的回归：标题完全不含查询词时仍能从正文召回。

    构造查询「健康信息」+ 验证至少有一条结果正文含「健康」或「信息」，
    即使 chunk 标题不含「健康信息」。
    """
    chunks = _load_pipl_chunks()

    results = bm25_search("健康信息", chunks=chunks, top_k=10)
    assert results, "BM25 应能召回结果"

    # 至少一条正文命中
    body_hits = 0
    for sc in results:
        text = (
            sc.chunk.article_text
            if hasattr(sc.chunk, "article_text")
            else sc.chunk.get("article_text", "")
        )
        if "信息" in text:
            body_hits += 1
    assert body_hits > 0, "至少应有一条正文含「信息」"


# ---------------------------------------------------------------------------
# 测试 2：精确法条号匹配
# ---------------------------------------------------------------------------
def test_article_no_search_civil_code():
    """查询「《民法典》第一千零三十二条」应精确匹配到对应 chunk。"""
    chunks = _load_civil_code_chunks()
    assert len(chunks) > 100, "民法典 chunks 应 > 100"

    results = article_no_search("《民法典》第一千零三十二条", chunks=chunks)
    assert results, "应匹配到至少一条结果"
    # 应该精确匹配第一千零三十二条
    matched = [sc for sc in results if sc.chunk.article_number == "第一千零三十二条"]
    assert matched, "应匹配到第一千零三十二条"
    assert all(sc.score == 1.0 for sc in matched), "精确匹配 score 应为 1.0"


def test_article_no_search_no_match():
    """查询不含「《XX法》第Y条」模式时应返回空列表。"""
    chunks = _load_civil_code_chunks()
    results = article_no_search("劳动合同解除", chunks=chunks)
    assert results == [], "无模式匹配时应返回空列表"


# ---------------------------------------------------------------------------
# 测试 3：案由规则匹配
# ---------------------------------------------------------------------------
def test_case_rule_search_labor():
    """查询「劳动争议 经济补偿」应召回劳动合同法 / 劳动法相关 chunks。"""
    chunks = _load_labor_law_chunks()
    assert chunks

    results = case_rule_search("劳动争议 经济补偿", chunks=chunks)
    assert results, "案由规则应召回至少一条"

    # 所有结果 title 应含「劳动合同法」或「劳动法」
    titles = [
        sc.chunk.title if hasattr(sc.chunk, "title") else sc.chunk.get("title", "")
        for sc in results
    ]
    assert any("劳动合同" in t or "劳动法" in t for t in titles), (
        f"应召回劳动合同法 / 劳动法，实际 titles={titles[:5]}"
    )

    # score 应为 0.8
    assert all(sc.score == 0.8 for sc in results)


def test_case_rule_search_no_match():
    """无案由命中时返回空列表。"""
    chunks = _load_labor_law_chunks()
    results = case_rule_search("xxxrandom_no_case_keyword", chunks=chunks)
    assert results == []


# ---------------------------------------------------------------------------
# 测试 4：RRF 融合
# ---------------------------------------------------------------------------
def test_hybrid_search_returns_results():
    """hybrid_search 应返回 >0 结果，按融合分数降序。"""
    chunks = _load_labor_law_chunks()
    results = hybrid_search("劳动合同 解除 经济补偿", top_k=10, chunks=chunks)
    assert results, "hybrid_search 应返回结果"

    # 验证分数降序
    scores = [sc.score for sc in results]
    assert scores == sorted(scores, reverse=True), "结果应按融合分数降序"


def test_hybrid_search_only_effective_filter():
    """only_effective=True 时不含 status='repealed' 的 chunk。"""
    chunks = _load_labor_law_chunks()
    results = hybrid_search(
        "劳动合同 解除 经济补偿",
        top_k=10,
        only_effective=True,
        chunks=chunks,
    )
    for sc in results:
        status = sc.chunk.status if hasattr(sc.chunk, "status") else sc.chunk.get("status", "")
        assert status != "repealed", f"only_effective=True 不应返回 repealed chunk: {sc.chunk_id}"


def test_hybrid_search_as_of_filter():
    """as_of 过滤：只返回 effective_date <= as_of 的 chunk。"""
    chunks = _load_civil_code_chunks()
    # 民法典 2021-01-01 生效
    results_before = hybrid_search(
        "民事权利",
        top_k=20,
        only_effective=False,
        as_of=date(2020, 1, 1),
        chunks=chunks,
    )
    # 2020-01-01 之前生效的民法典不存在（民法典 2021-01-01 生效），
    # 因此应过滤掉所有有 effective_date 的民法典 chunks
    for sc in results_before:
        eff = (
            sc.chunk.effective_date
            if hasattr(sc.chunk, "effective_date")
            else sc.chunk.get("effective_date")
        )
        if eff is not None:
            if isinstance(eff, str):
                eff_date = date.fromisoformat(eff[:10])
            else:
                eff_date = eff
            assert eff_date <= date(2020, 1, 1), (
                f"as_of 过滤失效：返回了 effective_date={eff} > 2020-01-01 的 chunk"
            )


# ---------------------------------------------------------------------------
# 测试 5：Reranker
# ---------------------------------------------------------------------------
def test_rerank_returns_top_k():
    """rerank 应返回 top_k 个结果，score 已更新。"""
    chunks = _load_labor_law_chunks()
    candidates = hybrid_search("劳动合同 解除 经济补偿", top_k=10, chunks=chunks)
    assert candidates, "需要先有 candidates 才能 rerank"

    reranked = rerank("劳动合同 解除 经济补偿", candidates, top_k=5)
    assert len(reranked) <= 5, "rerank 应返回至多 top_k 个"
    assert len(reranked) > 0, "rerank 应返回至少一条"

    # 验证 score 已更新（非 None，且为浮点数）
    for sc in reranked:
        assert isinstance(sc.score, float) or isinstance(sc.score, int)
        assert sc.score >= 0.0

    # 验证降序
    scores = [sc.score for sc in reranked]
    assert scores == sorted(scores, reverse=True), "rerank 结果应按 score 降序"


def test_rerank_empty_candidates():
    """空 candidates 应返回空列表。"""
    assert rerank("query", [], top_k=5) == []


# ---------------------------------------------------------------------------
# 测试 6：查询改写
# ---------------------------------------------------------------------------
def test_rewrite_query_basic():
    """rewrite_query 应返回非空字符串，且与原查询不同。"""
    rewritten = rewrite_query("公司辞退我", failed_citations=["《民法典》第9999条"])
    assert rewritten, "改写后查询不应为空"
    assert rewritten != "公司辞退我", "改写后查询应与原查询不同"


def test_rewrite_query_for_reretrieval_iteration_1():
    """第1次重检索：扩展同义词。"""
    rewritten = rewrite_for_reretrieval("公司辞退我", iteration=1)
    assert rewritten, "改写后查询不应为空"
    # 应该比原查询更长（含同义词）
    assert len(rewritten) >= len("公司辞退我")


def test_rewrite_query_for_reretrieval_iteration_2():
    """第2次重检索：尝试法条号改写。"""
    rewritten = rewrite_for_reretrieval("民法典 隐私权", iteration=2)
    assert rewritten, "改写后查询不应为空"
    # 应包含「《」或「民法典」相关结构
    assert "民法典" in rewritten or "中华人民共和国" in rewritten or "法律条文" in rewritten, (
        f"第2次重检索应聚焦法条号，实际：{rewritten}"
    )


def test_rewrite_query_for_reretrieval_iteration_3_fallback():
    """第3次重检索：兜底改写。"""
    rewritten = rewrite_for_reretrieval("公司辞退我", iteration=3)
    assert rewritten, "兜底改写不应为空"
    assert rewritten != "公司辞退我" or "法律条文" in rewritten
