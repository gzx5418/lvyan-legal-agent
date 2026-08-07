"""Task 11: 检索与权威解析节点单元测试。

覆盖 SubTask 11.1 ~ 11.4：
  1. ``parallel_retrieval`` 写入 statutes / cases（不报错且返回 list）。
  2. ``evidence_analyzer`` 证据缺口分析（至少一个 current_status="missing"）。
  3. ``authority_resolver`` 去重（同 source_id + article_number 仅保留 1 条）。
  4. ``authority_resolver`` 效力层级排序（宪法在法律之前）。
  5. ``authority_resolver`` 版本冲突检测（同 title 多 effective 版本触发
     version 类型冲突）。
"""

from __future__ import annotations

from datetime import date, datetime


from lvyan.config import LAWTEXT_DIR
from lvyan.nodes.evidence_analyzer import authority_resolver, evidence_analyzer
from lvyan.nodes.retrieve_cases import case_difference_compare
from lvyan.nodes.retrieve_statutes import parallel_retrieval
from lvyan.schemas import Authority, CaseAuthority, Fact, RetrievalQuery


# ---------------------------------------------------------------------------
# 测试 1：parallel_retrieval 写入 statutes / cases
# ---------------------------------------------------------------------------
def test_parallel_retrieval_writes_statutes_and_cases():
    """parallel_retrieval 应返回 statutes / cases 列表，且不报错。

    检索质量受官方法律库可用性影响，故此处仅断言「返回 list 且不抛异常」，
    不强求 statutes 非空。
    """
    state = {
        "user_goal": "公司辞退我",
        "retrieval_queries": [
            RetrievalQuery(
                query_id="q1",
                query_text="劳动争议 解除 经济补偿",
                route="hybrid",
            )
        ],
        "statutes": [],
        "cases": [],
        "plan": [],
    }

    result = parallel_retrieval(state)

    assert isinstance(result, dict)
    assert "statutes" in result
    assert "cases" in result
    assert isinstance(result["statutes"], list)
    assert isinstance(result["cases"], list)
    # plan 应被返回（即使原 plan 为空，也应回传空列表）
    assert "plan" in result
    assert isinstance(result["plan"], list)
    # 若官方法律库可用且检索成功，statutes 应非空；否则至少不应抛异常
    if LAWTEXT_DIR.is_dir() and result["statutes"]:
        # 验证返回的是 Authority 模型（或 dict 含 source_id）
        first = result["statutes"][0]
        sid = getattr(first, "source_id", None) or (
            first.get("source_id") if isinstance(first, dict) else None
        )
        assert sid, "Authority 应含 source_id"


def test_parallel_retrieval_empty_queries_returns_empty_lists():
    """无 retrieval_queries 且无 user_goal 时返回空列表，不抛异常。

    注：parallel_retrieval 在 retrieval_queries 为空时会回退到 user_goal
    作为类案查询文本，故仅在 user_goal 也为空时才返回空 cases。
    """
    state = {
        "user_goal": "",
        "retrieval_queries": [],
        "statutes": [],
        "cases": [],
        "plan": [],
    }
    result = parallel_retrieval(state)
    assert result["statutes"] == []
    assert result["cases"] == []


def test_parallel_retrieval_marks_plan_done():
    """parallel_retrieval 应将 plan 中 statute_retrieval / case_retrieval 标 done。"""
    from lvyan.schemas import PlanStep

    plan = [
        PlanStep(step_id="s1", action="检索相关法规", tool="statute_retrieval"),
        PlanStep(step_id="s2", action="检索类案", tool="case_retrieval"),
        PlanStep(step_id="s3", action="证据缺口分析", tool="evidence_analyzer"),
    ]
    state = {
        "user_goal": "公司辞退我",
        "retrieval_queries": [],
        "statutes": [],
        "cases": [],
        "plan": plan,
    }
    result = parallel_retrieval(state)
    updated_plan = result["plan"]
    statuses = {getattr(s, "tool", ""): getattr(s, "status", "") for s in updated_plan}
    assert statuses.get("statute_retrieval") == "done"
    assert statuses.get("case_retrieval") == "done"
    # 非检索步骤保持 pending
    assert statuses.get("evidence_analyzer") == "pending"


