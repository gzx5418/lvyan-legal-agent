"""Citation Verifier 节点单元测试（SubTask 13.5）。

覆盖场景：
1. 正常通过：所有验证器通过 → citation_audit.passed=True，无重检索
2. 虚构引用触发重检索：「《民法典》第9999条」→ passed=False, iteration+1, 追加 retrieval_query
3. 达到迭代上限：iteration >= max → risk_level="high", confidence="insufficient"
4. 已废止法规：statute status=repealed → CitationDetail status="repealed"
5. route_after_citation 路由逻辑
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from lvyan.config import settings
from lvyan.graph.routing import route_after_citation
from lvyan.nodes.citation_verifier import citation_verifier
from lvyan.retrieval.version_aware import StatuteVerification
from lvyan.schemas import Authority, ReasoningResult, RetrievalQuery


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
    article_text: str = (
        "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
        "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
    ),
    effective_date: date | None = date(2021, 1, 1),
    status: str = "effective",
    source_id: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level="法律",
        effective_date=effective_date,
        status=status,  # type: ignore[arg-type]
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_reasoning_result(
    key_factors: list[str] | None = None,
    judicial_tendency: str = "somewhat_favorable",
) -> ReasoningResult:
    if key_factors is None:
        key_factors = [
            "依据《中华人民共和国民法典》第五百七十七条，违约方应承担违约责任"
        ]
    return ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）", "违约行为（已满足）"],
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["原告主张对方违约应赔偿"],
        defendant_arguments=["被告主张不可抗力免责"],
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency=judicial_tendency,  # type: ignore[arg-type]
        evidence_confidence="medium",
        key_factors=key_factors,
    )


def _mock_verify(status: str = "effective", superseded_by: str | None = None):
    """构造 mock verify_statute_status 函数。"""

    def _mock(source_id: str, as_of: Any = None) -> StatuteVerification:
        return StatuteVerification(
            source_id=source_id,
            title="mock-title",
            current_status=status,  # type: ignore[arg-type]
            superseded_by=superseded_by,
            checked_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
        )

    return _mock


def _make_state(
    reasoning_result: ReasoningResult | None = None,
    statutes: list[Authority] | None = None,
    iteration: int = 0,
    retrieval_queries: list[RetrievalQuery] | None = None,
    user_goal: str = "公司辞退我要求经济补偿",
) -> dict:
    """构造测试用 state dict。"""
    if statutes is None:
        statutes = [_make_authority()]
    if reasoning_result is None:
        reasoning_result = _make_reasoning_result()
    if retrieval_queries is None:
        retrieval_queries = [
            RetrievalQuery(
                query_id="rq-1",
                query_text="劳动合同 经济补偿",
                route="hybrid",
            )
        ]
    return {
        "run_id": "run-citation-test",
        "thread_id": "thread-citation-test",
        "current_date": date(2026, 7, 23),
        "user_goal": user_goal,
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "deep",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": retrieval_queries,
        "statutes": statutes,
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": reasoning_result,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "medium",
        "iteration": iteration,
        "final_output": None,
    }


# ---------------------------------------------------------------------------
# 1. 正常通过
# ---------------------------------------------------------------------------
def test_citation_verifier_passes_good_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """所有验证器通过 → citation_audit.passed=True，无重检索。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    state = _make_state()
    result = citation_verifier(state)

    assert "citation_audit" in result
    audit = result["citation_audit"]
    assert audit["passed"] is True
    # 不应触发重检索
    assert "iteration" not in result
    assert "risk_level" not in result


# ---------------------------------------------------------------------------
# 2. 虚构引用触发重检索
# ---------------------------------------------------------------------------
def test_citation_verifier_fabricated_triggers_reretrieval(
    monkeypatch: pytest.MonkeyPatch,
):
    """「《民法典》第9999条」虚构引用 → passed=False, iteration+1, 追加 retrieval_query。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条承担违约责任",
            "依据《中华人民共和国民法典》第九千九百九十九条虚构法条",
        ]
    )
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(article_number="第五百七十七条")],
        iteration=0,
    )
    original_queries = list(state["retrieval_queries"])
    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is False
    assert audit["fabricated"] >= 1

    # 应触发重检索：iteration+1, 追加 retrieval_query
    assert result["iteration"] == 1
    assert len(result["retrieval_queries"]) == len(original_queries) + 1
    new_query = result["retrieval_queries"][-1]
    assert isinstance(new_query, RetrievalQuery)
    assert new_query.query_text  # 改写后的查询非空
    assert new_query.route == "hybrid"


# ---------------------------------------------------------------------------
# 3. 达到迭代上限：强制通过，标记高风险
# ---------------------------------------------------------------------------
def test_citation_verifier_max_iterations_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """iteration >= max → risk_level="high", confidence="insufficient"。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条虚构法条"]
    )
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(article_number="第五百七十七条")],
        iteration=settings.max_retrieval_iterations,  # 已达上限
    )
    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is False
    # 应标记高风险
    assert result["risk_level"] == "high"
    assert result["confidence"] == "insufficient"
    # 不应再追加 retrieval_query
    assert "retrieval_queries" not in result


