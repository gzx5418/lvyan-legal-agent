"""共享 LangGraph 图实例管理器。

确保 API 层、SSE runner 和 CaseMemory 使用同一个图实例（同一 checkpointer），
避免旧代码中每次 ``build_graph()`` 创建新 MemorySaver 导致状态丢失的问题。

公开接口
--------
- ``get_shared_graph()``：获取/创建共享图实例（优先 Postgres，回退 MemorySaver）。
- ``get_case_memory()``：获取绑定到共享图的 CaseMemory。
- ``reset_shared_graph()``：重置（测试用）。
"""

from __future__ import annotations

import logging
from typing import Any

from lvyan.memory.store import CaseMemory

_logger = logging.getLogger("lvyan.runtime")

# 模块级单例
_shared_graph: Any = None
_case_memory: CaseMemory | None = None


def get_shared_graph() -> Any:
    """获取或创建共享 LangGraph 图实例。

    优先使用 PostgreSQL checkpointer（生产级持久化 + 多实例部署），
    不可用时回退到 MemorySaver（本地开发，进程内持久化）。
    """
    global _shared_graph
    if _shared_graph is not None:
        return _shared_graph

    from lvyan.graph import build_graph_with_postgres

    _shared_graph = build_graph_with_postgres()
    _logger.info(
        "共享图实例已创建 (checkpointer=%s)",
        type(_shared_graph.checkpointer).__name__,
    )
    return _shared_graph


def get_case_memory() -> CaseMemory:
    """获取绑定到共享图的 CaseMemory 实例。"""
    global _case_memory
    if _case_memory is not None:
        return _case_memory
    graph = get_shared_graph()
    _case_memory = CaseMemory(graph=graph)
    return _case_memory


def reset_shared_graph() -> None:
    """重置共享图和 CaseMemory（测试隔离用）。"""
    global _shared_graph, _case_memory
    _shared_graph = None
    _case_memory = None


__all__ = ["get_shared_graph", "get_case_memory", "reset_shared_graph"]
