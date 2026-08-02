"""LangGraph 状态图与 checkpoint 恢复单元测试。

覆盖 Task 3 验证标准：
1. ``build_graph`` 返回编译后的图。
2. 图包含全部 12 个节点。
3. 条件路由函数可调用且返回值正确。
4. ``PolicyViolationError`` 可被抛出和捕获。
5. checkpoint 恢复：节点中断后，之前节点写入的追加语义字段（``facts``）不丢失，
   且恢复后不重复追加。
6. ``build_graph_with_postgres`` 在 Postgres 不可达时回退到 MemorySaver。
"""

from __future__ import annotations

from datetime import date

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from lvyan.graph import (
    NODE_NAMES,
    PolicyViolationError,
    build_graph,
    build_graph_with_postgres,
    enforce_policies,
    route_after_citation,
    route_after_missing_fact,
    route_by_complexity,
)
from lvyan.graph import builder as builder_module
from lvyan.schemas import (
    CaseState,
    CitationAudit,
    Fact,
    MissingFact,
)
from lvyan.schemas.output import CitationDetail


# ---------------------------------------------------------------------------
# 辅助：构造完整初始 GraphState dict
# ---------------------------------------------------------------------------
def _initial_state(thread_id: str = "thread-cp-test") -> dict:
    """构造包含 GraphState 全部字段的初始状态。"""
    return {
        "run_id": "run-cp-test",
        "thread_id": thread_id,
        "current_date": date(2026, 7, 23),
        "user_goal": "测试 checkpoint 恢复",
        "jurisdiction": None,
        "case_type": None,
        "complexity": "light",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": [],
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": None,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "insufficient",
        "iteration": 0,
        "final_output": None,
    }


# ---------------------------------------------------------------------------
# 1. build_graph 返回编译后的图
# ---------------------------------------------------------------------------
def test_build_graph_returns_compiled_graph():
    g = build_graph()
    assert isinstance(g, CompiledStateGraph)


# ---------------------------------------------------------------------------
# 2. 图包含全部 13 个节点
# ---------------------------------------------------------------------------
def test_graph_contains_all_twelve_nodes():
    g = build_graph()
    graph_nodes = set(g.get_graph().nodes.keys())
    # 13 个业务节点全部注册
    for name in NODE_NAMES:
        assert name in graph_nodes, f"节点 {name} 未注册到图中"
    assert len(NODE_NAMES) == 13


# ---------------------------------------------------------------------------
# 3. 条件路由函数可调用且返回值正确
# ---------------------------------------------------------------------------
def _state(**kwargs) -> CaseState:
    base = dict(
        run_id="r",
        thread_id="t",
        current_date=date(2026, 7, 23),
        user_goal="g",
    )
    base.update(kwargs)
    return CaseState(**base)


def test_route_after_missing_fact_blocking_returns_ask_user():
    state = _state(missing_facts=[MissingFact(fact_key="k", question="q?", reason="r", is_blocking=True)])
    assert route_after_missing_fact(state) == "ask_user"


def test_route_after_missing_fact_nonblocking_returns_continue():
    state = _state(missing_facts=[MissingFact(fact_key="k", question="q?", reason="r", is_blocking=False)])
    assert route_after_missing_fact(state) == "continue"


def test_route_after_missing_fact_empty_returns_continue():
    assert route_after_missing_fact(_state()) == "continue"


def test_route_after_citation_failed_within_budget_returns_reretrieve():
    audit = CitationAudit(
        passed=False,
        total_citations=1,
        verified=0,
        fabricated=1,
        repealed_cited=0,
        unsupported=0,
        details=[],
    )
    state = _state(citation_audit=audit, iteration=1)
    assert route_after_citation(state) == "reretrieve"


def test_route_after_citation_failed_at_budget_cap_returns_compose():
    audit = CitationAudit(
        passed=False,
        total_citations=1,
        verified=0,
        fabricated=1,
        repealed_cited=0,
        unsupported=0,
        details=[],
    )
    # iteration 达到 MAX_RETRIEVAL_ITERATIONS(=3)，不再重检索
    # P1-9b：路由目标改为 output_guardrail
    state = _state(citation_audit=audit, iteration=3)
    assert route_after_citation(state) == "output_guardrail"


def test_route_after_citation_passed_returns_compose():
    audit = CitationAudit(
        passed=True,
        total_citations=1,
        verified=1,
        fabricated=0,
        repealed_cited=0,
        unsupported=0,
        details=[
            CitationDetail(citation_text="《民法典》第六百八十条", status="verified"),
        ],
    )
    # P1-9b：路由目标改为 output_guardrail
    assert route_after_citation(_state(citation_audit=audit, iteration=0)) == "output_guardrail"