# ---------------------------------------------------------------------------
# 4. 已废止法规 → CitationDetail status="repealed"
# ---------------------------------------------------------------------------
def test_citation_verifier_repealed_statute(
    monkeypatch: pytest.MonkeyPatch,
):
    """statute status=repealed → CitationDetail status="repealed"。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("repealed"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("repealed"),
    )
    rr = _make_reasoning_result()
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(status="repealed")],
    )
    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is False
    assert audit["repealed_cited"] >= 1
    # 检查 CitationDetail
    repealed_details = [d for d in audit["details"] if d["status"] == "repealed"]
    assert len(repealed_details) >= 1


# ---------------------------------------------------------------------------
# 5. route_after_citation 路由逻辑
# ---------------------------------------------------------------------------
def test_route_after_citation_pass():
    """citation_audit.passed=True → output_guardrail（P1-9b 修复后路由目标）。"""
    state = {
        "citation_audit": {"passed": True},
        "iteration": 0,
    }
    assert route_after_citation(state) == "output_guardrail"


def test_route_after_citation_reretrieve():
    """citation_audit.passed=False 且 iteration < max → reretrieve。"""
    state = {
        "citation_audit": {"passed": False},
        "iteration": 0,
    }
    assert route_after_citation(state) == "reretrieve"


def test_route_after_citation_max_reached():
    """citation_audit.passed=False 且 iteration >= max → output_guardrail（P1-9b）。"""
    state = {
        "citation_audit": {"passed": False},
        "iteration": settings.max_retrieval_iterations,
    }
    assert route_after_citation(state) == "output_guardrail"


def test_route_after_citation_no_audit():
    """citation_audit=None → output_guardrail（P1-9b）。"""
    state = {"citation_audit": None, "iteration": 0}
    assert route_after_citation(state) == "output_guardrail"


# ---------------------------------------------------------------------------
# 6. 重检索查询改写验证
# ---------------------------------------------------------------------------
def test_citation_verifier_rewrite_query(
    monkeypatch: pytest.MonkeyPatch,
):
    """重检索时调用 rewrite_for_reretrieval 改写查询，新查询非空。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条虚构法条"]
    )
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(article_number="第五百七十七条")],
        iteration=0,
    )
    result = citation_verifier(state)

    # 验证新增的 retrieval_query 文本非空且与原查询不同（或至少有内容）
    new_queries = result["retrieval_queries"]
    assert len(new_queries) >= 2  # 原有 + 新增
    new_query = new_queries[-1]
    assert new_query.query_text
    assert len(new_query.query_text) > 0


# ---------------------------------------------------------------------------
# 7. 多次迭代递增
# ---------------------------------------------------------------------------
def test_citation_verifier_iteration_increments(
    monkeypatch: pytest.MonkeyPatch,
):
    """多次调用 citation_verifier，iteration 应递增。"""
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条虚构法条"]
    )

    # 第一次：iteration 0 → 1
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(article_number="第五百七十七条")],
        iteration=0,
    )
    result1 = citation_verifier(state)
    assert result1["iteration"] == 1

    # 第二次：iteration 1 → 2
    state2 = dict(state)
    state2["iteration"] = result1["iteration"]
    state2["retrieval_queries"] = result1["retrieval_queries"]
    result2 = citation_verifier(state2)
    assert result2["iteration"] == 2


# ---------------------------------------------------------------------------
# 8. spec 约束：citation_verifier 内部限制为 2 次（min(settings.max_retrieval_iterations, 2)）
# ---------------------------------------------------------------------------
def test_citation_verifier_internal_cap_is_two(
    monkeypatch: pytest.MonkeyPatch,
):
    """iteration=2 且 settings.max_retrieval_iterations=3（默认）→ 强制通过。

    覆盖 spec 重要约束：「citation_verifier 内部限制为 2 次
    （取 min(settings.MAX_RETRIEVAL_ITERATIONS, 2)）」。
    即使全局配置允许 3 次，节点内部最多重检索 2 次，第 3 次（iteration=2）
    应强制通过并标记高风险。
    """
    monkeypatch.setattr(
        "lvyan.validators.citation.verify_statute_status",
        _mock_verify("effective"),
    )
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    # 确保全局配置 > 2，以验证内部 min(, 2) 生效
    assert settings.max_retrieval_iterations >= 2

    rr = _make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条虚构法条"]
    )
    state = _make_state(
        reasoning_result=rr,
        statutes=[_make_authority(article_number="第五百七十七条")],
        iteration=2,  # 等于内部上限 min(3, 2)=2
    )
    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is False
    # iteration=2 已达内部上限 → 强制通过，标记高风险
    assert result["risk_level"] == "high"
    assert result["confidence"] == "insufficient"
    # 不应再追加 retrieval_query 或递增 iteration
    assert "retrieval_queries" not in result
    assert "iteration" not in result


def test_route_after_citation_internal_cap_is_two():
    """route_after_citation 与 citation_verifier 内部上限一致：iteration=2 → output_guardrail。

    当 settings.max_retrieval_iterations=3（默认）时，iteration=2 已达内部上限
    min(3, 2)=2，路由应返回 output_guardrail 而非 reretrieve。
    """
    assert settings.max_retrieval_iterations >= 2
    state = {
        "citation_audit": {"passed": False},
        "iteration": 2,  # 等于内部上限
    }
    assert route_after_citation(state) == "output_guardrail"

    # iteration=1 仍可重检索
    state_below = {
        "citation_audit": {"passed": False},
        "iteration": 1,
    }
    assert route_after_citation(state_below) == "reretrieve"