# ---------------------------------------------------------------------------
# 测试 2：evidence_analyzer 证据缺口
# ---------------------------------------------------------------------------
def test_evidence_analyzer_identifies_missing_evidence():
    """劳动争议案由 → 至少一个 current_status="missing"。"""
    state = {
        "case_type": "劳动争议",
        "facts": [
            Fact(fact_id="f1", category="当事人", content="公司", source="user"),
            Fact(fact_id="f2", category="行为", content="辞退", source="user"),
        ],
    }
    result = evidence_analyzer(state)

    assert "evidence_requirements" in result
    requirements = result["evidence_requirements"]
    assert isinstance(requirements, list)
    assert len(requirements) > 0, "劳动争议应生成至少一条证据要求"

    # 至少一个 missing
    statuses = [getattr(r, "current_status", "") for r in requirements]
    assert "missing" in statuses, f"应至少有一个 missing，实际：{statuses}"

    # 验证字段完整性
    for r in requirements:
        assert getattr(r, "requirement_id", "")
        assert getattr(r, "fact_to_prove", "")
        assert isinstance(getattr(r, "evidence_types", []), list)


def test_evidence_analyzer_no_case_type_returns_empty():
    """无 case_type 时返回空列表，不抛异常。"""
    state = {"case_type": None, "facts": []}
    result = evidence_analyzer(state)
    assert result["evidence_requirements"] == []


def test_evidence_analyzer_marks_met_when_evidence_provided():
    """用户提供「劳动合同」证据 → 对应证据要求应标 met。"""
    state = {
        "case_type": "劳动争议",
        "facts": [
            Fact(fact_id="f1", category="证据", content="劳动合同", source="user"),
        ],
    }
    result = evidence_analyzer(state)
    requirements = result["evidence_requirements"]
    # 找到对应「劳动合同」的 requirement，应标 met
    met_names = [
        r for r in requirements
        if getattr(r, "current_status", "") == "met"
        and any("劳动合同" in t for t in getattr(r, "evidence_types", []))
    ]
    assert len(met_names) >= 1, "持有劳动合同后该证据要求应标 met"


# ---------------------------------------------------------------------------
# 测试 3：authority_resolver 去重
# ---------------------------------------------------------------------------
def _make_authority(
    source_id: str = "a",
    title: str = "劳动合同法",
    article_number: str | None = "第十条",
    authority_level: str = "法律",
    status: str = "effective",
    effective_date: date | None = None,
    lexical_score: float = 0.5,
) -> Authority:
    """构造测试用 Authority。"""
    return Authority(
        source_id=source_id,
        title=title,
        article_number=article_number,
        article_text="测试条文",
        authority_level=authority_level,
        effective_date=effective_date,
        status=status,  # type: ignore[arg-type]
        retrieved_at=datetime.now(),
        lexical_score=lexical_score,
    )


def test_authority_resolver_dedup():
    """同 source_id + article_number 的两条 Authority 去重后只剩 1 条。"""
    state = {
        "statutes": [
            _make_authority(
                source_id="a",
                article_number="第十条",
                lexical_score=0.5,
            ),
            _make_authority(
                source_id="a",
                article_number="第十条",
                lexical_score=0.8,  # 更高分数，应保留
            ),
        ]
    }
    result = authority_resolver(state)
    assert "statutes" in result
    statutes = result["statutes"]
    assert len(statutes) == 1, f"去重后应只剩 1 条，实际：{len(statutes)}"
    # 保留的是分数更高的那条
    assert getattr(statutes[0], "lexical_score", 0) == 0.8


def test_authority_resolver_dedup_different_articles():
    """同 source_id 不同 article_number 不应被去重。"""
    state = {
        "statutes": [
            _make_authority(source_id="a", article_number="第十条"),
            _make_authority(source_id="a", article_number="第十一条"),
        ]
    }
    result = authority_resolver(state)
    assert len(result["statutes"]) == 2


# ---------------------------------------------------------------------------
# 测试 4：authority_resolver 效力层级排序
# ---------------------------------------------------------------------------
def test_authority_resolver_hierarchy_sort():
    """宪法级应排在法律级之前。"""
    state = {
        "statutes": [
            _make_authority(
                source_id="law1",
                title="某法律",
                article_number="第一条",
                authority_level="法律",
                lexical_score=0.9,
            ),
            _make_authority(
                source_id="const1",
                title="某宪法",
                article_number="第一条",
                authority_level="宪法",
                lexical_score=0.5,
            ),
        ]
    }
    result = authority_resolver(state)
    statutes = result["statutes"]
    assert len(statutes) == 2
    # 宪法在前
    assert getattr(statutes[0], "authority_level", "") == "宪法"
    assert getattr(statutes[1], "authority_level", "") == "法律"


