"""P0-B：corpus_manifest —— 法库 / ArticleIndex / BM25 三者一致性校验。

问题背景
--------
Docker 构建期预热生成的 ``article_index_v2.pkl`` 与 ``bm25_index.pkl`` 仅校验
``schema_version``（和 BM25 的 ``signature``）。``signature`` 由 chunks 内容计算，
而 chunks 本身来自旧缓存 —— 两层缓存链式自洽，无法发现 ``LAWTEXT_DIR`` 已更新
（submodule 升级 / 法条正文修改 / 挂载卷覆盖）。

更极端：构建时 submodule 为空 → 生成空索引 → 运行时挂载完整法库 →
``is_official_db_available()=True`` → ``/readyz=ok`` → 检索返回空结果。

解决方案
--------
引入 ``corpus_manifest.json`` 作为 **外部锚点**，记录构建时 ``LAWTEXT_DIR`` 的
内容指纹（``corpus_hash``），独立于 chunks 内容。运行时加载缓存前校验：

    当前 LAWTEXT_DIR 的 corpus_hash
            ==
    manifest 中记录的 corpus_hash

不一致则丢弃缓存、现场重建，并供 ``/readyz`` 报告 ``index_mismatch``。

Manifest schema
---------------
::

    {
      "manifest_schema_version": 1,
      "article_index_schema_version": 3,
      "corpus_hash": "<16 hex>",          # LAWTEXT_DIR 全量 .md 文件指纹
      "lawtext_file_count": 2445,         # .md 文件数
      "chunks_count": 85639,              # ArticleChunk 总数
      "chunks_signature": "<16 hex>",     # 复用 _compute_chunk_signature
      "bm25_signature": "<16 hex>",       # 同 chunks_signature（BM25 由 chunks 构建）
      "bm25_n_docs": 85639,               # BM25 文档数（= chunks_count）
      "generated_at": "<ISO8601>"
    }
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lvyan.config import AGENT_DIR, LAWTEXT_DIR

_logger = logging.getLogger("lvyan.retrieval.manifest")

MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_FILE: Path = AGENT_DIR / "knowledge" / "manifests" / "corpus_manifest.json"


def compute_corpus_hash(lawtext_dir: Path) -> str:
    """计算 LAWTEXT_DIR 内容指纹（聚合所有 .md 文件的 sha256）。

    对每个 ``.md`` 文件取相对路径 + 文件内容的 sha256，按路径排序后聚合，
    最终取前 16 hex 字符。排序保证同一法库内容（即使文件系统遍历顺序不同）
    产生相同 hash。空目录返回 ``"empty"``。
    """
    lawtext_dir = Path(lawtext_dir)
    if not lawtext_dir.is_dir():
        return "empty"

    files = sorted(lawtext_dir.rglob("*.md"))
    if not files:
        return "empty"

    h = hashlib.sha256()
    for md_file in files:
        # 相对路径确保目录迁移后 hash 不变
        try:
            rel = md_file.relative_to(lawtext_dir).as_posix()
        except ValueError:
            rel = str(md_file)
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        try:
            content = md_file.read_bytes()
            h.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def write_corpus_manifest(
    chunks: list[Any],
    lawtext_dir: Path,
    manifests_dir: Path | None = None,
) -> Path:
    """构建并落盘 corpus_manifest.json。

    Args:
        chunks: ArticleChunk 列表（构建期产物）。
        lawtext_dir: 构建时扫描的 LAWTEXT_DIR。
        manifests_dir: 输出目录（默认 ``AGENT/knowledge/manifests/``）。

    Returns:
        manifest 文件路径。
    """
    # 延迟导入避免循环依赖
    from lvyan.retrieval.lexical import (
        ARTICLE_INDEX_SCHEMA_VERSION,
        _compute_chunk_signature,
    )

    manifests_dir = Path(manifests_dir or (AGENT_DIR / "knowledge" / "manifests"))
    manifests_dir.mkdir(parents=True, exist_ok=True)

    chunks_sig = _compute_chunk_signature(chunks)
    manifest: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "article_index_schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
        "corpus_hash": compute_corpus_hash(lawtext_dir),
        "lawtext_dir": str(lawtext_dir),
        "lawtext_file_count": sum(1 for _ in Path(lawtext_dir).rglob("*.md")) if Path(lawtext_dir).is_dir() else 0,
        "chunks_count": len(chunks),
        "chunks_signature": chunks_sig,
        "bm25_signature": chunks_sig,  # BM25 由同一批 chunks 构建
        "bm25_n_docs": len(chunks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    manifest_path = manifests_dir / "corpus_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _logger.info("[manifest] 已写入 corpus_manifest.json -> %s", manifest_path)
    return manifest_path


def load_corpus_manifest(manifests_dir: Path | None = None) -> dict[str, Any] | None:
    """读取 corpus_manifest.json；不存在或损坏返回 None。"""
    manifest_path = Path(manifests_dir or (AGENT_DIR / "knowledge" / "manifests")) / "corpus_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except (OSError, json.JSONDecodeError):
        return None


def verify_corpus_consistency(
    lawtext_dir: Path | None = None,
    manifests_dir: Path | None = None,
) -> dict[str, Any]:
    """P0-B：校验法库 / ArticleIndex / BM25 三者一致性。

    返回结构::

        {
          "consistent": bool,           # 三者是否一致
          "reason": str | None,         # 不一致原因（consistent=True 时为 None）
          "manifest": dict | None,      # manifest 内容（缺失时 None）
          "current_corpus_hash": str,   # 当前 LAWTEXT_DIR 实时 hash
          "current_file_count": int,    # 当前 .md 文件数
        }

    校验维度（任一不一致即 ``consistent=False``）：

    1. **manifest 存在**：``corpus_manifest.json`` 缺失 → 不一致。
    2. **corpus_hash 匹配**：当前 ``LAWTEXT_DIR`` 的 hash 与 manifest 记录不同
       → 法库已更新，索引陈旧（``reason="lawtext_changed"``）。
    3. **chunks_count > 0**：manifest 记录的 chunks 数为 0 → 空索引（构建时
       submodule 未检出），``reason="empty_index"``。
    4. **BM25 签名一致**：``bm25_signature != chunks_signature`` → BM25 与
       article_index 不对应（``reason="signature_mismatch"``）。
    """
    lawtext_dir = Path(lawtext_dir or LAWTEXT_DIR)
    manifest = load_corpus_manifest(manifests_dir)

    current_hash = compute_corpus_hash(lawtext_dir)
    current_file_count = sum(1 for _ in lawtext_dir.rglob("*.md")) if lawtext_dir.is_dir() else 0

    result: dict[str, Any] = {
        "consistent": False,
        "reason": None,
        "manifest": manifest,
        "current_corpus_hash": current_hash,
        "current_file_count": current_file_count,
    }

    if manifest is None:
        result["reason"] = "manifest_missing"
        return result

    # manifest_schema_version 校验
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        result["reason"] = "manifest_schema_mismatch"
        return result

    # corpus_hash 校验（核心：法库内容是否与构建时一致）
    recorded_hash = manifest.get("corpus_hash", "")
    if recorded_hash != current_hash:
        result["reason"] = "lawtext_changed"
        return result

    # chunks_count > 0 校验（空索引通常是 submodule 未检出）
    chunks_count = int(manifest.get("chunks_count", 0))
    if chunks_count == 0:
        result["reason"] = "empty_index"
        return result

    # BM25 签名一致性（bm25 应由同一批 chunks 构建）
    chunks_sig = manifest.get("chunks_signature", "")
    bm25_sig = manifest.get("bm25_signature", "")
    if not chunks_sig or chunks_sig != bm25_sig:
        result["reason"] = "signature_mismatch"
        return result

    # bm25_n_docs 应等于 chunks_count
    if int(manifest.get("bm25_n_docs", 0)) != chunks_count:
        result["reason"] = "bm25_doc_count_mismatch"
        return result

    result["consistent"] = True
    return result


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "compute_corpus_hash",
    "write_corpus_manifest",
    "load_corpus_manifest",
    "verify_corpus_consistency",
]
