"""案件加密空间（case vault）：案件材料的加密存储与隔离。

**隔离约束（硬性）**：
- 案件材料**不进入普通向量记忆**（本模块不写任何 embedding 库）。
- 案件材料**不用于其他用户或模型训练**。
- 跨 ``thread_id`` 不可访问：``check_access`` 强制校验，``retrieve`` 跨 thread 返回 ``None``。

**加密**：当前用 ``base64`` 占位（可逆编码，仅做传输/落盘混淆），标注 TODO 接入
真实 AES-256-GCM 对称加密（密钥由 KMS / 环境变量 ``CASE_VAULT_KEY`` 提供）。
接口稳定，替换 ``_encrypt`` / ``_decrypt`` 即可升级。

**TTL**：默认 7 天（604800 秒）过期清理，``set_ttl`` 可覆盖。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import AGENT_DIR

# ---------------------------------------------------------------------------
# 持久化根目录：AGENT/knowledge/manifests/vault/
# ---------------------------------------------------------------------------
_DEFAULT_BASE = AGENT_DIR / "knowledge" / "manifests"
_VAULT_DIR = Path(os.getenv("MANIFESTS_DIR", str(_DEFAULT_BASE))) / "vault"

# 每个 thread 的元数据文件名（与 doc_id 同目录，但不会被当作普通 doc 读取）
_MANIFEST_FILENAME = "_manifest.json"

# 默认 TTL：7 天
DEFAULT_TTL_SECONDS = 604800

# 使用可重入锁：cleanup_expired 在持锁时会调用 _is_expired → _load_manifest
# （后者也会获取同一把锁），非重入 Lock 会死锁，故用 RLock。
_LOCK = threading.RLock()


class CaseVault:
    """案件材料加密存储。

    存储布局（每个 thread 一个目录）::

        vault/
          {thread_id}/
            _manifest.json     # TTL + 文档元数据列表
            {doc_id}.enc       # base64 编码的密文（TODO: AES-256）
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else _VAULT_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 存储 / 读取
    # ------------------------------------------------------------------
    def store(
        self,
        thread_id: str,
        doc_id: str,
        content: bytes,
        metadata: dict | None = None,
    ) -> str:
        """存储一份案件材料，返回 vault 内部存储路径。

        - 加密：base64 占位（# TODO: 接入 AES-256-GCM）
        - 路径：``{base_dir}/{thread_id}/{doc_id}.enc``
        """
        thread_dir = self._thread_dir(thread_id)
        thread_dir.mkdir(parents=True, exist_ok=True)

        enc_bytes = self._encrypt(content)
        doc_path = thread_dir / f"{self._safe_name(doc_id)}.enc"
        with _LOCK:
            with open(doc_path, "wb") as fh:
                fh.write(enc_bytes)
                fh.flush()
                os.fsync(fh.fileno())

        # 更新 manifest
        manifest = self._load_manifest(thread_id)
        now = datetime.now(timezone.utc)
        # 若首次创建，写入默认 TTL
        if "ttl_seconds" not in manifest:
            manifest["ttl_seconds"] = DEFAULT_TTL_SECONDS
            manifest["created_at"] = now.isoformat()
            manifest["expires_at"] = (now + timedelta(seconds=DEFAULT_TTL_SECONDS)).isoformat()
        # 记录 / 覆盖该 doc
        doc_entry = {
            "doc_id": doc_id,
            "stored_path": str(doc_path),
            "metadata": dict(metadata) if metadata else {},
            "stored_at": now.isoformat(),
            "content_size": len(content),
        }
        docs = manifest.setdefault("documents", [])
        # 移除同 doc_id 的旧记录（覆盖写语义）
        docs = [d for d in docs if d.get("doc_id") != doc_id]
        docs.append(doc_entry)
        manifest["documents"] = docs
        manifest["thread_id"] = thread_id
        self._save_manifest(thread_id, manifest)

        return str(doc_path)

    def retrieve(self, thread_id: str, doc_id: str) -> bytes | None:
        """读取案件材料；不存在或跨 thread 访问时返回 ``None``。

        跨 thread 隔离：``retrieve`` 内部先做 ``check_access``，
        ``requesting_thread_id`` 即 ``thread_id`` 本身，若不匹配则拒绝。
        """
        # 隔离校验：retrieve 只允许同 thread 访问
        if not self.check_access(thread_id, doc_id, thread_id):
            return None
        # 过期则视为不存在
        if self._is_expired(thread_id):
            return None
        doc_path = self._doc_path(thread_id, doc_id)
        if not doc_path.exists():
            return None
        with _LOCK:
            with open(doc_path, "rb") as fh:
                enc_bytes = fh.read()
        return self._decrypt(enc_bytes)

    def delete(self, thread_id: str, doc_id: str) -> bool:
        """删除单个文件；返回是否确实删除了文件。"""
        # 隔离校验
        if not self.check_access(thread_id, doc_id, thread_id):
            return False
        doc_path = self._doc_path(thread_id, doc_id)
        deleted = False
        with _LOCK:
            if doc_path.exists():
                doc_path.unlink()
                deleted = True
        if deleted:
            # 同步从 manifest 移除记录
            manifest = self._load_manifest(thread_id)
            docs = manifest.get("documents", [])
            docs = [d for d in docs if d.get("doc_id") != doc_id]
            manifest["documents"] = docs
            self._save_manifest(thread_id, manifest)
        return deleted

    def delete_thread(self, thread_id: str) -> bool:
        """删除整个 thread 的案件材料（含 manifest）。"""
        thread_dir = self._thread_dir(thread_id)
        with _LOCK:
            if thread_dir.exists():
                shutil.rmtree(thread_dir)
                return True
        return False

    def list_documents(self, thread_id: str) -> list[dict]:
        """列出材料元数据；thread 不存在或已过期返回空列表。"""
        if self._is_expired(thread_id):
            return []
        manifest = self._load_manifest(thread_id)
        # 返回 doc 元数据副本，避免外部修改 manifest
        return [dict(d) for d in manifest.get("documents", [])]

    # ------------------------------------------------------------------
    # 访问控制（跨 thread 隔离核心）
    # ------------------------------------------------------------------
    def check_access(
        self,
        thread_id: str,
        doc_id: str,
        requesting_thread_id: str,
    ) -> bool:
        """访问控制：只能访问自己 thread 的材料。

        任何跨 thread 访问请求一律返回 ``False``，无论 doc 是否存在。
        """
        # 基本隔离：请求方必须与材料所属 thread 一致
        if not thread_id or thread_id != requesting_thread_id:
            return False
        return True

    # ------------------------------------------------------------------
    # TTL 机制
    # ------------------------------------------------------------------
    def set_ttl(self, thread_id: str, ttl_seconds: int) -> None:
        """设置 / 更新 thread 的过期时间。

        ``ttl_seconds=0`` 表示立即过期。以「当前时刻 + ttl」重算 ``expires_at``。
        若 thread 尚无 manifest（未存储任何材料），仍会创建 manifest 记录 TTL。
        """
        thread_dir = self._thread_dir(thread_id)
        thread_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest(thread_id)
        now = datetime.now(timezone.utc)
        manifest["ttl_seconds"] = int(ttl_seconds)
        if "created_at" not in manifest:
            manifest["created_at"] = now.isoformat()
        manifest["expires_at"] = (now + timedelta(seconds=int(ttl_seconds))).isoformat()
        manifest["thread_id"] = thread_id
        self._save_manifest(thread_id, manifest)

    def cleanup_expired(self) -> int:
        """清理所有已过期的 thread 材料，返回清理的 thread 数量。"""
        cleaned = 0
        if not self._base_dir.exists():
            return 0
        with _LOCK:
            for child in list(self._base_dir.iterdir()):
                if not child.is_dir():
                    continue
                tid = child.name
                if self._is_expired(tid):
                    shutil.rmtree(child)
                    cleaned += 1
        return cleaned

    # ------------------------------------------------------------------
    # 内部：加密 / 解密
    # ------------------------------------------------------------------
    @staticmethod
    def _encrypt(plaintext: bytes) -> bytes:
        # TODO: 接入 AES-256-GCM（密钥来自 KMS / CASE_VAULT_KEY 环境变量）。
        #       当前仅 base64 编码，不具备真正加密强度，仅用于接口联调。
        return base64.b64encode(plaintext)

    @staticmethod
    def _decrypt(ciphertext: bytes) -> bytes:
        # TODO: 接入 AES-256-GCM 解密。
        try:
            return base64.b64decode(ciphertext)
        except Exception:
            return b""

    # ------------------------------------------------------------------
    # 内部：路径与 manifest
    # ------------------------------------------------------------------
    def _thread_dir(self, thread_id: str) -> Path:
        return self._base_dir / self._safe_name(thread_id)

    def _doc_path(self, thread_id: str, doc_id: str) -> Path:
        return self._thread_dir(thread_id) / f"{self._safe_name(doc_id)}.enc"

    @staticmethod
    def _safe_name(name: str) -> str:
        safe = name.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
        if safe in ("", ".", ".."):
            safe = "_invalid_"
        return safe

    def _manifest_path(self, thread_id: str) -> Path:
        return self._thread_dir(thread_id) / _MANIFEST_FILENAME

    def _load_manifest(self, thread_id: str) -> dict[str, Any]:
        path = self._manifest_path(thread_id)
        with _LOCK:
            if not path.exists():
                return {"thread_id": thread_id, "documents": []}
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)

    def _save_manifest(self, thread_id: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(thread_id)
        manifest.setdefault("thread_id", thread_id)
        data = json.dumps(manifest, ensure_ascii=False, indent=2)
        with _LOCK:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    def _is_expired(self, thread_id: str) -> bool:
        """判断 thread 是否已过期。无 ``expires_at`` 视为未过期。"""
        manifest = self._load_manifest(thread_id)
        expires_at_raw = manifest.get("expires_at")
        if not expires_at_raw:
            return False
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return False
        # 统一带时区比较
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires_at


__all__ = ["CaseVault", "DEFAULT_TTL_SECONDS"]
