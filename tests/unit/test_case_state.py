"""CaseState 与核心数据模型单元测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lvyan.schemas import (
    Authority,
    CaseAuthority,
    CaseState,
    CitationAudit,
    CitationDetail,
    EvidenceRequirement,
    Fact,
    ReasoningResult,
)
from lvyan.schemas.authority import Authority as AuthorityDirect
from lvyan.schemas.case import CaseState as CaseStateDirect
from lvyan.schemas.evidence import AuthorityConflict
from lvyan.schemas.output import CitationAudit as CitationAuditDirect


# ---------------------------------------------------------------------------
# 1. CaseState 可用最小必填字段构造
# ---------------------------------------------------------------------------
def test_case_state_minimal_construction():
    """仅传 run_id / thread_id / current_date / user_goal 即可构造，其余走默认值。"""
    state = CaseState(
        run_id="run-001",
        thread_id="thread-001",
        current_date=date(2026, 7, 23),
        user_goal="判断合同违约赔偿数额",
    )

    assert state.run_id == "run-001"
    assert state.thread_id == "thread-001"
    assert state.current_date == date(2026, 7, 23)
    assert state.user_goal == "判断合同违约赔偿数额"

    # 默认值校验
    assert state.jurisdiction is None
    assert state.case_type is None
    assert state.complexity == "light"
    assert state.facts == []
    assert state.disputed_facts == []
    assert state.timeline == []
    assert state.missing_facts == []
    assert state.uploaded_documents == []
    assert state.plan == []
    assert state.retrieval_queries == []
    assert state.statutes == []
    assert state.cases == []
    assert state.evidence_requirements == []
    assert state.conflicts == []
    assert state.reasoning_result is None
    assert state.citation_audit is None
    assert state.risk_level == "low"
    assert state.confidence == "insufficient"
    assert state.iteration == 0
    assert state.final_output is None


# ---------------------------------------------------------------------------
# 2. CaseState JSON 序列化/反序列化字段完整
# ---------------------------------------------------------------------------
def _build_full_state() -> CaseState:
    """构造一个填满各子模型的状态，用于序列化往返测试。"""
    return CaseState(
        run_id="run-002",
        thread_id="thread-002",
        current_date=date(2026, 7, 23),
        user_goal="民间借贷纠纷，请求本金与利息",
        jurisdiction="中国大陆",
        case_type="民间借贷",
        complexity="deep",
        facts=[
            Fact(
                fact_id="f1",
                category="当事人",
                content="原告张三，被告李四",
                source="user",
                confidence=0.9,
            ),
            Fact(
                fact_id="f2",
                category="金额",
                content="借款本金 10 万元",
                source="extracted",
                confidence=0.8,
            ),
        ],
        statutes=[
            Authority(
                source_id="law-civil-680",
                title="中华人民共和国民法典",
                article_number="第六百八十条",
                article_text="禁止高利放贷，借款的利率不得违反国家有关规定。",
                authority_level="法律",
                publication_date=date(2020, 5, 28),
                effective_date=date(2021, 1, 1),
                status="effective",
                jurisdiction="中国大陆",
                official_source="https://flk.npc.gov.cn/",
                content_hash="abc123",
                retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
                lexical_score=0.7,
                dense_score=0.8,
                rerank_score=0.85,
            ),
        ],
        cases=[
            CaseAuthority(
                case_id="case-001",
                case_number="(2023)京01民终123号",
                court="北京市第一中级人民法院",
                case_type="民间借贷",
                brief_facts="借款 10 万元未还",
                ruling_summary="支持本金及合法利息",
                ruling_date=date(2023, 3, 15),
                similarity_score=0.82,
                source_url="https://example.com/case/001",
            ),
        ],
        evidence_requirements=[
            EvidenceRequirement(
                requirement_id="er1",
                fact_to_prove="借贷关系成立",
                evidence_types=["借条", "转账记录"],
                current_status="partial",
                gap_description="缺少转账记录",
            ),
        ],
        conflicts=[
            AuthorityConflict(
                conflict_id="c1",
                authority_ids=["law-civil-680", "reg-old-001"],
                conflict_type="version",
                description="新旧法对利率上限规定不同",
                resolution=None,
            ),
        ],
        reasoning_result=ReasoningResult(
            legal_relationship="民间借贷",
            elements=["借贷合意", "款项交付"],
            disputed_focus=["利率是否超过法定上限"],
            plaintiff_arguments=["已交付本金"],
            defendant_arguments=["利率过高"],
            evidence_mapping=["借条证明借贷合意"],
            judicial_tendency="somewhat_favorable",
            evidence_confidence="medium",
            key_factors=["本金数额", "利率合规性"],
        ),
        citation_audit=CitationAudit(
            passed=True,
            total_citations=2,
            verified=2,
            fabricated=0,
            repealed_cited=0,
            unsupported=0,
            details=[
                CitationDetail(
                    citation_text="《民法典》第六百八十条",
                    status="verified",
                    matched_source_id="law-civil-680",
                    note=None,
                ),
            ],
            reretrieval_count=1,
        ),
        risk_level="medium",
        confidence="medium",
        iteration=2,
        final_output="（最终意见正文略）",
    )


def test_case_state_json_roundtrip():
    state = _build_full_state()
    payload = state.model_dump_json()
    restored = CaseState.model_validate_json(payload)

    # 顶层标量字段
    assert restored.run_id == state.run_id
    assert restored.thread_id == state.thread_id
    assert restored.current_date == state.current_date
    assert restored.user_goal == state.user_goal
    assert restored.jurisdiction == state.jurisdiction
    assert restored.case_type == state.case_type
    assert restored.complexity == state.complexity
    assert restored.risk_level == state.risk_level
    assert restored.confidence == state.confidence
    assert restored.iteration == state.iteration
    assert restored.final_output == state.final_output

    # 列表长度完整
    assert len(restored.facts) == 2
    assert len(restored.statutes) == 1
    assert len(restored.cases) == 1
    assert len(restored.evidence_requirements) == 1
    assert len(restored.conflicts) == 1

    # 嵌套模型字段完整
    assert restored.facts[0].fact_id == "f1"
    assert restored.facts[0].confidence == 0.9
    assert restored.statutes[0].article_number == "第六百八十条"
    assert restored.statutes[0].retrieved_at == state.statutes[0].retrieved_at
    assert restored.cases[0].case_number == "(2023)京01民终123号"
    assert restored.cases[0].ruling_date == date(2023, 3, 15)
    assert restored.evidence_requirements[0].current_status == "partial"
    assert restored.conflicts[0].conflict_type == "version"

    # 嵌套可选模型
    assert restored.reasoning_result is not None
    assert restored.reasoning_result.judicial_tendency == "somewhat_favorable"
    assert restored.reasoning_result.evidence_confidence == "medium"
    assert restored.citation_audit is not None
    assert restored.citation_audit.passed is True
    assert restored.citation_audit.reretrieval_count == 1
    assert len(restored.citation_audit.details) == 1


# ---------------------------------------------------------------------------
# 3. Authority.status 默认值
# ---------------------------------------------------------------------------
def test_authority_default_status_unknown():
    a = Authority(
        source_id="s1",
        title="示例法规",
        article_text="示例条文正文",
        authority_level="法律",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert a.status == "unknown"
    # 其它默认值
    assert a.jurisdiction == "中国大陆"
    assert a.article_number is None
    assert a.lexical_score == 0.0
    assert a.dense_score == 0.0
    assert a.rerank_score == 0.0


# ---------------------------------------------------------------------------
# 4. ReasoningResult 不含任何数字概率字段
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden_key",
    ["probability", "win_rate", "winrate", "percentage", "percent", "odds", "rate"],
)
def test_reasoning_result_has_no_numeric_probability_fields(forbidden_key: str):
    """model_fields 中不得出现任何暗示数字概率的键名。"""
    fields = ReasoningResult.model_fields.keys()
    assert forbidden_key not in fields, f"ReasoningResult 不应包含字段: {forbidden_key}"


def test_reasoning_result_fields_are_non_numeric_probability():
    """完整列出字段并断言无概率类键（子串检查）。"""
    fields = set(ReasoningResult.model_fields.keys())
    expected = {
        "legal_relationship",
        "elements",
        "disputed_focus",
        "plaintiff_arguments",
        "defendant_arguments",
        "evidence_mapping",
        "judicial_tendency",
        "evidence_confidence",
        "key_factors",
    }
    assert fields == expected

    forbidden_substrings = ("prob", "rate", "percent", "odds", "win", "chance")
    for name in fields:
        low = name.lower()
        for sub in forbidden_substrings:
            assert sub not in low, f"字段 {name} 含敏感子串 {sub}"


# ---------------------------------------------------------------------------
# 5. CitationAudit 默认 reretrieval_count 为 0
# ---------------------------------------------------------------------------
def test_citation_audit_default_reretrieval_count_zero():
    audit = CitationAudit(
        passed=False,
        total_citations=3,
        verified=1,
        fabricated=1,
        repealed_cited=0,
        unsupported=1,
        details=[
            CitationDetail(
                citation_text="《虚构法》第一条",
                status="fabricated",
                matched_source_id=None,
                note="未检索到该条文",
            ),
        ],
    )
    assert audit.reretrieval_count == 0


# ---------------------------------------------------------------------------
# 额外：导出路径一致性（包级 vs 模块直接导入）
# ---------------------------------------------------------------------------
def test_import_paths_consistent():
    assert Authority is AuthorityDirect
    assert CaseState is CaseStateDirect
    assert CitationAudit is CitationAuditDirect
