"""Task 9: 法规版本感知检索接口测试。

覆盖：
  1. as_of 历史时间点过滤——不返回 2021 年后生效的版本（如《民法典》）
  2. only_effective 过滤——不返回 status="repealed" 的版本
  3. verify_statute_status——effective / repealed 两类法规的核验
  4. 召回漏洞回归——「公司未经同意公开我的健康信息怎么办」应召回含
     「个人信息」或「隐私」的条文
  5. 转换为 Authority 模型——类型正确、字段齐全、可序列化
"""

from __future__ import annotations

from datetime import date

import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.version_aware import (
    StatuteVerification,
    search_statutes,
    verify_statute_status,
)
from lvyan.retrieval.version_resolver import build_version_groups, scan_all_laws
from lvyan.schemas.authority import Authority


# ---------------------------------------------------------------------------
# 跳过守卫：官方法律库不存在时整体跳过
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not LAWTEXT_DIR.is_dir(),
    reason=f"官方法律库目录不存在：{LAWTEXT_DIR}",
)


# ---------------------------------------------------------------------------
# Session 级预热：首次 search_statutes 会触发 dense 向量预计算（~30s），
# 在所有测试开始前统一预热一次，避免单个测试被首次耗时拖累。
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _warm_up_cache():
    """预热 hybrid_search 全局缓存（chunks / BM25 / dense 向量）。"""
    try:
        search_statutes(query="劳动", top_k=1)
    except Exception:
        # 预热失败不阻塞测试，后续测试会再次尝试并暴露真实错误
        pass


# ---------------------------------------------------------------------------
# 测试 1：as_of 历史时间点过滤
# ---------------------------------------------------------------------------
def test_search_statutes_as_of_filter():
    """as_of="2018-06-30" 时不应返回 2021 年后生效的版本（如《民法典》）。"""
    results = search_statutes(
        query="解除劳动合同经济补偿",
        as_of="2018-06-30",
        only_effective=True,
        top_k=5,
    )

    cutoff = date(2018, 6, 30)

    # 验证：所有结果的 effective_date <= cutoff 或 effective_date 为 None
    for r in results:
        if r.effective_date is not None:
            assert r.effective_date <= cutoff, (
                f"as_of 过滤失效：返回了 effective_date={r.effective_date} "
                f"(> {cutoff}) 的版本：{r.title}"
            )

    # 验证：不返回 2021 年后才生效的版本（如《民法典》2021-01-01 生效）
    for r in results:
        assert not (
            "民法典" in r.title and r.effective_date is not None and r.effective_date > cutoff
        ), f"《民法典》2021 版本不应出现在 as_of={cutoff} 的查询结果中"

    # only_effective=True 同时要求 status="effective"
    for r in results:
        assert r.status == "effective", (
            f"only_effective=True 不应返回 status={r.status} 的结果：{r.title}"
        )


def test_search_statutes_as_of_with_date_object():
    """as_of 也接受 date 对象（不仅限于字符串）。"""
    results = search_statutes(
        query="劳动",
        as_of=date(2018, 6, 30),
        only_effective=True,
        top_k=3,
    )
    cutoff = date(2018, 6, 30)
    for r in results:
        if r.effective_date is not None:
            assert r.effective_date <= cutoff


# ---------------------------------------------------------------------------
# 测试 2：only_effective 过滤
# ---------------------------------------------------------------------------
def test_search_statutes_only_effective():
    """only_effective=True 时返回结果中无 status="repealed" 的。"""
    results = search_statutes(
        query="劳动",
        only_effective=True,
        top_k=5,
    )

    for r in results:
        assert r.status != "repealed", f"only_effective=True 不应返回 repealed 结果：{r.title}"


def test_search_statutes_only_effective_includes_repealed_when_disabled():
    """only_effective=False 时不强制过滤 repealed（仅作宽松验证，不要求一定有 repealed）。"""
    results = search_statutes(
        query="劳动",
        only_effective=False,
        top_k=10,
    )
    # 不强制断言一定含 repealed（取决于召回），但所有结果应是合法 status
    valid_statuses = {"effective", "repealed", "not_yet_effective", "unknown"}
    for r in results:
        assert r.status in valid_statuses


# ---------------------------------------------------------------------------
# 测试 3：verify_statute_status
# ---------------------------------------------------------------------------
def _find_current_effective_meta():
    """从全库找一个 status=effective 且未被 superseded 的法规元数据。"""
    metas = scan_all_laws(LAWTEXT_DIR)
    groups = build_version_groups(metas)
    for group in groups:
        if group.current_effective is not None:
            return group.current_effective
    return None


def _find_repealed_meta():
    """从全库找一个 status=repealed 的法规元数据。"""
    metas = scan_all_laws(LAWTEXT_DIR)
    for m in metas:
        if m.status == "repealed":
            return m
    return None


def test_verify_statute_status_effective():
    """对已知有效的法规（current_effective），核验 is_effective_as_of=True。"""
    meta = _find_current_effective_meta()
    if meta is None:
        pytest.skip("全库未找到 current_effective 法规")

    result = verify_statute_status(meta.source_id)

    assert isinstance(result, StatuteVerification)
    assert result.source_id == meta.source_id
    assert result.title == meta.title
    assert result.current_status == "effective"
    assert result.is_effective_as_of is True, (
        f"current_effective 法规应 is_effective_as_of=True：{meta.title}"
    )
    assert result.checked_at is not None


