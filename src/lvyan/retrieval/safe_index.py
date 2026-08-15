"""安全索引存储：替代 Pickle 的 MsgPack 格式索引文件读写。

格式规范 (LVIX v1)
------------------
文件结构：
    [4B magic "LVIX"] [1B version=1] [4B header_len (big-endian)]
    [header: MsgPack dict] [payload: MsgPack object]

header 字段：
    - schema_version (int): 索引 schema 版本号
    - corpus_hash (str): 语料哈希（SHA-256 前16字符）
    - payload_sha256 (str): payload 区域的 SHA-256 hex
    - item_count (int): 主集合元素数量
    - created_at (str): ISO 格式创建时间
    - format_version (int): 文件格式版本 (1)

安全限制：
    - payload 最大 500MB
    - item_count 最大 10,000,000
    - header_len 最大 4KB
    - 读取时验证 payload_sha256 完整性

写入策略：
    临时文件 → fsync → os.replace 原子替换
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msgpack

__all__ = ["SafeIndexStore", "IndexCorruptedError", "IndexVersionMismatchError"]

_logger = logging.getLogger("lvyan.retrieval.safe_index")

# 格式常量
_MAGIC = b"LVIX"
_FORMAT_VERSION = 1
_HEADER_STRUCT = struct.Struct(">I")  # 4字节 big-endian unsigned int

# 安全限制
_MAX_PAYLOAD_BYTES = 500 * 1024 * 1024  # 500MB
_MAX_ITEM_COUNT = 10_000_000
_MAX_HEADER_BYTES = 4096


class IndexCorruptedError(Exception):
    """索引文件损坏或被篡改。"""


class IndexVersionMismatchError(Exception):
    """索引 schema 版本不匹配。"""


class SafeIndexStore:
    """安全索引文件的读写器。

    使用 MsgPack 序列化，带完整性校验和版本控制。
    不执行任意代码（与 Pickle 的核心区别）。
    """

    @staticmethod
    def save(
        path: str | Path,
        data: Any,
        *,
        corpus_hash: str,
        schema_version: int,
        item_count: int | None = None,
    ) -> None:
        """将数据写入安全索引文件。

        Args:
            path: 目标文件路径（扩展名建议 .lvix）。
            data: 要序列化的数据（必须是 MsgPack 兼容类型）。
            corpus_hash: 语料哈希标识（用于校验索引是否与源数据匹配）。
            schema_version: 索引 schema 版本号。
            item_count: 主集合元素数量（用于安全限制校验）；
                        None 时尝试从 data 推断。

        Raises:
            ValueError: 参数不合法（如 item_count 超限）。
            OSError: 文件写入失败。
        """
        path = Path(path)

        if item_count is None:
            if isinstance(data, (list, tuple)):
                item_count = len(data)
            elif isinstance(data, dict) and "chunks" in data:
                chunks = data.get("chunks")
                item_count = len(chunks) if isinstance(chunks, (list, tuple)) else 0
            else:
                item_count = 1

        if item_count > _MAX_ITEM_COUNT:
            raise ValueError(f"item_count ({item_count}) 超过安全限制 ({_MAX_ITEM_COUNT})")

        payload_bytes = msgpack.packb(data, use_bin_type=True)
        if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload 大小 ({len(payload_bytes)} bytes) 超过安全限制 "
                f"({_MAX_PAYLOAD_BYTES} bytes)"
            )

        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        header = {
            "schema_version": schema_version,
            "corpus_hash": str(corpus_hash),
            "payload_sha256": payload_sha256,
            "item_count": item_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "format_version": _FORMAT_VERSION,
        }
        header_bytes = msgpack.packb(header, use_bin_type=True)

        if len(header_bytes) > _MAX_HEADER_BYTES:
            raise ValueError(
                f"header 大小 ({len(header_bytes)} bytes) 超过限制 ({_MAX_HEADER_BYTES})"
            )

        path.parent.mkdir(parents=True, exist_ok=True)

        # 原子写入：临时文件 → fsync → rename
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".lvix.tmp")
            with os.fdopen(fd, "wb") as f:
                fd = None  # os.fdopen 接管了 fd
                f.write(_MAGIC)
                f.write(struct.pack("B", _FORMAT_VERSION))
                f.write(_HEADER_STRUCT.pack(len(header_bytes)))
                f.write(header_bytes)
                f.write(payload_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
            tmp_path = None
            _logger.debug(
                "索引写入完成: %s (schema_v=%d, items=%d, size=%d)",
                path.name,
                schema_version,
                item_count,
                len(payload_bytes),
            )
        finally:
            if fd is not None:
                os.close(fd)
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def load(
        path: str | Path,
        *,
        expected_schema_version: int | None = None,
    ) -> dict[str, Any] | None:
        """安全读取索引文件。

        Args:
            path: 索引文件路径。
            expected_schema_version: 期望的 schema 版本号；不匹配时返回 None。

        Returns:
            包含 header 和 data 的字典：
            ``{"header": {...}, "data": <deserialized payload>}``
            文件不存在或校验失败返回 None。

        Raises:
            IndexCorruptedError: 文件存在但完整性校验失败（SHA-256 不匹配）。
            IndexVersionMismatchError: schema_version 不匹配。
        """
        path = Path(path)
        if not path.is_file():
            return None

        # 读取前预检文件大小：文件头（magic 4B + version 1B + header_len 4B）
        # + header 上限 + payload 上限之外的文件直接拒绝，避免先全量读入
        # 数百 MB 甚至更大的损坏/恶意文件后再做大小检查。
        try:
            file_size = path.stat().st_size
        except OSError:
            _logger.warning("索引文件状态读取失败: %s", path.name)
            return None
        if file_size > len(_MAGIC) + 1 + 4 + _MAX_HEADER_BYTES + _MAX_PAYLOAD_BYTES:
            _logger.warning(
                "索引文件大小超限: %s (%d bytes)", path.name, file_size
            )
            return None

        try:
            with open(path, "rb") as f:
                # 读取 magic
                magic = f.read(4)
                if magic != _MAGIC:
                    _logger.warning("索引文件 magic 不匹配: %s", path.name)
                    return None

                # 读取格式版本
                version_byte = f.read(1)
                if len(version_byte) != 1:
                    return None
                fmt_version = struct.unpack("B", version_byte)[0]
                if fmt_version != _FORMAT_VERSION:
                    _logger.warning(
                        "索引格式版本不支持: %d (期望 %d)", fmt_version, _FORMAT_VERSION
                    )
                    return None

                # 读取 header 长度
                header_len_bytes = f.read(4)
                if len(header_len_bytes) != 4:
                    return None
                header_len = _HEADER_STRUCT.unpack(header_len_bytes)[0]
                if header_len > _MAX_HEADER_BYTES:
                    _logger.warning("header 长度超限: %d", header_len)
                    return None

                # 读取 header
                header_bytes = f.read(header_len)
                if len(header_bytes) != header_len:
                    return None
                header = msgpack.unpackb(header_bytes, raw=False)

                if not isinstance(header, dict):
                    _logger.warning("header 不是字典类型")
                    return None

                # schema 版本校验
                schema_ver = header.get("schema_version")
                if expected_schema_version is not None and schema_ver != expected_schema_version:
                    raise IndexVersionMismatchError(
                        f"schema_version={schema_ver}, 期望={expected_schema_version}"
                    )

                # item_count 安全校验
                item_count = header.get("item_count", 0)
                if item_count > _MAX_ITEM_COUNT:
                    _logger.warning("item_count 超限: %d", item_count)
                    return None

                # 读取 payload
                payload_bytes = f.read()
                if len(payload_bytes) > _MAX_PAYLOAD_BYTES:
                    _logger.warning("payload 大小超限: %d", len(payload_bytes))
                    return None

                # SHA-256 完整性校验
                expected_sha = header.get("payload_sha256", "")
                actual_sha = hashlib.sha256(payload_bytes).hexdigest()
                if actual_sha != expected_sha:
                    raise IndexCorruptedError(
                        f"payload SHA-256 不匹配: 期望={expected_sha[:16]}..., "
                        f"实际={actual_sha[:16]}..."
                    )

                # 反序列化 payload
                data = msgpack.unpackb(payload_bytes, raw=False)

                _logger.debug(
                    "索引加载完成: %s (schema_v=%d, items=%d)",
                    path.name,
                    schema_ver,
                    item_count,
                )
                return {"header": header, "data": data}

        except (IndexCorruptedError, IndexVersionMismatchError):
            raise
        except (OSError, msgpack.exceptions.UnpackException) as exc:
            _logger.warning("索引文件读取失败 (%s): %s", path.name, exc)
            return None

    @staticmethod
    def is_valid(path: str | Path) -> bool:
        """快速校验索引文件是否有效（仅检查 magic + 版本，不校验 payload）。"""
        path = Path(path)
        if not path.is_file():
            return False
        try:
            with open(path, "rb") as f:
                magic = f.read(4)
                if magic != _MAGIC:
                    return False
                version_byte = f.read(1)
                if len(version_byte) != 1:
                    return False
                return struct.unpack("B", version_byte)[0] == _FORMAT_VERSION
        except OSError:
            return False
