"""共享 LangGraph 图实例管理器。

确保 API 层、SSE runner 和 CaseMemory 使用同一个图实例（同一 checkpointer），
避免旧代码中每次 ``build_graph()`` 创建新 MemorySaver 导致状态丢失的问题。

公开接口
--------
- ``get_shared_graph()``：获取/创建共享图实例（**同步**，CLI 路径用）。
  使用 ``PostgresSaver``（同步 ``put``/``get``），失败回退 ``MemorySaver``。
- ``get_shared_graph_async()``：获取/创建共享图实例（**异步**，API server 路径用）。
  使用 ``AsyncPostgresSaver``（异步 ``aput``/``aget``），在当前事件循环中 ``await``
  初始化。与同步单例独立，因为两种 saver 不可互换。
- ``get_case_memory()``：获取绑定到共享图的 CaseMemory（优先异步单例，否则同步）。
- ``reset_shared_graph()``：重置同步/异步图与 CaseMemory（测试用）。
- ``get_checkpointer_kind()``：返回当前共享图实际使用的 checkpointer 类型
  （``"postgres"`` / ``"memory"`` / ``"unknown"``），供 ``/readyz`` 暴露（P0-1）。
"""

from __future__ import annotations

import logging
from typing import Any

from lvyan.memory.store import CaseMemory

_logger = logging.getLogger("lvyan.runtime")

# 模块级单例
_shared_graph: Any = None
# 异步图单例（API 路径用，AsyncPostgresSaver 绑定到 uvicorn 事件循环）
_shared_graph_async: Any = None
_case_memory: CaseMemory | None = None
# P0-1：记录实际 checkpointer 类型，readyz 据此判断是否处于「半持久化」状态
_checkpointer_kind: str = "unknown"


def get_shared_graph() -> Any:
    """获取或创建共享 LangGraph 图实例（同步路径，CLI 用）。

    使用 **同步** ``PostgresSaver``。若不可用回退到 ``MemorySaver``。

    P0-1：在强制持久化模式下，``build_graph_with_postgres`` 会抛
    :class:`PersistenceUnavailable`，本函数不再吞掉，让其向上传播使服务启动失败。
    """
    global _shared_graph, _checkpointer_kind
    if _shared_graph is not None:
        return _shared_graph

    from lvyan.graph import build_graph_with_postgres

    _shared_graph = build_graph_with_postgres()
    _checkpointer_kind = _detect_checkpointer_kind(_shared_graph.checkpointer)
    _logger.warning(
        "共享图实例已创建 (sync, checkpointer=%s, kind=%s)",
        type(_shared_graph.checkpointer).__name__,
        _checkpointer_kind,
    )
    return _shared_graph


async def get_shared_graph_async() -> Any:
    """获取或创建共享 LangGraph 图实例（异步路径，API server 用）。

    使用 **异步** ``AsyncPostgresSaver``，在当前事件循环中 ``await`` 初始化，
    确保 ``AsyncConnection`` 绑定到正确的循环。

    与同步 :func:`get_shared_graph` 使用独立的单例，因为 ``AsyncPostgresSaver``
    和 ``PostgresSaver`` 不可互换（前者不实现 sync ``get``，后者不实现 async ``aput``）。
    """
    global _shared_graph_async, _checkpointer_kind
    if _shared_graph_async is not None:
        return _shared_graph_async

    from lvyan.graph import build_graph_with_postgres_async

    _shared_graph_async = await build_graph_with_postgres_async()
    _checkpointer_kind = _detect_checkpointer_kind(_shared_graph_async.checkpointer)
    _logger.warning(
        "共享图实例已创建 (async, checkpointer=%s, kind=%s)",
        type(_shared_graph_async.checkpointer).__name__,
        _checkpointer_kind,
    )
    return _shared_graph_async


def _detect_checkpointer_kind(checkpointer: Any) -> str:
    """根据 checkpointer 实例类名判断后端类型。"""
    name = type(checkpointer).__name__.lower()
    if "postgres" in name:
        return "postgres"
    if "memory" in name:
        return "memory"
    return "unknown"


def get_checkpointer_kind() -> str:
    """返回当前共享图实际使用的 checkpointer 后端类型。

    若图尚未创建，返回 ``"unknown"``；调用方可据此决定是否触发创建。
    """
    return _checkpointer_kind


def _resolve_shared_graph() -> Any:
    """图解析器：优先返回异步图单例，否则返回同步图单例。

    供 :class:`CaseMemory` 延迟绑定使用。每次方法调用时实时解析，确保：

    - **API 异步路径**：``_shared_graph_async`` 在首个 API 请求触发
      :func:`get_shared_graph_async` 后就绪，CaseMemory 自动切换到异步图，
      使用 ``aget_state`` 不阻塞事件循环（修复 C1/C3）。
    - **CLI 同步路径**：``_shared_graph_async`` 始终为 ``None``，回退到
      :func:`get_shared_graph`（首次调用时按需创建同步图）。
    - **两者均未就绪**：返回 ``None``，由 CaseMemory 抛 ``RuntimeError``。

    注意：本函数不触发图的创建，仅返回已存在的单例。CLI 路径需先调用
    :func:`get_shared_graph`，API 路径需先调用 :func:`get_shared_graph_async`。
    """
    if _shared_graph_async is not None:
        return _shared_graph_async
    if _shared_graph is not None:
        return _shared_graph
    return None


def get_case_memory() -> CaseMemory:
    """获取绑定到共享图的 CaseMemory 实例（延迟绑定）。

    修复 C1/C3：CaseMemory 不再在构造时绑定特定图实例，而是通过
    :func:`_resolve_shared_graph` 解析器延迟绑定。每次 checkpoint 操作时
    实时选择当前可用的图（异步优先，同步兜底），确保：

    - API 异步路径写入异步图后，CaseMemory 读取也走异步图（数据一致）
    - CLI 同步路径读写均走同步图（行为不变）
    - MemorySaver 回退场景下两路径共享同一进程内实例（仅当通过本函数
      获取 CaseMemory 时；测试中注入独立 graph 的桩不受影响）

    异步端点应使用 CaseMemory 的 ``aload_strict`` / ``adelete_strict`` /
    ``alist_threads_strict`` 异步方法，避免同步 ``get_state`` 阻塞事件循环。
    """
    global _case_memory
    if _case_memory is not None:
        return _case_memory
    _case_memory = CaseMemory(graph_resolver=_resolve_shared_graph)
    return _case_memory


def reset_shared_graph() -> None:
    """重置共享图和 CaseMemory（测试隔离用）。"""
    global _shared_graph, _shared_graph_async, _case_memory, _checkpointer_kind
    _shared_graph = None
    _shared_graph_async = None
    _case_memory = None
    _checkpointer_kind = "unknown"


__all__ = [
    "get_shared_graph",
    "get_shared_graph_async",
    "get_case_memory",
    "reset_shared_graph",
    "get_checkpointer_kind",
    "_resolve_shared_graph",
]