def test_verify_statute_status_repealed():
    """对 status=repealed 的法规，核验 is_effective_as_of=False。"""
    meta = _find_repealed_meta()
    if meta is None:
        pytest.skip("全库未找到 repealed 法规")

    result = verify_statute_status(meta.source_id)

    assert isinstance(result, StatuteVerification)
    assert result.source_id == meta.source_id
    assert result.current_status == "repealed"
    assert result.is_effective_as_of is False, (
        f"repealed 法规应 is_effective_as_of=False：{meta.title}"
    )


def test_verify_statute_status_unknown_source_id():
    """不存在的 source_id 应返回 current_status=unknown / is_effective_as_of=False。"""
    result = verify_statute_status("nonexistent_source_id_xyz_12345")
    assert result.current_status == "unknown"
    assert result.is_effective_as_of is False


def test_verify_statute_status_as_of_before_effective():
    """as_of 早于 effective_date 时应判定为无效。

    取一个 current_effective 且 effective_date 已知的法规，
    用早于其 effective_date 的日期做 as_of 查询。
    """
    meta = _find_current_effective_meta()
    if meta is None or meta.effective_date is None:
        pytest.skip("未找到带 effective_date 的 current_effective 法规")

    # as_of 设为 effective_date 前一天
    as_of = meta.effective_date.replace(day=1)  # 至少回到月初
    if as_of >= meta.effective_date:
        as_of = date(meta.effective_date.year - 1, 1, 1)

    result = verify_statute_status(meta.source_id, as_of=as_of)
    assert result.is_effective_as_of is False, (
        f"as_of={as_of} 早于 effective_date={meta.effective_date}，应判定为无效：{meta.title}"
    )


def test_verify_statute_status_as_of_after_effective():
    """as_of 晚于 effective_date 且 status=effective 时应判定为有效。"""
    meta = _find_current_effective_meta()
    if meta is None or meta.effective_date is None:
        pytest.skip("未找到带 effective_date 的 current_effective 法规")

    # as_of 设为 effective_date 之后
    as_of = date(meta.effective_date.year + 1, 12, 31)
    # 不能超过当前日期太远（避免语义混乱），但 date 对象本身无上限
    result = verify_statute_status(meta.source_id, as_of=as_of)
    assert result.is_effective_as_of is True


# ---------------------------------------------------------------------------
# 测试 4：召回漏洞回归测试
# ---------------------------------------------------------------------------
def test_recall_gap_regression_personal_info():
    """spec 明确召回漏洞回归场景：查询「公司未经同意公开我的健康信息怎么办」
    应能召回含「个人信息」或「隐私」的条文。
    """
    results = search_statutes(
        query="公司未经同意公开我的健康信息怎么办",
        top_k=5,
    )

    assert results, "召回漏洞回归：应返回至少一条结果"

    has_personal_info = any(
        "个人信息" in r.article_text or "隐私" in r.article_text for r in results
    )
    assert has_personal_info, (
        "召回漏洞回归：返回结果中应至少有一条 article_text 含「个人信息」或「隐私」，"
        f"实际 titles={[r.title[:30] for r in results]}"
    )


# ---------------------------------------------------------------------------
# 测试 5：转换为 Authority 模型
# ---------------------------------------------------------------------------
def test_search_statutes_returns_authority_models():
    """验证 search_statutes 返回的是 Authority 模型列表，字段齐全且可序列化。"""
    results = search_statutes(query="劳动", top_k=3)

    assert results, "应返回非空结果以验证模型转换"
    for r in results:
        assert isinstance(r, Authority), f"返回类型应为 Authority，实际 {type(r)}"

        # 必填字段非空
        assert r.source_id, "source_id 不应为空"
        assert r.title, "title 不应为空"
        assert r.article_text, "article_text 不应为空"
        assert r.authority_level, "authority_level 不应为空"
        assert r.retrieved_at is not None, "retrieved_at 不应为 None"

        # status 在合法枚举内
        assert r.status in ("effective", "repealed", "not_yet_effective", "unknown")

        # 分数字段为 float
        assert isinstance(r.lexical_score, float)
        assert isinstance(r.dense_score, float)
        assert isinstance(r.rerank_score, float)
        assert r.lexical_score >= 0.0

        # jurisdiction 默认中国大陆
        assert r.jurisdiction == "中国大陆"


def test_authority_model_serialization():
    """验证 Authority 可 model_dump_json() 序列化为非空 JSON 字符串。"""
    results = search_statutes(query="劳动", top_k=1)
    assert results, "需要至少一条结果验证序列化"

    authority = results[0]
    json_str = authority.model_dump_json()
    assert isinstance(json_str, str)
    assert json_str, "model_dump_json() 不应返回空字符串"

    # JSON 中应包含关键字段
    assert "source_id" in json_str
    assert "article_text" in json_str
    assert "status" in json_str

    # 反序列化验证 round-trip
    restored = Authority.model_validate_json(json_str)
    assert restored.source_id == authority.source_id
    assert restored.title == authority.title
    assert restored.article_text == authority.article_text


def test_search_statutes_empty_query_returns_empty():
    """空查询应返回空列表，不抛异常。"""
    assert search_statutes(query="", top_k=5) == []
    assert search_statutes(query="   ", top_k=5) == []


def test_search_statutes_respects_top_k():
    """top_k 应限制返回结果数。"""
    results = search_statutes(query="劳动", top_k=2)
    assert len(results) <= 2, f"top_k=2 应返回至多 2 条，实际 {len(results)}"
