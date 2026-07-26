"""统一案件记忆存储：基于 LangGraph checkpointer 的单一状态源。

替代旧的 ``ShortTermMemory`` 双轨问题（JSON 文件 vs LangGraph checkpoint）。

设计要点
--------
- ``CaseMemory`` 包装 LangGraph 编译图的 checkpointer，成为唯一状态源。
- Agent 运行时状态由 checkpointer 自动保存（无需手动 save）。
- API 历史查询从同一 checkpointer 读取（``graph.get_state``）。
- 会话删除直接操作 checkpointer（``checkpointer.delete_thread``）。
- 轻量线程索引（JSON sidecar）存储 thread_id → 元数据映射，用于快速列表。

公开接口
--------
- ``CaseMemory(graph, index_path)``：构造时传入共享图实例。
- ``load(thread_id) -> CaseState | None``：从 checkpointer 加载状态。
- ``delete(thread_id) -> bool``：删除会话。
- ``list_threads() -> list[tuple[str, dict]]``：列出所有会话摘要。
- ``register(thread_id, ...)``：注册新会话到索引。
- ``mark_output(thread_id)``：标记会话已有输出。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from lvyan.config import AGENT_DIR
from lvyan.schemas.case import CaseState

_logger = logging.getLogger("lvyan.memory.store")

# 线程索引默认路径
_DEFAULT_INDEX = AGENT_DIR / "data" / "thread_index.json"


class CaseMemory:
    """统一案件记忆存储，替代 ShortTermMemory 的双轨问题。

    通过共享 LangGraph checkpointer 实现单一状态源：
    - Agent 运行时状态由 checkpointer 自动保存
    - API 历史查询从同一 checkpointer 读取
    - 会话删除直接操作 checkpointer
    - 轻量 JSON 索引存储 thread_id → 元数据（title/complexity/created_at）

    Attributes:
        graph: 共享的 LangGraph 编译图实例。
        index_path: 线程索引 JSON 文件路径。
    """

    def __init__(
        self,
        graph: Any,
        index_path: Path | str | None = None,
    ) -> None:
        self._graph = graph
        self._index_path = Path(index_path) if index_path else _DEFAULT_INDEX
        self._index: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load_index()

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
    # 公开 API
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

    def load(self, thread_id: str) -> CaseState | None:
        """从 LangGraph checkpointer 加载会话状态。

        替代旧 ``ShortTermMemory.load`` 的 JSON 文件读取。
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = self._graph.get_state(config)
            if snapshot is None or not snapshot.values:
                return None
            # 从 checkpoint values 重建 CaseState
            values = snapshot.values
            # 过滤掉 CaseState 不支持的字段
            case_fields = set(CaseState.model_fields.keys())
            filtered = {k: v for k, v in values.items() if k in case_fields}
            return CaseState.model_validate(filtered)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("加载 thread %s 状态失败: %s", thread_id, exc)
            return None

    def delete(self, thread_id: str) -> bool:
        """删除会话：从 checkpointer 和索引中移除。"""
        existed = False
        # 从 checkpointer 删除
        try:
            checkpointer = getattr(self._graph, "checkpointer", None)
            if checkpointer is not None:
                if hasattr(checkpointer, "delete_thread"):
                    checkpointer.delete_thread(thread_id)
                existed = True
        except Exception as exc:  # noqa: BLE001
            _logger.debug("从 checkpointer 删除 thread %s 失败: %s", thread_id, exc)

        # 从索引删除
        with self._lock:
            if thread_id in self._index:
                existed = True
                del self._index[thread_id]
                self._save_index()

        return existed

    def list_threads(self) -> list[tuple[str, dict[str, Any]]]:
        """列出所有会话摘要（thread_id, metadata）。"""
        with self._lock:
            return list(self._index.items())

    def has_interrupt(self, thread_id: str) -> dict[str, Any] | None:
        """检查会话是否有待处理的 HITL 中断。

        Returns:
            中断信息字典（含 message 等），无中断返回 None。
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = self._graph.get_state(config)
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
        except Exception:  # noqa: BLE001
            return None


__all__ = ["CaseMemory"]