def test_route_after_citation_no_audit_returns_compose():
    # P1-9b：路由目标改为 output_guardrail
    assert route_after_citation(_state()) == "output_guardrail"


def test_route_by_complexity_returns_expected_mode():
    assert route_by_complexity(_state(complexity="light")) == "light"
    assert route_by_complexity(_state(complexity="deep")) == "deep"
    assert route_by_complexity(_state(complexity="document")) == "document"


# ---------------------------------------------------------------------------
# 4. PolicyViolationError 可被抛出和捕获
# ---------------------------------------------------------------------------
def test_policy_violation_raised_when_retrieval_budget_exhausted():
    # iteration=99 远超 MAX_RETRIEVAL_ITERATIONS=3
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(_state(iteration=99))
    assert exc_info.value.kind == "retrieval_budget"


def test_policy_violation_raised_when_loop_detected():
    from lvyan.schemas import RetrievalQuery

    q = RetrievalQuery(query_id="q1", query_text="重复查询", route="bm25")
    state = _state(
        retrieval_queries=[q, q, q],  # 同一 query_text 出现 3 次
        iteration=0,
    )
    with pytest.raises(PolicyViolationError) as exc_info:
        enforce_policies(state)
    assert exc_info.value.kind == "loop"


def test_policy_passes_when_within_budgets():
    # iteration=0，无循环，应在预算内不抛出
    enforce_policies(_state(iteration=0))


def test_policy_violation_is_runtime_error_subclass():
    assert issubclass(PolicyViolationError, RuntimeError)


# ---------------------------------------------------------------------------
# 5. checkpoint 恢复：中断后状态不丢失，恢复后不重复追加
# ---------------------------------------------------------------------------
def test_checkpoint_restore_preserves_facts(monkeypatch):
    """legal_reasoner 中断后，fact_extractor 写入的 facts 不丢失；恢复后不重复。"""
    raise_flag = {"raise": True}
    test_fact = Fact(
        fact_id="test-f1",
        category="当事人",
        content="测试事实：原告张三",
        source="user",
        confidence=0.9,
    )

    def fact_extractor_stub(state):  # noqa: ANN001
        """写入一条测试事实，模拟真实 fact_extractor 行为。"""
        return {"facts": [test_fact]}

    def legal_reasoner_stub(state):  # noqa: ANN001
        """根据 raise_flag 决定是否 raise，模拟节点中断。"""
        if raise_flag["raise"]:
            raise RuntimeError("模拟 legal_reasoner 节点失败")
        return {}

    # 替换 builder 模块中的桩函数引用（_register_nodes 按模块级名字查找）
    monkeypatch.setattr(builder_module, "fact_extractor", fact_extractor_stub)
    monkeypatch.setattr(builder_module, "legal_reasoner", legal_reasoner_stub)

    graph = builder_module.build_graph()
    config = {"configurable": {"thread_id": "thread-cp-test"}}
    initial = _initial_state()

    # 第一次执行：跑到 legal_reasoner 时 raise
    with pytest.raises(RuntimeError, match="模拟 legal_reasoner"):
        graph.invoke(initial, config)

    # 检查 checkpoint：fact_extractor 已完成，facts 应已持久化
    snapshot = graph.get_state(config)
    assert snapshot is not None
    assert snapshot.values is not None
    persisted_facts = snapshot.values.get("facts", [])
    assert any(
        (f.get("fact_id") if isinstance(f, dict) else f.fact_id) == "test-f1"
        for f in persisted_facts
    ), "中断后 checkpoint 应保留 fact_extractor 写入的 facts"
    # 下一步应从 legal_reasoner 恢复
    assert "legal_reasoner" in snapshot.next

    # 修复 legal_reasoner，从 checkpoint 恢复执行
    raise_flag["raise"] = False
    result = graph.invoke(None, config)

    # 恢复后 facts 仍包含测试事实
    final_facts = result["facts"]
    assert any(
        (f.get("fact_id") if isinstance(f, dict) else f.fact_id) == "test-f1"
        for f in final_facts
    ), "恢复后 facts 应仍包含测试事实"

    # 追加语义 + checkpoint：fact_extractor 不重新执行，facts 不应重复
    test_fact_count = sum(
        1 for f in final_facts
        if (f.get("fact_id") if isinstance(f, dict) else f.fact_id) == "test-f1"
    )
    assert test_fact_count == 1, "恢复后 fact_extractor 不应重新执行，测试事实不应重复"


