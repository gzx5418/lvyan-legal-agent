"""P0-4：GraphState reducer 行为单测。

验证修复后：
  1. ``merge_authorities`` 按 (source_id, article_number) 去重，同键保留分数更高者；
  2. ``merge_plan`` 按 step_id 去重，新值覆盖旧值的 status；
  3. ``merge_retrieval_queries`` 按 query_id 去重；
  4. ``merge_evidence_requirements`` / ``merge_conflicts`` / ``merge_missing_facts``
     按各自唯一键去重；
  5. ``merge_cases`` 按 case_id 去重并保留 similarity_score 更高者。

关键回归：节点返回「完整列表」时不会让状态膨胀（如 10+10=20）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lvyan.graph.state import (  # noqa: E402
    merge_authorities,
    merge_cases,
    merge_conflicts,
    merge_evidence_requirements,
    merge_missing_facts,
    merge_plan,
    merge_retrieval_queries,
)
from lvyan.schemas.authority import Authority  # noqa: E402
from lvyan.schemas.case import (  # noqa: E402
    MissingFact,
    PlanStep,
    RetrievalQuery,
)
from lvyan.schemas.evidence import (  # noqa: E402
    AuthorityConflict,
    CaseAuthority,
    EvidenceRequirement,
)


# ---------------------------------------------------------------------------
# 1. merge_authorities：完整列表返回时不会翻倍
# ---------------------------------------------------------------------------
def _make_auth(
    source_id: str = "src-1",
    title: str = "民法典",
    article_number: str | None = "第1条",
    rerank_score: float = 0.5,
) -> Authority:
    return Authority(
        source_id=source_id,
        title=title,
        article_number=article_number,
        article_text="...",
        authority_level="法律",
        status="effective",
        retrieved_at=datetime(2026, 7, 26),
        rerank_score=rerank_score,
    )


def test_merge_authorities_dedups_by_source_and_article():
    """同 (source_id, article_number) 应去重，结果只有 1 条。"""
    a1 = _make_auth(rerank_score=0.3)
    a2 = _make_auth(rerank_score=0.6)  # 同键，分数更高

    merged = merge_authorities([a1], [a2])

    assert len(merged) == 1
    # 保留分数更高者
    assert merged[0].rerank_score == 0.6


def test_merge_authorities_no_duplication_when_returning_full_list():
    """P0-4 核心回归：节点返回完整列表不应导致状态翻倍。

    模拟 authority_resolver 行为：旧 10 条 → 节点返回同样 10 条完整列表
    → 合并后仍应只有 10 条，而非 20 条。
    """
    old = [_make_auth(source_id=f"src-{i}", article_number=f"第{i}条") for i in range(10)]
    # 节点返回完整列表（同 10 条，分数略高模拟 rerank）
    new = [_make_auth(source_id=f"src-{i}", article_number=f"第{i}条", rerank_score=0.9) for i in range(10)]

    merged = merge_authorities(old, new)

    assert len(merged) == 10, f"应去重为 10 条，实际 {len(merged)}"


def test_merge_authorities_different_keys_kept():
    """不同键应保留全部。"""
    a1 = _make_auth(source_id="src-1", article_number="第1条")
    a2 = _make_auth(source_id="src-1", article_number="第2条")
    a3 = _make_auth(source_id="src-2", article_number="第1条")

    merged = merge_authorities([a1], [a2, a3])

    assert len(merged) == 3


def test_merge_authorities_empty_inputs():
    assert merge_authorities([], []) == []
    assert len(merge_authorities([_make_auth()], [])) == 1


# ---------------------------------------------------------------------------
# 2. merge_plan：step_id 去重，新值覆盖旧值
# ---------------------------------------------------------------------------
def test_merge_plan_dedups_by_step_id():
    """同 step_id 的 step 应去重，新值覆盖旧值 status。"""
    s1_old = PlanStep(step_id="s1", action="检索", tool="bm25", status="pending")
    s1_new = PlanStep(step_id="s1", action="检索", tool="bm25", status="done")
    s2 = PlanStep(step_id="s2", action="推理", tool="reason", status="pending")

    merged = merge_plan([s1_old], [s1_new, s2])

    assert len(merged) == 2
    by_id = {s.step_id: s for s in merged}
    assert by_id["s1"].status == "done"  # 新值覆盖
    assert by_id["s2"].status == "pending"


def test_merge_plan_no_duplication_when_returning_full_list():
    """plan 全量返回时不会翻倍。"""
    old = [PlanStep(step_id=f"s{i}", action="a", tool="t", status="done") for i in range(5)]
    new = [PlanStep(step_id=f"s{i}", action="a", tool="t", status="done") for i in range(5)]

    merged = merge_plan(old, new)
    assert len(merged) == 5


# ---------------------------------------------------------------------------
# 3. merge_retrieval_queries：query_id 去重
# ---------------------------------------------------------------------------
def test_merge_retrieval_queries_dedups_by_query_id():
    q1_old = RetrievalQuery(query_id="q1", query_text="a", route="bm25", result_count=0)
    q1_new = RetrievalQuery(query_id="q1", query_text="a", route="bm25", result_count=5)
    q2 = RetrievalQuery(query_id="q2", query_text="b", route="dense", result_count=0)

    merged = merge_retrieval_queries([q1_old], [q1_new, q2])

    assert len(merged) == 2
    by_id = {q.query_id: q for q in merged}
    assert by_id["q1"].result_count == 5


# ---------------------------------------------------------------------------
# 4. merge_cases：case_id 去重，保留 similarity_score 更高者
# ---------------------------------------------------------------------------
def _make_case(case_id: str = "c1", similarity: float = 0.5) -> CaseAuthority:
    return CaseAuthority(
        case_id=case_id,
        court="法院",
        case_type="民事",
        brief_facts="...",
        ruling_summary="...",
        similarity_score=similarity,
    )


def test_merge_cases_dedups_by_case_id():
    c1_old = _make_case("c1", similarity=0.3)
    c1_new = _make_case("c1", similarity=0.7)
    c2 = _make_case("c2", similarity=0.6)

    merged = merge_cases([c1_old], [c1_new, c2])

    assert len(merged) == 2
    by_id = {c.case_id: c for c in merged}
    assert by_id["c1"].similarity_score == 0.7


# ---------------------------------------------------------------------------
# 5. merge_evidence_requirements / merge_conflicts / merge_missing_facts
# ---------------------------------------------------------------------------
def test_merge_evidence_requirements_dedups():
    er1_old = EvidenceRequirement(
        requirement_id="r1", fact_to_prove="A", evidence_types=[], current_status="missing"
    )
    er1_new = EvidenceRequirement(
        requirement_id="r1", fact_to_prove="A", evidence_types=[], current_status="met"
    )
    er2 = EvidenceRequirement(
        requirement_id="r2", fact_to_prove="B", evidence_types=[], current_status="missing"
    )

    merged = merge_evidence_requirements([er1_old], [er1_new, er2])

    assert len(merged) == 2
    by_id = {e.requirement_id: e for e in merged}
    assert by_id["r1"].current_status == "met"


def test_merge_conflicts_dedups():
    cf1_old = AuthorityConflict(
        conflict_id="cf1", authority_ids=["a"], conflict_type="version", description="..."
    )
    cf1_new = AuthorityConflict(
        conflict_id="cf1",
        authority_ids=["a"],
        conflict_type="version",
        description="...",
        resolution="已解决",
    )

    merged = merge_conflicts([cf1_old], [cf1_new])

    assert len(merged) == 1
    assert merged[0].resolution == "已解决"


def test_merge_missing_facts_dedups():
    mf1_old = MissingFact(fact_key="k1", question="Q1", reason="R1")
    mf1_new = MissingFact(fact_key="k1", question="Q1-updated", reason="R1-new")

    merged = merge_missing_facts([mf1_old], [mf1_new])

    assert len(merged) == 1
    assert merged[0].question == "Q1-updated"
