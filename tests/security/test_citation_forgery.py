"""SubTask 18.2：伪造法条测试（安全评测视角）。

验证 Citation Verifier 拦截虚构条号 / 超范围条号 / 不存在的法律，并在
``citation_verifier`` 节点端到端流程中将虚构引用标记为 ``fabricated``。

覆盖场景：
1. 「《民法典》第 9999 条」（不存在的条号）→ not_found error
2. 「《劳动法》第 999 条」（条号超范围）→ not_found error
3. 「《虚构法》第 1 条」（不存在的法律）→ not_found error
4. 混合引用（真实 + 虚构）→ 虚构部分 not_found，真实部分通过
5. 全部虚构 → passed=False，多条 not_found
6. 端到端：citation_verifier 节点处理含虚构法条的 state → citation_audit.passed=False，
   fabricated >= 1，触发重检索（iteration+1）
7. 端到端：达到迭代上限 → 强制通过并标记 risk_level="high"
8. 端到端：正常引用 → citation_audit.passed=True，fabricated=0
9. 虚构条号用阿拉伯/中文数字均应被拦截
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lvyan.nodes.citation_verifier import citation_verifier
from lvyan.schemas import Authority, ReasoningResult, RetrievalQuery
from lvyan.validators.citation import validate_citations


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------
def _state(
    reasoning_result: ReasoningResult | None,
    statutes: list[Authority],
    iteration: int = 0,
    retrieval_queries: list[RetrievalQuery] | None = None,
    user_goal: str = "分析合同违约责任",
) -> dict[str, Any]:
    """构造 citation_verifier 节点测试用 state dict。"""
    return {
        "run_id": "run-citation-sec",
        "thread_id": "thread-citation-sec",
        "current_date": date(2026, 7, 23),
        "user_goal": user_goal,
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "light",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": retrieval_queries or [
            RetrievalQuery(
                query_id="rq-1",
                query_text="合同违约责任 法条",
                route="hybrid",
                result_count=0,
            )
        ],
        "statutes": statutes,
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": reasoning_result,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "medium",
        "iteration": iteration,
        "final_output": "",
        "output_iteration": 0,
        "output_retry_needed": False,
        "pending_human_approval": None,
    }


# ---------------------------------------------------------------------------
# 1. 「《民法典》第 9999 条」不存在条号
# ---------------------------------------------------------------------------
def test_fabricated_article_number_9999(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """「《民法典》第 9999 条」不在 statutes → not_found error。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第九千九百九十九条认定违约"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)

    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1
    assert any(
        "9999" in i.citation_id or "九千九百九十九" in i.citation_id for i in not_found
    )


