"""LangGraph 主流程状态图构建器。

构建律言 Agent 的显式状态图，节点链顺序：

    preflight → jurisdiction_triage → fact_extractor → missing_fact_assessor
        → (route_after_missing_fact)
            ├─ "ask_user"   → END（中断提问，等待用户补充）
            └─ "continue"   → planner → parallel_retrieval → authority_resolver
                              → legal_reasoner → critic
        → (route_after_critic)
            ├─ "legal_reasoner"    → legal_reasoner（回退重试，iteration+1）
            └─ "composer"         → composer（先组装初稿）
        → citation_verifier（对最终文本做引用校验）
        → (route_after_citation)
            ├─ "reretrieve" → parallel_retrieval（重检索，受策略守卫约束）
            └─ "output_guardrail" → output_guardrail
        → (route_after_output_guardrail)
            ├─ "composer" → composer（回退重写，受 MAX_OUTPUT_ITERATIONS 约束）
            └─ "end"      → END

P1-9b 修复：composer 移到 citation_verifier 之前。
旧流程 ``reasoner → critic → citation_verifier → composer`` 验证的是中间
reasoning_result 而非用户看到的最终文本。新流程先让 composer 组装初稿，
再对完整输出做引用校验，确保验证的是用户实际看到的内容。

``route_by_complexity`` 不作为主链条件边，而由 ``composer`` 内部读取
``state.complexity`` 选择输出模板（light / deep / document）；该函数亦可用于
未来在 ``jurisdiction_triage`` 之后跳过深度节点的扩展。

checkpointer 策略
-----------------
- :func:`build_graph`：默认用 ``MemorySaver``（内存 checkpointer），开箱即运行，
  适合本地开发与单元测试。
- :func:`build_graph_with_postgres`：用 **同步** ``PostgresSaver`` 持久化 checkpoint；
  供 CLI / 同步入口（``graph.invoke()``）使用。若 psycopg 未安装或数据库不可达，
  捕获异常并回退到 ``MemorySaver``，打印警告。
- :func:`build_graph_with_postgres_async`：用 **异步** ``AsyncPostgresSaver``，
  在当前事件循环中 ``await`` 初始化；供 API 异步路径（``graph.astream()``）使用。
  ``AsyncPostgresSaver`` 必须与运行时事件循环绑定，否则会话会在 ``astream`` 时挂起。

切换到 Postgres 的方式
----------------------
- 同步路径（CLI）：直接调用 ``build_graph_with_postgres(dsn)``，或在自建图时把
  ``MemorySaver()`` 替换为::

    from langgraph.checkpoint.postgres import PostgresSaver
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True)
    saver = PostgresSaver(conn)
    saver.setup()          # 首次需建表
    graph = compiled.compile(checkpointer=saver)

- 异步路径（API server）：``await build_graph_with_postgres_async(dsn)``，或::

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    import psycopg
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    saver = AsyncPostgresSaver(conn)
    await saver.setup()
    graph = compiled.compile(checkpointer=saver)

注意：``PostgresSaver`` 的异步方法（``aput``/``aget``）继承自基类会抛
``NotImplementedError``；``AsyncPostgresSaver`` 的同步方法（``put``/``get``）同理。
因此同步 / 异步路径必须使用各自对应的 saver，不可互换。
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
from lvyan.nodes.legal_answer_finalizer import legal_answer_finalizer
from lvyan.nodes.legal_reasoner import legal_reasoner
from lvyan.nodes.output_guardrail import output_guardrail
from lvyan.nodes.planner import missing_fact_assessor, planner
from lvyan.nodes.preflight import preflight
from lvyan.nodes.attachment_retriever import attachment_retriever
from lvyan.nodes.retrieve_statutes import parallel_retrieval
from lvyan.nodes.triage import jurisdiction_triage

from .routing import (
    route_after_citation,
    route_after_critic,
    route_after_missing_fact,
    route_after_output_guardrail,
)
from .state import GraphState

__all__ = ["build_graph", "build_graph_with_postgres", "build_graph_with_postgres_async", "NODE_NAMES", "PersistenceUnavailable"]

# 14 个节点名（注册顺序 = 主链顺序，不含 START/END）
NODE_NAMES: tuple[str, ...] = (
    "preflight",
    "attachment_retriever",
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
    "legal_answer_finalizer",
)


def _register_nodes(graph: StateGraph) -> None:
    """向 StateGraph 注册全部 14 个节点。"""
    graph.add_node("preflight", preflight)
    graph.add_node("attachment_retriever", attachment_retriever)
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
    graph.add_node("legal_answer_finalizer", legal_answer_finalizer)


def _wire_edges(graph: StateGraph) -> None:
    """连接主链与两条条件边。

    P1-9b 修复后节点顺序：
      critic → (route_after_critic)
        ├─ legal_reasoner（回退重试）
        └─ composer（先组装初稿）
      → citation_verifier（对最终文本做引用校验）
      → (route_after_citation)
        ├─ reretrieve → parallel_retrieval
        └─ output_guardrail
      → (route_after_output_guardrail)
        ├─ composer（回退重写）
        └─ end → END
    """
    # 主链：START → preflight → attachment_retriever → jurisdiction_triage → fact_extractor → missing_fact_assessor
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "attachment_retriever")
    graph.add_edge("attachment_retriever", "jurisdiction_triage")
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

    # P1-9b：Critic 评审后 → composer（先组装初稿）或回退 legal_reasoner
    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {"legal_reasoner": "legal_reasoner", "composer": "composer"},
    )

    # P1-9b：composer → citation_verifier（对最终文本做引用校验）
    graph.add_edge("composer", "citation_verifier")

    # 引用校验后：重检索（→ parallel_retrieval）或输出守卫（→ output_guardrail）
    graph.add_conditional_edges(
        "citation_verifier",
        route_after_citation,
        {"reretrieve": "parallel_retrieval", "output_guardrail": "output_guardrail"},
    )

    # output_guardrail → (route_after_output_guardrail)
    #   ├─ "composer"              → 回退 composer 重新生成
    #   └─ "legal_answer_finalizer" → 重建结构化输出后结束
    graph.add_conditional_edges(
        "output_guardrail",
        route_after_output_guardrail,
        {"composer": "composer", "legal_answer_finalizer": "legal_answer_finalizer"},
    )

    # legal_answer_finalizer → END
    graph.add_edge("legal_answer_finalizer", END)


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


class PersistenceUnavailable(RuntimeError):
    """P0-1：生产/强制持久化模式下 PostgreSQL checkpointer 初始化失败。

    在 development 且未强制持久化时，:func:`build_graph_with_postgres` 仍会
    回退到 MemorySaver；但在 production（或 PERSISTENCE_REQUIRED=true）下，
    任何回退都是不可接受的「半持久化」状态，必须抛出本异常让服务启动失败。
    """


def _persistence_required() -> bool:
    from lvyan.config import persistence_required

    return persistence_required()


def _resolve_backend_and_required() -> tuple[str, bool]:
    """读取 CHECKPOINTER_BACKEND 环境变量，返回 (backend, required)。"""
    import os

    backend = os.getenv("CHECKPOINTER_BACKEND", settings.checkpointer_backend).strip().lower()
    if backend not in {"memory", "postgres", "auto"}:
        raise PersistenceUnavailable(
            f"CHECKPOINTER_BACKEND='{backend}' 非法；允许值: memory / postgres / auto"
        )
    if backend == "memory":
        if _persistence_required():
            raise PersistenceUnavailable(
                "PERSISTENCE_REQUIRED=true 时禁止使用 MemorySaver；请使用 postgres"
            )
        return "memory", False
    if backend == "postgres":
        return "postgres", True
    return "auto", _persistence_required()


def build_graph_with_postgres(dsn: str | None = None) -> Any:
    """构建并返回编译后的图，优先使用 **同步** PostgreSQL checkpoint。

    本函数用于 CLI / 同步入口（``graph.invoke()``）。``PostgresSaver`` 实现了
    同步 ``put`` / ``get`` 方法。API 异步路径（``astream``）请使用
    :func:`build_graph_with_postgres_async`，因为 ``PostgresSaver`` 的异步方法
    ``aput`` / ``aget`` 继承自基类会抛 ``NotImplementedError``。

    - ``dsn`` 为空时从 ``settings.database_url`` 推导。
    - 若 psycopg / langgraph-checkpoint-postgres 未安装，或数据库不可达：
        * development 且未强制持久化 → 回退 :func:`build_graph`（MemorySaver）；
        * production / PERSISTENCE_REQUIRED=true → 抛 :class:`PersistenceUnavailable`。
    """
    backend, required = _resolve_backend_and_required()
    if backend == "memory":
        return build_graph()

    # 实时读环境变量（与 _resolve_backend_and_required 一致），
    # 避免 settings 单例在导入时冻结导致 monkeypatch 不生效。
    import os

    resolved_dsn = _to_dsn(dsn or os.getenv("DATABASE_URL", settings.database_url))
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        if required:
            raise PersistenceUnavailable(
                f"psycopg / langgraph-checkpoint-postgres 未安装（{exc}），"
                f"且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] psycopg 未安装（{exc}），回退到 MemorySaver")
        return build_graph()

    try:
        from psycopg.rows import dict_row

        conn = psycopg.connect(
            resolved_dsn,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            connect_timeout=3,
        )
        saver = PostgresSaver(conn)
        saver.setup()
    except Exception as exc:  # noqa: BLE001
        if required:
            raise PersistenceUnavailable(
                f"PostgreSQL 不可达（{exc}），且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] PostgreSQL 不可达（{exc}），回退到 MemorySaver")
        return build_graph()

    try:
        return _build_graph_with_checkpointer(saver)
    except Exception as exc:  # noqa: BLE001
        if required:
            raise PersistenceUnavailable(
                f"PostgresSaver 初始化失败（{exc}），且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] PostgresSaver 初始化失败（{exc}），回退到 MemorySaver")
        return build_graph()


async def build_graph_with_postgres_async(dsn: str | None = None) -> Any:
    """构建并返回编译后的图，使用 **异步** PostgreSQL checkpoint。

    本函数用于 API 异步路径（``graph.astream()``）。``AsyncPostgresSaver`` 实现
    了异步 ``aput`` / ``aget`` 方法，必须在运行的事件循环中 ``await`` 初始化，
    以确保 ``AsyncConnection`` 绑定到正确的循环。

    - ``dsn`` 为空时从 ``settings.database_url`` 推导。
    - 若 psycopg / langgraph-checkpoint-postgres 未安装，或数据库不可达：
        * development 且未强制持久化 → 回退 :func:`build_graph`（MemorySaver）；
        * production / PERSISTENCE_REQUIRED=true → 抛 :class:`PersistenceUnavailable`。
    """
    backend, required = _resolve_backend_and_required()
    if backend == "memory":
        return build_graph()

    # 实时读环境变量（与 _resolve_backend_and_required 一致），
    # 避免 settings 单例在导入时冻结导致 monkeypatch 不生效。
    import os

    resolved_dsn = _to_dsn(dsn or os.getenv("DATABASE_URL", settings.database_url))
    try:
        import psycopg  # noqa: F401
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        if required:
            raise PersistenceUnavailable(
                f"psycopg / langgraph-checkpoint-postgres 未安装（{exc}），"
                f"且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] psycopg 未安装（{exc}），回退到 MemorySaver")
        return build_graph()

    try:
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(
            resolved_dsn,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            connect_timeout=3,
        )
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
    except Exception as exc:  # noqa: BLE001
        if required:
            raise PersistenceUnavailable(
                f"PostgreSQL 不可达（{exc}），且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] PostgreSQL 不可达（{exc}），回退到 MemorySaver")
        return build_graph()

    try:
        return _build_graph_with_checkpointer(saver)
    except Exception as exc:  # noqa: BLE001
        if required:
            raise PersistenceUnavailable(
                f"AsyncPostgresSaver 初始化失败（{exc}），且当前为强制持久化模式，拒绝回退 MemorySaver"
            ) from exc
        print(f"[lvyan.graph] AsyncPostgresSaver 初始化失败（{exc}），回退到 MemorySaver")
        return build_graph()
