"""统一案件记忆存储：基于 LangGraph checkpointer 的单一状态源。

替代旧的 ``ShortTermMemory`` 双轨问题（JSON 文件 vs LangGraph checkpoint）。

设计要点
--------
- ``CaseMemory`` 包装 LangGraph 编译图的 checkpointer，成为唯一状态源。
- Agent 运行时状态由 checkpointer 自动保存（无需手动 save）。
- API 历史查询从同一 checkpointer 读取（``graph.get_state`` / ``graph.aget_state``）。
- 会话删除直接操作 checkpointer（``checkpointer.delete_thread``）。
- 轻量线程索引（JSON sidecar）存储 thread_id → 元数据，用于快速列表。

同步 / 异步双路径支持
----------------------
LangGraph 的 ``PostgresSaver`` 仅实现同步 ``get_state``，``AsyncPostgresSaver``
仅实现异步 ``aget_state``，两者不可互换。CaseMemory 通过 ``graph_resolver``
延迟解析当前应使用的图实例：

- **API 异步路径**：``runtime._shared_graph_async`` 已就绪时优先使用，调用
  ``aget_state`` 不阻塞事件循环。
- **CLI 同步路径**：回退到 ``runtime._shared_graph``，调用同步 ``get_state``。

``aload_strict`` / ``adelete_str`` / ``alist_threads`` 为异步端点专用，使用
异步图方法；``load_strict`` / ``delete_strict`` / ``list_threads`` 保留给
CLI / 测试桩等同步路径，向后兼容。

公开接口
--------
- ``CaseMemory(graph_resolver, index_path)``：构造时传入图解析器（延迟绑定）。
- ``CaseMemory(graph=...)``：旧式构造（立即绑定），向后兼容。
- ``load(thread_id) -> CaseState | None``：从 checkpointer 加载状态（同步）。
- ``aload_strict(thread_id) -> CaseState | None``：异步严格加载（API 用）。
- ``delete(thread_id) -> bool``：删除会话（同步）。
- ``adelete_strict(thread_id) -> bool``：异步严格删除（API 用）。
- ``list_threads() -> list[tuple[str, dict]]``：列出所有会话摘要。
- ``register(thread_id, ...)``：注册新会话到索引。
- ``mark_output(thread_id)``：标记会话已有输出。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from lvyan.config import AGENT_DIR
from lvyan.schemas.case import CaseState

_logger = logging.getLogger("lvyan.memory.store")

# 线程索引默认路径
_DEFAULT_INDEX = AGENT_DIR / "data" / "thread_index.json"

# 图解析器类型：返回当前应使用的 LangGraph 编译图实例。
# 实现可根据运行时单例状态选择异步图（API 路径）或同步图（CLI 路径）。
GraphResolver = Callable[[], Any]


class CaseMemory:
    """统一案件记忆存储，替代 ShortTermMemory 的双轨问题。

    通过共享 LangGraph checkpointer 实现单一状态源：
    - Agent 运行时状态由 checkpointer 自动保存
    - API 历史查询从同一 checkpointer 读取（异步路径用 ``aget_state``）
    - 会话删除直接操作 checkpointer
    - 轻量 JSON 索引存储 thread_id → 元数据（title/complexity/created_at）

    图实例解析策略（修复同步/异步图绑定问题）：
        - 优先使用 ``graph_resolver()`` 返回的图（延迟绑定，支持运行时切换）。
        - 回退到构造时传入的 ``graph``（立即绑定，向后兼容 / 测试用）。
        - 解析失败抛 ``RuntimeError``，由调用方处理。

    Attributes:
        graph_resolver: 图解析器 callable，返回当前应使用的图实例。
        graph: 立即绑定的图实例（向后兼容，与 graph_resolver 二选一）。
        index_path: 线程索引 JSON 文件路径。
    """

    def __init__(
        self,
        graph: Any = None,
        index_path: Path | str | None = None,
        *,
        graph_resolver: GraphResolver | None = None,
    ) -> None:
        self._graph = graph
        self._graph_resolver = graph_resolver
        self._index_path = Path(index_path) if index_path else _DEFAULT_INDEX
        self._index: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load_index()

    # ------------------------------------------------------------------
    # 图实例解析
    # ------------------------------------------------------------------
    def _resolve_graph(self) -> Any:
        """解析当前应使用的图实例。

        优先使用 ``graph_resolver``（延迟绑定，支持同步/异步图动态切换）；
        否则回退到构造时传入的 ``graph``（向后兼容）。

        Raises:
            RuntimeError: 两种来源均不可用时抛出。
        """
        if self._graph_resolver is not None:
            graph = self._graph_resolver()
            if graph is not None:
                return graph
        if self._graph is not None:
            return self._graph
        raise RuntimeError(
            "CaseMemory 未绑定图实例：graph_resolver 返回 None 且未传入 graph"
        )

    # ------------------------------------------------------------------
    # 索引持久化
    # ------------------------------------------------------------------
    def _load_index(self) -> None:
        """从 JSON 文件加载线程索引。"""
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, "r", encoding="utf-8") as fh:
                self._index = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("加载线程索引失败: %s", exc)
            self._index = {}

    def _save_index(self) -> None:
        """保存线程索引到 JSON 文件。"""
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._index_path)

    # ------------------------------------------------------------------
    # 公开 API：索引操作（线程安全，无图依赖）
    # ------------------------------------------------------------------
    def register(
        self,
        thread_id: str,
        title: str = "",
        complexity: str = "light",
        user_id: str = "anonymous",
    ) -> None:
        """注册新会话到索引；已存在时仅更新可变字段，不覆盖创建时间。

        P1-6 / P2-13 修复：
          - 原 register 无条件覆盖 ``created_at`` / ``has_output``，导致用户
            「继续」会话后标题被截短、创建时间被刷新、``has_output`` 被重置。
          - 现区分新建 vs 更新；并记录 ``user_id`` 字段供 ownership 过滤。
        """
        with self._lock:
            existing = self._index.get(thread_id)
            if existing is not None:
                existing["complexity"] = complexity
                existing["updated_at"] = time.time()
                # user_id 不覆盖（防止恶意篡改归属）
                existing.setdefault("user_id", user_id)
                old_title = existing.get("title", "")
                if title and (not old_title or old_title == thread_id):
                    existing["title"] = title
            else:
                self._index[thread_id] = {
                    "title": title or thread_id,
                    "complexity": complexity,
                    "user_id": user_id,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "has_output": False,
                }
            self._save_index()

    def mark_output(self, thread_id: str) -> None:
        """标记会话已有最终输出。"""
        with self._lock:
            if thread_id in self._index:
                self._index[thread_id]["has_output"] = True
                self._save_index()

    def list_threads(self) -> list[tuple[str, dict[str, Any]]]:
        """列出所有会话摘要（thread_id, metadata）。"""
        with self._lock:
            return list(self._index.items())

    # ------------------------------------------------------------------
    # 公开 API：checkpoint 读取 / 删除（同步路径，CLI 用）
    # ------------------------------------------------------------------
    def load(self, thread_id: str) -> CaseState | None:
        """从 LangGraph checkpointer 加载会话状态（同步路径）。

        替代旧 ``ShortTermMemory.load`` 的 JSON 文件读取。
        """
        try:
            return self.load_strict(thread_id)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("加载 thread %s 状态失败: %s", thread_id, exc)
            return None

    def load_strict(self, thread_id: str) -> CaseState | None:
        """加载会话状态（同步路径，CLI 用），将 checkpointer 故障传播给调用方。

        使用 ``graph.get_state()``。注意：当 ``graph_resolver`` 返回异步图
        （``AsyncPostgresSaver``）时，其 ``get_state`` 继承自基类会抛
        ``NotImplementedError``。API 异步端点请使用 :meth:`aload_strict`。
        """
        config = {"configurable": {"thread_id": thread_id}}
        graph = self._resolve_graph()
        snapshot = graph.get_state(config)
        return _snapshot_to_state(snapshot)

    def delete(self, thread_id: str) -> bool:
        """删除会话：从 checkpointer 和索引中移除（同步路径）。"""
        try:
            return self.delete_strict(thread_id)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("从 checkpointer 删除 thread %s 失败: %s", thread_id, exc)
            return False

    def delete_strict(self, thread_id: str) -> bool:
        """严格删除 checkpoint 与索引（同步路径），任何存储故障均向调用方传播。

        使用 ``checkpointer.delete_thread()``。异步图（``AsyncPostgresSaver``）
        的 ``delete_thread`` 不可用，API 异步端点请使用 :meth:`adelete_strict`。
        """
        graph = self._resolve_graph()
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is None or not hasattr(checkpointer, "delete_thread"):
            raise RuntimeError("checkpointer delete_thread unavailable")

        checkpointer.delete_thread(thread_id)

        # 从索引删除
        with self._lock:
            if thread_id in self._index:
                del self._index[thread_id]
                self._save_index()

        return True

    def has_interrupt(self, thread_id: str) -> dict[str, Any] | None:
        """检查会话是否有待处理的 HITL 中断（同步路径）。

        Returns:
            中断信息字典（含 message 等），无中断返回 None。
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            graph = self._resolve_graph()
            snapshot = graph.get_state(config)
            return _snapshot_interrupt(snapshot)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 公开 API：checkpoint 读取 / 删除（异步路径，API server 用）
    # ------------------------------------------------------------------
    async def aload(self, thread_id: str) -> CaseState | None:
        """从 LangGraph checkpointer 加载会话状态（异步路径，API 用）。

        优先使用 ``graph.aget_state()``（异步图，不阻塞事件循环）；
        图实例未实现 ``aget_state`` 时回退到 ``asyncio.to_thread`` 包装同步
        ``get_state``（兼容 MemorySaver 等仅同步实现的 checkpointer）。
        """
        try:
            return await self.aload_strict(thread_id)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("异步加载 thread %s 状态失败: %s", thread_id, exc)
            return None

    async def aload_strict(self, thread_id: str) -> CaseState | None:
        """异步严格加载会话状态（API 用），将 checkpointer 故障传播给调用方。

        使用 ``await graph.aget_state()``。当 ``graph_resolver`` 返回的图未实现
        ``aget_state`` 时（如 MemorySaver 场景），回退到 ``asyncio.to_thread``
        包装同步 ``get_state``，确保所有 checkpointer 后端均可工作。
        """
        config = {"configurable": {"thread_id": thread_id}}
        graph = self._resolve_graph()
        aget = getattr(graph, "aget_state", None)
        if callable(aget):
            snapshot = await aget(config)
        else:
            # 回退：MemorySaver 等仅同步实现的 checkpointer，用线程池卸载避免阻塞
            snapshot = await asyncio.to_thread(graph.get_state, config)
        return _snapshot_to_state(snapshot)

    async def adelete_strict(self, thread_id: str) -> bool:
        """异步严格删除 checkpoint 与索引（API 用）。

        ``checkpointer.delete_thread`` 为同步操作（LangGraph 未提供异步版本），
        通过 ``asyncio.to_thread`` 卸载到线程池，避免阻塞事件循环。
        """
        graph = self._resolve_graph()
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is None or not hasattr(checkpointer, "delete_thread"):
            raise RuntimeError("checkpointer delete_thread unavailable")

        await asyncio.to_thread(checkpointer.delete_thread, thread_id)

        # 从索引删除
        with self._lock:
            if thread_id in self._index:
                del self._index[thread_id]
                self._save_index()

        return True

    async def alist_threads_strict(self) -> list[tuple[str, dict[str, Any]]]:
        """异步列出所有可恢复的会话摘要（API 用）。

        对每个索引项调用 :meth:`aload_strict` 验证 checkpoint 仍存在，
        仅返回可恢复的会话。``asyncio.to_thread`` 包装索引读取避免锁竞争。
        """
        index_snapshot: list[tuple[str, dict[str, Any]]] = await asyncio.to_thread(
            self.list_threads
        )
        recoverable: list[tuple[str, dict[str, Any]]] = []
        for tid, meta in index_snapshot:
            try:
                if await self.aload_strict(tid) is not None:
                    recoverable.append((tid, meta))
            except Exception as exc:  # noqa: BLE001
                _logger.debug("异步列出 thread %s 失败: %s", tid, exc)
        return recoverable


