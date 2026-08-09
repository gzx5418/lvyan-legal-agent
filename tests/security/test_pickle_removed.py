"""验证源码不再包含 pickle.load/loads，且恶意 Pickle 文件无法被加载。

退出条件验证：
1. 源码中无 pickle.load / pickle.loads
2. SafeIndexStore 拒绝非 LVIX 格式文件（含恶意 pickle）
3. .pkl 文件存在不影响正常索引加载
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. 源码中不存在 pickle.load / pickle.loads
# ---------------------------------------------------------------------------
def test_no_pickle_load_in_source():
    """确认 src/ 中不存在运行时 pickle.load / pickle.loads 调用。"""
    src_dir = Path(__file__).resolve().parents[2] / "src"
    violations: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # 跳过注释行
            if stripped.startswith("#"):
                continue
            if "pickle.load(" in line or "pickle.loads(" in line:
                rel = py_file.relative_to(src_dir)
                violations.append(f"{rel}:{i}: {stripped}")

    assert not violations, f"源码中发现 {len(violations)} 处 pickle.load/loads:\n" + "\n".join(
        violations
    )


def test_no_pickle_import_in_source():
    """确认 src/ 中无直接 import pickle 语句（排除测试和注释）。"""
    src_dir = Path(__file__).resolve().parents[2] / "src"
    violations: list[str] = []

    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "import pickle" in stripped:
                rel = py_file.relative_to(src_dir)
                violations.append(f"{rel}:{i}: {stripped}")

    assert not violations, f"源码中发现 {len(violations)} 处 import pickle:\n" + "\n".join(
        violations
    )


# ---------------------------------------------------------------------------
# 2. SafeIndexStore 拒绝恶意 Pickle 文件
# ---------------------------------------------------------------------------
class _MaliciousPayload:
    """恶意 pickle payload：反序列化时执行 os.system。"""

    def __reduce__(self):
        return (os.system, ("echo PWNED",))


def test_safe_index_rejects_pickle_file(tmp_path: Path):
    """SafeIndexStore.load() 不会执行恶意 pickle 文件。"""
    from lvyan.retrieval.safe_index import SafeIndexStore

    malicious_file = tmp_path / "evil.lvix"
    malicious_file.write_bytes(pickle.dumps(_MaliciousPayload()))

    result = SafeIndexStore.load(malicious_file)
    # 非 LVIX 格式 → 返回 None，不执行
    assert result is None


def test_safe_index_rejects_tampered_payload(tmp_path: Path):
    """payload SHA-256 被篡改后 load() 抛出 IndexCorruptedError。"""
    from lvyan.retrieval.safe_index import SafeIndexStore, IndexCorruptedError

    # 先写入合法文件
    test_data = {"key": "value", "items": [1, 2, 3]}
    path = tmp_path / "test.lvix"
    SafeIndexStore.save(path, test_data, corpus_hash="abc123", schema_version=1)

    # 篡改 payload 区域的最后一字节
    content = bytearray(path.read_bytes())
    content[-1] ^= 0xFF
    path.write_bytes(bytes(content))

    with pytest.raises(IndexCorruptedError):
        SafeIndexStore.load(path, expected_schema_version=1)


def test_safe_index_rejects_oversized_item_count(tmp_path: Path):
    """item_count 超过安全限制时 save() 拒绝。"""
    from lvyan.retrieval.safe_index import SafeIndexStore

    with pytest.raises(ValueError, match="item_count.*超过安全限制"):
        SafeIndexStore.save(
            tmp_path / "big.lvix",
            {"data": "x"},
            corpus_hash="test",
            schema_version=1,
            item_count=20_000_000,
        )


# ---------------------------------------------------------------------------
# 3. 正常 SafeIndexStore 功能测试
# ---------------------------------------------------------------------------
def test_safe_index_roundtrip(tmp_path: Path):
    """正常 save/load 往返。"""
    from lvyan.retrieval.safe_index import SafeIndexStore

    data = {
        "schema_version": 3,
        "chunks": [{"text": "测试条文", "source": "民法典"}],
    }
    path = tmp_path / "test.lvix"

    SafeIndexStore.save(path, data, corpus_hash="deadbeef", schema_version=3, item_count=1)
    assert path.is_file()

    result = SafeIndexStore.load(path, expected_schema_version=3)
    assert result is not None
    assert result["data"] == data
    assert result["header"]["corpus_hash"] == "deadbeef"
    assert result["header"]["item_count"] == 1


def test_safe_index_version_mismatch(tmp_path: Path):
    """schema_version 不匹配时抛出 IndexVersionMismatchError。"""
    from lvyan.retrieval.safe_index import SafeIndexStore, IndexVersionMismatchError

    path = tmp_path / "test.lvix"
    SafeIndexStore.save(path, {"x": 1}, corpus_hash="a", schema_version=2)

    with pytest.raises(IndexVersionMismatchError):
        SafeIndexStore.load(path, expected_schema_version=99)


def test_safe_index_is_valid(tmp_path: Path):
    """is_valid() 快速格式检查。"""
    from lvyan.retrieval.safe_index import SafeIndexStore

    valid_path = tmp_path / "good.lvix"
    SafeIndexStore.save(valid_path, [1, 2], corpus_hash="x", schema_version=1)
    assert SafeIndexStore.is_valid(valid_path) is True

    invalid_path = tmp_path / "bad.lvix"
    invalid_path.write_bytes(b"NOT_LVIX_FORMAT")
    assert SafeIndexStore.is_valid(invalid_path) is False

    assert SafeIndexStore.is_valid(tmp_path / "nonexistent.lvix") is False
