"""LangGraph 主流程状态图构建器。

构建律言 Agent 的显式状态图，节点链顺序：

    preflight → jurisdiction_triage → fact_extractor → missing_fact_assessor
        → (route_after_missing_fact)
            ├─ "ask_user"   → END（中断提问，等待用户补充）
            └─ "continue"   → planner → parallel_retrieval → authority_resolver
                              → legal_reasoner → critic
        → (route_after_critic)
            ├─ "legal_reasoner"    → legal_reasoner（回退重试，iteration+1）
            └─ "citation_verifier" → citation_verifier
        → (route_after_citation)
            ├─ "reretrieve" → parallel_retrieval（重检索，受策略守卫约束）
            └─ "compose"    → composer → output_guardrail
        → (route_after_output_guardrail)
            ├─ "composer" → composer（回退重写，受 MAX_OUTPUT_ITERATIONS 约束）
            └─ "end"      → END

``route_by_complexity`` 不作为主链条件边，而由 ``composer`` 内部读取
``state.complexity`` 选择输出模板（light / deep / document）；该函数亦可用于
未来在 ``jurisdiction_triage`` 之后跳过深度节点的扩展。

checkpointer 策略
-----------------
- :func:`build_graph`：默认用 ``MemorySaver``（内存 checkpointer），开箱即运行，
  适合本地开发与单元测试。
- :func:`build_graph_with_postgres`：尝试用 ``PostgresSaver`` 持久化 checkpoint；
  若 psycopg / langgraph-checkpoint-postgres 未安装或数据库不可达，捕获异常并
  回退到 ``MemorySaver``，打印警告。

切换到 Postgres 的方式
----------------------
生产部署时直接调用 ``build_graph_with_postgres(dsn)``，或在自建图时把
``MemorySaver()`` 替换为::

    from langgraph.checkpoint.postgres import PostgresSaver
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True)
    saver = PostgresSaver(conn)
    saver.setup()          # 首次需建表
    graph = compiled.compile(checkpointer=saver)
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from lvyan.config import settings
from lvyan.nodes.citation_verifier import citation_verifier
from lvyan.nodes.composer import composer
from lvyan.nodes.critic import critic
from lvyan.nodes.evidence_analyzer import authority_resolver
from lvyan.nodes.fact_extractor import fact_extractor
from lvyan.nodes.legal_reasoner import legal_reasoner
from lvyan.nodes.output_guardrail import output_guardrail
from lvyan.nodes.planner import missing_fact_assessor, planner
from lvyan.nodes.preflight import preflight
from lvyan.nodes.retrieve_statutes import parallel_retrieval
from lvyan.nodes.triage import jurisdiction_triage

from .routing import (
    route_after_citation,
    route_after_critic,
    route_after_missing_fact,
    route_after_output_guardrail,
)
from .state import GraphState

__all__ = ["build_graph", "build_graph_with_postgres", "NODE_NAMES"]

# 12 个节点名（注册顺序 = 主链顺序，不含 START/END）
NODE_NAMES: tuple[str, ...] = (
    "preflight",
    "jurisdiction_triage",
    "fact_extractor",
    "missing_fact_assessor",
    "planner",
    "parallel_retrieval",
    "authority_resolver",
    "legal_reasoner",
    "critic",
    "citation_verifier",
    "composer",
    "output_guardrail",
)


def _register_nodes(graph: StateGraph) -> None:
    """向 StateGraph 注册全部 12 个节点。"""
    graph.add_node("preflight", preflight)
    graph.add_node("jurisdiction_triage", jurisdiction_triage)
    graph.add_node("fact_extractor", fact_extractor)
    graph.add_node("missing_fact_assessor", missing_fact_assessor)
    graph.add_node("planner", planner)
    graph.add_node("parallel_retrieval", parallel_retrieval)
    graph.add_node("authority_resolver", authority_resolver)
    graph.add_node("legal_reasoner", legal_reasoner)
    graph.add_node("critic", critic)
    graph.add_node("citation_verifier", citation_verifier)
    graph.add_node("composer", composer)
    graph.add_node("output_guardrail", output_guardrail)


def _wire_edges(graph: StateGraph) -> None:
    """连接主链与两条条件边。"""
    # 主链：START → preflight → jurisdiction_triage → fact_extractor → missing_fact_assessor
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "jurisdiction_triage")
    graph.add_edge("jurisdiction_triage", "fact_extractor")
    graph.add_edge("fact_extractor", "missing_fact_assessor")

    # 缺失事实评估后：阻断提问（→ END）或继续规划（→ planner）
    graph.add_conditional_edges(
        "missing_fact_assessor",
        route_after_missing_fact,
        {"ask_user": END, "continue": "planner"},
    )

    # planner → parallel_retrieval → authority_resolver → legal_reasoner → critic
    graph.add_edge("planner", "parallel_retrieval")
    graph.add_edge("parallel_retrieval", "authority_resolver")
    graph.add_edge("authority_resolver", "legal_reasoner")
    graph.add_edge("legal_reasoner", "critic")

    # Critic 评审后：回退 legal_reasoner（重试）或进入 citation_verifier
    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {"legal_reasoner": "legal_reasoner", "citation_verifier": "citation_verifier"},
    )

    # 引用校验后：重检索（→ parallel_retrieval）或组装（→ composer）
    graph.add_conditional_edges(
        "citation_verifier",
        route_after_citation,
        {"reretrieve": "parallel_retrieval", "compose": "composer"},
    )

    # composer → output_guardrail → (route_after_output_guardrail)
    #   ├─ "composer" → 回退 composer 重新生成（受 MAX_OUTPUT_ITERATIONS 约束）
    #   └─ "end"      → END
    graph.add_edge("composer", "output_guardrail")
    graph.add_conditional_edges(
        "output_guardrail",
        route_after_output_guardrail,
        {"composer": "composer", "end": END},
    )


def _build_graph_with_checkpointer(checkpointer: Any) -> Any:
    """用给定 checkpointer 构建、连接并编译图，返回编译后的可执行图。"""
    graph: StateGraph = StateGraph(GraphState)
    _register_nodes(graph)
    _wire_edges(graph)
    return graph.compile(checkpointer=checkpointer)


def build_graph() -> Any:
    """构建并返回编译后的图（使用 ``MemorySaver`` 内存 checkpointer）。

    返回值可直接 ``graph.invoke(initial_state, {"configurable": {"thread_id": ...}})``。
    """
    return _build_graph_with_checkpointer(MemorySaver())


def _to_dsn(url: str) -> str:
    """将 SQLAlchemy 风格连接串转为 psycopg 原生 DSN。

    ``postgresql+psycopg://...`` → ``postgresql://...``；其余原样返回。
    """
    prefix = "postgresql+psycopg://"
    if url.startswith(prefix):
        return "postgresql://" + url[len(prefix):]
    return url


def build_graph_with_postgres(dsn: str | None = None) -> Any:
    """构建并返回编译后的图，优先使用 PostgreSQL checkpoint。

    - ``dsn`` 为空时从 ``settings.database_url`` 推导（自动去掉 SQLAlchemy 的
      ``+psycopg`` 前缀）。
    - 若 psycopg / langgraph-checkpoint-postgres 未安装，或数据库不可达，
      捕获异常并回退到 :func:`build_graph`（MemorySaver），打印警告。
    """
    resolved_dsn = _to_dsn(dsn or settings.database_url)
    try:
        import psycopg  # noqa: F401  仅探测是否可用
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        print(
            f"[lvyan.graph] psycopg / langgraph-checkpoint-postgres 未安装"
            f"（{exc}），回退到 MemorySaver"
        )
        return build_graph()

    try:
        conn = psycopg.connect(resolved_dsn, autocommit=True)
    except Exception as exc:  # noqa: BLE001 连接失败需宽口径捕获
        print(
            f"[lvyan.graph] PostgreSQL 不可达（{exc}），回退到 MemorySaver"
        )
        return build_graph()

    try:
        saver = PostgresSaver(conn)
        saver.setup()  # 首次运行需建 checkpoint 表
        return _build_graph_with_checkpointer(saver)
    except Exception as exc:  # noqa: BLE001 setup / 编译失败需宽口径捕获
        print(
            f"[lvyan.graph] PostgresSaver 初始化失败（{exc}），回退到 MemorySaver"
        )
        try:
            conn.close()
        except Exception:  # noqa: S110, BLE001 关闭失败无需上报
            pass
        return build_graph()
