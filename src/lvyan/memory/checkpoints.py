"""短期记忆：按 thread_id 保存会话状态（CaseState）。

当前实现为文件系统持久化桩，序列化 ``CaseState`` 到
``AGENT/knowledge/manifests/checkpoints/{thread_id}.json``。

保存内容覆盖 Task 19.1 要求的「短期记忆」要素：
- 用户已提供的事实（``facts``）
- 已检索法条（``statutes`` 中的 ``source_id`` 列表）
- 已确认的管辖地（``jurisdiction``）
- 当前计划（``plan``）
- citation audit 结果（``citation_audit``）

后续接入 PostgreSQL checkpoint 时，只需替换 ``_write`` / ``_read`` 两个私有方法即可。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..config import AGENT_DIR
from ..schemas.case import CaseState

# ---------------------------------------------------------------------------
# 持久化根目录：AGENT/knowledge/manifests/checkpoints/
# ---------------------------------------------------------------------------
# 优先读取环境变量 MANIFESTS_DIR（便于测试隔离），否则使用默认路径。
_DEFAULT_BASE = AGENT_DIR / "knowledge" / "manifests"
_CHECKPOINT_DIR = Path(os.getenv("MANIFESTS_DIR", str(_DEFAULT_BASE))) / "checkpoints"

# 模块级可重入互斥锁：保证同进程内多线程并发写文件时的简单线程安全。
# 跨进程安全不在本桩的范围（PostgreSQL 接入后由数据库事务保证）。
_LOCK = threading.RLock()


class ShortTermMemory:
    """短期记忆：按 ``thread_id`` 持久化 ``CaseState``。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        # 允许调用方注入 base_dir，便于测试隔离；默认使用模块级常量。
        self._base_dir = Path(base_dir) if base_dir is not None else _CHECKPOINT_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def save(self, thread_id: str, state: CaseState) -> None:
        """保存状态到 ``{base_dir}/{thread_id}.json``。

        会同步写盘（``fsync``）以保证中断后可恢复。
        注：PostgreSQL checkpoint 已通过 ``build_graph_with_postgres`` 接入
        （详见 ``graph/builder.py``），本文件系统桩用于无 PG 的开发/测试环境。
        """
        path = self._path_of(thread_id)
        payload = state.model_dump(mode="json")
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        with _LOCK:
            # 先写临时文件再原子替换，避免写一半崩溃产生半截 JSON。
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    def load(self, thread_id: str) -> CaseState | None:
        """加载状态；不存在时返回 ``None``。"""
        path = self._path_of(thread_id)
        if not path.exists():
            return None
        with _LOCK:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        # CaseState 内嵌 date/datetime 等类型，Pydantic v2 可从 JSON 字符串还原。
        return CaseState.model_validate(payload)

    def delete(self, thread_id: str) -> None:
        """删除指定 thread 的 checkpoint；不存在时静默忽略。"""
        path = self._path_of(thread_id)
        with _LOCK:
            if path.exists():
                path.unlink()

    def list_threads(self) -> list[str]:
        """列出所有已持久化的 ``thread_id``（按文件名，去后缀）。"""
        if not self._base_dir.exists():
            return []
        threads: list[str] = []
        with _LOCK:
            for p in sorted(self._base_dir.iterdir()):
                if p.is_file() and p.suffix == ".json":
                    threads.append(p.stem)
        return threads

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _path_of(self, thread_id: str) -> Path:
        # 对 thread_id 做基本清洗，防止路径穿越（thread_id 由系统生成，但仍需防御）。
        safe = thread_id.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
        if safe in ("", ".", ".."):
            safe = "_invalid_"
        return self._base_dir / f"{safe}.json"


__all__ = ["ShortTermMemory"]