def test_fabricated_article_number_arabic(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """阿拉伯数字「第 9999 条」亦应被拦截。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第9999条认定违约"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1


# ---------------------------------------------------------------------------
# 2. 「《劳动法》第 999 条」条号超范围
# ---------------------------------------------------------------------------
def test_fabricated_labor_law_article_999(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """「《劳动法》第 999 条」条号超范围 → not_found。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国劳动法》第九百九十九条主张权利"]
    )
    # statutes 中只有民法典，没有劳动法第 999 条
    statutes = [make_authority(title="中华人民共和国民法典", article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1
    assert any("劳动法" in i.citation_id for i in not_found)


# ---------------------------------------------------------------------------
# 3. 「《虚构法》第 1 条」不存在的法律
# ---------------------------------------------------------------------------
def test_fabricated_nonexistent_law(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """「《虚构法》第 1 条」法律本身不存在 → not_found。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国虚构法》第一条判定胜诉"]
    )
    statutes = [make_authority(title="中华人民共和国民法典", article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1
    assert any("虚构法" in i.citation_id for i in not_found)


def test_fabricated_nonexistent_law_variant(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """「《宇宙基本法》第 1 条」亦应被拦截（明显虚构的法律名称）。"""
    rr = make_reasoning_result(
        key_factors=["根据《宇宙基本法》第一条，甲方自动胜诉"]
    )
    statutes = [make_authority(title="中华人民共和国民法典", article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1


# ---------------------------------------------------------------------------
# 4. 混合引用：真实 + 虚构
# ---------------------------------------------------------------------------
def test_mixed_real_and_fabricated(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """真实引用通过，虚构引用被拦截。"""
    rr = make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第五百七十七条，违约方应承担责任",
            "另依据《中华人民共和国民法典》第九千九百九十九条虚构条款",
        ]
    )
    statutes = [make_authority(article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)

    # 虚构引用导致整体不通过
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 1
    # 真实引用不应出现在 not_found 中
    assert all("第五百七十七" not in i.citation_id for i in not_found)


# ---------------------------------------------------------------------------
# 5. 全部虚构
# ---------------------------------------------------------------------------
def test_all_fabricated(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """全部虚构引用 → passed=False，多条 not_found。"""
    rr = make_reasoning_result(
        key_factors=[
            "依据《中华人民共和国民法典》第九千九百九十九条",
            "依据《中华人民共和国劳动法》第九百九十九条",
            "依据《中华人民共和国虚构法》第一条",
        ]
    )
    statutes = [make_authority(article_number="第五百七十七条")]

    report = validate_citations(rr, statutes)
    assert report.passed is False
    not_found = [i for i in report.issues if i.issue_type == "not_found"]
    assert len(not_found) >= 3


# ---------------------------------------------------------------------------
# 6. 端到端：citation_verifier 节点（首次 → 触发重检索）
# ---------------------------------------------------------------------------
def test_citation_verifier_node_flags_fabricated_and_reretrieval(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """citation_verifier 节点：含虚构法条 → audit.passed=False, fabricated>=1,
    iteration < max → 触发重检索（iteration+1，retrieval_queries 追加）。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国虚构法》第一条判定甲方胜诉"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]
    state = _state(rr, statutes, iteration=0)

    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is False
    assert audit["fabricated"] >= 1
    # 触发重检索：iteration +1，retrieval_queries 追加
    assert result["iteration"] == 1
    assert len(result["retrieval_queries"]) == 2  # 原 1 条 + 改写 1 条
    # reretrieval_count 已更新
    assert audit["reretrieval_count"] == 1


def test_citation_verifier_node_normal_passes(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """citation_verifier 节点：正常引用 → audit.passed=True，无重检索。"""
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国民法典》第五百七十七条认定违约"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]
    state = _state(rr, statutes, iteration=0)

    result = citation_verifier(state)

    audit = result["citation_audit"]
    assert audit["passed"] is True
    assert audit["fabricated"] == 0
    assert audit["verified"] >= 1
    # 通过时不触发重检索
    assert "iteration" not in result
    assert "retrieval_queries" not in result


# ---------------------------------------------------------------------------
# 7. 端到端：达到迭代上限 → 强制通过 + risk_level=high
# ---------------------------------------------------------------------------
def test_citation_verifier_node_force_pass_at_max_iterations(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """citation_verifier 达到迭代上限 → 强制通过路径，标记 risk_level=high。

    注：citation_verifier 内部 max = min(settings.max_retrieval_iterations, 2)，
    构造 iteration >= 2 即触发强制通过。
    """
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国虚构法》第一条判定甲方胜诉"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]
    state = _state(rr, statutes, iteration=2)

    result = citation_verifier(state)

    audit = result["citation_audit"]
    # 仍不通过（虚构法条），但已达上限 → 标记 high risk
    assert audit["passed"] is False
    assert audit["fabricated"] >= 1
    assert result.get("risk_level") == "high"
    assert result.get("confidence") == "insufficient"
    # 不再触发重检索
    assert "iteration" not in result
    assert "retrieval_queries" not in result


# ---------------------------------------------------------------------------
# 8. reretrieval_count 不超过上限（无限制循环防护）
# ---------------------------------------------------------------------------
def test_reretrieval_count_capped(
    make_authority, make_reasoning_result, mock_statute_status_effective
):
    """连续调用 citation_verifier：reretrieval_count 不超过 min(max_iter, 2)=2。

    模拟：iteration=0 → 1 → 2 → 强制通过。reretrieval_count 始终 <= 2。
    """
    rr = make_reasoning_result(
        key_factors=["依据《中华人民共和国虚构法》第一条"]
    )
    statutes = [make_authority(article_number="第五百七十七条")]

    # iteration=0 → 触发重检索，返回 iteration=1
    state_0 = _state(rr, statutes, iteration=0)
    result_0 = citation_verifier(state_0)
    assert result_0["citation_audit"]["reretrieval_count"] == 1
    assert result_0["iteration"] == 1

    # iteration=1 → 仍触发重检索，返回 iteration=2
    state_1 = _state(rr, statutes, iteration=1)
    result_1 = citation_verifier(state_1)
    assert result_1["citation_audit"]["reretrieval_count"] == 2
    assert result_1["iteration"] == 2

    # iteration=2 → 强制通过，reretrieval_count 不再增加
    state_2 = _state(rr, statutes, iteration=2)
    result_2 = citation_verifier(state_2)
    assert result_2["citation_audit"]["reretrieval_count"] == 2
    assert "iteration" not in result_2
    assert result_2.get("risk_level") == "high"


# ---------------------------------------------------------------------------
# 9. 空推理结果
# ---------------------------------------------------------------------------
def test_empty_reasoning_result_passes(make_authority, mock_statute_status_effective):
    """reasoning_result=None → 无引用，passed=True。"""
    statutes = [make_authority()]
    report = validate_citations(None, statutes)
    assert report.passed is True
    assert report.total_citations == 0