def test_checkpoint_writes_use_memory_saver_by_default():
    """build_graph 默认使用 MemorySaver，可在中断后通过 get_state 读取 checkpoint。"""
    raise_flag = {"raise": True}

    def fact_extractor_stub(state):  # noqa: ANN001
        return {"facts": [Fact(fact_id="f-mem", category="金额", content="100元", source="user")]}

    def critic_stub(state):  # noqa: ANN001
        if raise_flag["raise"]:
            raise RuntimeError("critic 失败")
        return {}

    # 直接构建一个最小可运行图，验证 MemorySaver checkpoint 可读
    from langgraph.graph import END, START, StateGraph

    from lvyan.graph.state import GraphState
    from lvyan.nodes.preflight import preflight
    from lvyan.nodes.triage import jurisdiction_triage
    from lvyan.nodes.planner import missing_fact_assessor, planner
    from lvyan.nodes.retrieve_statutes import parallel_retrieval
    from lvyan.nodes.evidence_analyzer import authority_resolver
    from lvyan.nodes.legal_reasoner import legal_reasoner
    from lvyan.nodes.citation_verifier import citation_verifier
    from lvyan.nodes.composer import composer
    from lvyan.nodes.output_guardrail import output_guardrail
    from lvyan.graph.routing import route_after_missing_fact, route_after_citation, route_after_critic

    g = StateGraph(GraphState)
    g.add_node("preflight", preflight)
    g.add_node("jurisdiction_triage", jurisdiction_triage)
    g.add_node("fact_extractor", fact_extractor_stub)
    g.add_node("missing_fact_assessor", missing_fact_assessor)
    g.add_node("planner", planner)
    g.add_node("parallel_retrieval", parallel_retrieval)
    g.add_node("authority_resolver", authority_resolver)
    g.add_node("legal_reasoner", legal_reasoner)
    g.add_node("critic", critic_stub)
    g.add_node("citation_verifier", citation_verifier)
    g.add_node("composer", composer)
    g.add_node("output_guardrail", output_guardrail)

    g.add_edge(START, "preflight")
    g.add_edge("preflight", "jurisdiction_triage")
    g.add_edge("jurisdiction_triage", "fact_extractor")
    g.add_edge("fact_extractor", "missing_fact_assessor")
    g.add_conditional_edges(
        "missing_fact_assessor", route_after_missing_fact,
        {"ask_user": END, "continue": "planner"},
    )
    g.add_edge("planner", "parallel_retrieval")
    g.add_edge("parallel_retrieval", "authority_resolver")
    g.add_edge("authority_resolver", "legal_reasoner")
    g.add_edge("legal_reasoner", "critic")
    # P1-9b：critic → composer → citation_verifier → output_guardrail
    g.add_conditional_edges(
        "critic", route_after_critic,
        {"legal_reasoner": "legal_reasoner", "composer": "composer"},
    )
    g.add_edge("composer", "citation_verifier")
    g.add_conditional_edges(
        "citation_verifier", route_after_citation,
        {"reretrieve": "parallel_retrieval", "output_guardrail": "output_guardrail"},
    )
    g.add_edge("output_guardrail", END)

    app = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "thread-mem"}}
    with pytest.raises(RuntimeError, match="critic 失败"):
        app.invoke(_initial_state("thread-mem"), cfg)

    snap = app.get_state(cfg)
    assert snap.values is not None
    assert any(
        (f.get("fact_id") if isinstance(f, dict) else f.fact_id) == "f-mem"
        for f in snap.values.get("facts", [])
    )


# ---------------------------------------------------------------------------
# 6. build_graph_with_postgres 在 Postgres 不可达时回退到 MemorySaver
# ---------------------------------------------------------------------------
def test_build_graph_with_postgres_falls_back_to_memory_saver(capsys):
    """本机无运行中的 Postgres，应回退到 MemorySaver 并打印警告，返回可用图。"""
    # 指向一个肯定不可达的 DSN，避免依赖环境
    g = build_graph_with_postgres("postgresql://nobody:nobody@127.0.0.1:1/nowhere")
    # 应回退为编译后的图（MemorySaver）
    assert isinstance(g, CompiledStateGraph)
    captured = capsys.readouterr()
    assert "回退到 MemorySaver" in captured.out


def test_build_graph_with_postgres_invalid_dsn_still_returns_graph(capsys):
    g = build_graph_with_postgres("not-a-valid-dsn")
    assert isinstance(g, CompiledStateGraph)