# ---------------------------------------------------------------------------
# 辅助函数：snapshot → CaseState / 中断信息
# ---------------------------------------------------------------------------
def _snapshot_to_state(snapshot: Any) -> CaseState | None:
    """从 graph.get_state / aget_state 返回的 snapshot 重建 CaseState。"""
    if snapshot is None or not snapshot.values:
        return None
    values = snapshot.values
    # 过滤掉 CaseState 不支持的字段
    case_fields = set(CaseState.model_fields.keys())
    filtered = {k: v for k, v in values.items() if k in case_fields}
    return CaseState.model_validate(filtered)


def _snapshot_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从 snapshot 解析 HITL 中断信息（无中断返回 None）。"""
    if snapshot is None:
        return None
    # 有待执行节点 = 图被中断
    if snapshot.next:
        # 尝试获取 interrupt 信息
        tasks = getattr(snapshot, "tasks", {})
        for task in tasks.values() if isinstance(tasks, dict) else tasks:
            interrupts = getattr(task, "interrupts", [])
            if interrupts:
                return interrupts[0].value if interrupts else None
        # 回退：返回通用中断信息
        return {"message": "Agent 等待人工确认", "pending_nodes": list(snapshot.next)}
    return None


__all__ = ["CaseMemory", "GraphResolver"]