def test_authority_resolver_full_hierarchy_order():
    """全部效力层级应按 宪法 > 法律 > 行政法规 > 司法解释 > 监察法规 > 地方性法规 排序。"""
    levels_expected = [
        "宪法",
        "法律",
        "行政法规",
        "司法解释",
        "监察法规",
        "地方性法规",
    ]
    statutes = [
        _make_authority(
            source_id=f"src_{lvl}",
            title=f"某{lvl}",
            article_number="第一条",
            authority_level=lvl,
        )
        for lvl in reversed(levels_expected)  # 反向构造以验证排序生效
    ]
    state = {"statutes": statutes}
    result = authority_resolver(state)
    sorted_statutes = result["statutes"]
    actual_levels = [getattr(s, "authority_level", "") for s in sorted_statutes]
    assert actual_levels == levels_expected, f"排序错误：{actual_levels}"


# ---------------------------------------------------------------------------
# 测试 5：authority_resolver 版本冲突检测
# ---------------------------------------------------------------------------
def test_authority_resolver_version_conflict():
    """同 title 两个不同 effective_date 的 effective 版本 → version 类型冲突。"""
    state = {
        "statutes": [
            _make_authority(
                source_id="law_v1",
                title="劳动合同法",
                article_number="第十条",
                authority_level="法律",
                status="effective",
                effective_date=date(2008, 1, 1),
            ),
            _make_authority(
                source_id="law_v2",
                title="劳动合同法",
                article_number="第十条",
                authority_level="法律",
                status="effective",
                effective_date=date(2013, 7, 1),
            ),
        ]
    }
    result = authority_resolver(state)
    conflicts = result.get("conflicts", [])
    assert isinstance(conflicts, list)
    version_conflicts = [
        c for c in conflicts if getattr(c, "conflict_type", "") == "version"
    ]
    assert len(version_conflicts) >= 1, f"应至少有 1 个 version 冲突，实际：{conflicts}"
    # 验证冲突字段
    vc = version_conflicts[0]
    assert getattr(vc, "conflict_id", "")
    assert len(getattr(vc, "authority_ids", [])) >= 2
    assert "劳动合同法" in getattr(vc, "description", "")


def test_authority_resolver_no_version_conflict_when_single_version():
    """同 title 仅一个 effective 版本 → 不应报 version 冲突。"""
    state = {
        "statutes": [
            _make_authority(
                source_id="law_v1",
                title="劳动合同法",
                article_number="第十条",
                status="effective",
                effective_date=date(2008, 1, 1),
            ),
        ]
    }
    result = authority_resolver(state)
    version_conflicts = [
        c for c in result.get("conflicts", [])
        if getattr(c, "conflict_type", "") == "version"
    ]
    assert len(version_conflicts) == 0


def test_authority_resolver_hierarchy_conflict():
    """同 title 不同 authority_level → hierarchy 类型冲突。"""
    state = {
        "statutes": [
            _make_authority(
                source_id="h1",
                title="某条例",
                article_number="第一条",
                authority_level="法律",
                status="effective",
            ),
            _make_authority(
                source_id="h2",
                title="某条例",
                article_number="第二条",
                authority_level="行政法规",
                status="effective",
            ),
        ]
    }
    result = authority_resolver(state)
    hierarchy_conflicts = [
        c for c in result.get("conflicts", [])
        if getattr(c, "conflict_type", "") == "hierarchy"
    ]
    assert len(hierarchy_conflicts) >= 1, f"应至少有 1 个 hierarchy 冲突，实际：{result.get('conflicts', [])}"


# ---------------------------------------------------------------------------
# 辅助测试：case_difference_compare
# ---------------------------------------------------------------------------
def test_case_difference_compare_returns_list():
    """case_difference_compare 应返回差异分析列表。"""
    state = {
        "case_type": "劳动争议",
        "facts": [
            Fact(fact_id="f1", category="行为", content="辞退", source="user"),
        ],
        "cases": [
            CaseAuthority(
                case_id="c1",
                court="某法院",
                case_type="劳动争议",
                brief_facts="劳动者被违法辞退",
                ruling_summary="支持经济补偿",
                similarity_score=0.8,
            ),
        ],
    }
    result = case_difference_compare(state)
    # 因 state.reasoning_result 为 None，差异作为顶层字段返回
    assert "case_differences" in result
    diffs = result["case_differences"]
    assert isinstance(diffs, list)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["case_id"] == "c1"
    assert "facts_similarity" in diff
    assert "differences" in diff


def test_case_difference_compare_empty_cases():
    """无类案时返回空差异列表。"""
    state = {"case_type": "劳动争议", "facts": [], "cases": []}
    result = case_difference_compare(state)
    assert result.get("case_differences", []) == []
