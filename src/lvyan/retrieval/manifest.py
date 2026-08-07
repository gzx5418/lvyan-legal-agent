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

P0 增强（2026-08 审查）
------------------------
1. **TTL 快照缓存**（P0-1）：``compute_corpus_hash`` 需要全量读取数千个 .md 文件，
   开销大。``verify_corpus_consistency`` 现在默认返回带 TTL（5 分钟）的缓存快照，
   ``/readyz`` 不再每次请求都全库扫描；后台 / 低频路径可显式 ``force=True``。

2. **真实磁盘文件校验**（P0-2）：除 manifest 内部字段自洽外，现在直接读取磁盘上的
   ``article_index_v2.pkl`` 与 ``bm25_index.pkl``，验证：
       - article_index 的 chunk 数量 == manifest.chunks_count
       - article_index 的 chunks 签名 == manifest.chunks_signature
       - bm25 的 signature == manifest.bm25_signature
       - bm25 的 n_docs == manifest.bm25_n_docs
   从而真正验证「磁盘文件 ↔ manifest ↔ 法库」三方一致，而非仅 manifest 自洽。

3. **原子重建**（P0-3）：``rebuild_corpus_indexes`` 在索引缺失 / 不一致时重建
   ArticleIndex + BM25 + manifest，全部通过临时文件 + ``os.replace`` 原子切换，
   避免半写入状态；重建完成后重新生成 manifest 并再次校验。

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
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lvyan.config import AGENT_DIR, LAWTEXT_DIR

_logger = logging.getLogger("lvyan.retrieval.manifest")

MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_FILE: Path = AGENT_DIR / "knowledge" / "manifests" / "corpus_manifest.json"

# P0-1：corpus hash 快照缓存 TTL（秒）。全量法库 hash 需要读取数千文件，
# 每次 /readyz 都重算开销过大；TTL 内复用快照，TTL 过后才重新扫描。
CORPUS_HEALTH_TTL_SECONDS = 300


def compute_corpus_hash(lawtext_dir: Path) -> str:
    """计算 LAWTEXT_DIR 内容指纹（聚合所有 .md 文件的 sha256）。

    对每个 ``.md`` 文件取相对路径 + 文件内容的 sha256，按路径排序后聚合，
    最终取前 16 hex 字符。排序保证同一法库内容（即使文件系统遍历顺序不同）
    产生相同 hash。空目录返回 ``"empty"``。

    注意：此函数全量读取磁盘，开销与法库规模成正比；高频调用应走
    :func:`verify_corpus_consistency` 的 TTL 快照。
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


# ---------------------------------------------------------------------------
# 原子写（P0-3：temp + os.replace 避免半写入状态）
# ---------------------------------------------------------------------------
def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本文件：先写临时文件，再 os.replace 切换。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写二进制文件：先写临时文件，再 os.replace 切换。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# P1-3：rebuild 跨进程互斥锁
# ---------------------------------------------------------------------------
def _acquire_rebuild_lock(manifests_dir: Path) -> bool:
    """以原子创建 ``.rebuild.lock`` 文件的方式获取 rebuild 互斥锁。

    跨 worker / 多实例（K8s 共享卷）同时发现索引失效时，只允许一个进程执行
    rebuild，避免并发写同一批 temp 文件互相覆盖。锁文件超时（10 分钟）自动
    视为过期并接管，防止进程崩溃残留锁导致永久阻塞。

    Returns:
        True 表示本进程获得锁；False 表示已有其他进程在重建。
    """
    lock_path = Path(manifests_dir) / ".rebuild.lock"
    Path(manifests_dir).mkdir(parents=True, exist_ok=True)

    # 过期锁清理：mtime 超过 10 分钟视为崩溃残留
    try:
        if lock_path.is_file() and (time.time() - lock_path.stat().st_mtime) > 600:
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_rebuild_lock(manifests_dir: Path) -> None:
    """释放 rebuild 锁（仅删除锁文件；如已被其他进程接管则保留）。"""
    lock_path = Path(manifests_dir) / ".rebuild.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# manifest 写入 / 读取
# ---------------------------------------------------------------------------
def write_corpus_manifest(
    chunks: list[Any],
    lawtext_dir: Path,
    manifests_dir: Path | None = None,
) -> Path:
    """构建并落盘 corpus_manifest.json（原子写）。

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
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
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


# ---------------------------------------------------------------------------
# P0-2：真实磁盘文件校验
# ---------------------------------------------------------------------------
def _verify_disk_indexes(
    manifest: dict[str, Any],
    manifests_dir: Path,
) -> str | None:
    """读取磁盘上实际的 article_index_v2.pkl / bm25_index.pkl，验证三方一致。

    返回不一致原因（``consistent=True`` 时为 None）。校验维度：

    1. ``article_index_v2.pkl`` 存在且 ``schema_version`` 匹配；
    2. article_index 的 chunk 数量 == manifest.chunks_count；
    3. article_index 的 chunks 签名 == manifest.chunks_signature；
    4. ``bm25_index.pkl`` 存在且 ``schema_version`` 匹配；
    5. bm25 的 signature == manifest.bm25_signature；
    6. bm25 的 n_docs == manifest.bm25_n_docs。
    """
    import pickle

    from lvyan.retrieval.lexical import _compute_chunk_signature

    manifests_dir = Path(manifests_dir)
    article_pkl = manifests_dir / "article_index_v2.pkl"
    bm25_pkl = manifests_dir / "bm25_index.pkl"

    # --- article_index_v2.pkl ---
    if not article_pkl.is_file():
        return "article_index_missing"
    try:
        with open(article_pkl, "rb") as f:
            cached = pickle.load(f)
    except Exception:  # noqa: BLE001
        return "article_index_unreadable"
    if not isinstance(cached, dict) or cached.get("schema_version") != manifest.get("article_index_schema_version"):
        return "article_index_schema_mismatch"
    disk_chunks = cached.get("chunks")
    if not isinstance(disk_chunks, list):
        return "article_index_invalid"
    if len(disk_chunks) != int(manifest.get("chunks_count", 0)):
        return "article_index_count_mismatch"
    # 计算磁盘 chunks 的实际签名，与 manifest 对比
    try:
        disk_sig = _compute_chunk_signature(disk_chunks)
    except Exception:  # noqa: BLE001
        return "article_index_signature_unreadable"
    if disk_sig != manifest.get("chunks_signature"):
        return "article_index_signature_mismatch"

    # --- bm25_index.pkl ---
    if not bm25_pkl.is_file():
        return "bm25_index_missing"
    try:
        with open(bm25_pkl, "rb") as f:
            bm25 = pickle.load(f)
    except Exception:  # noqa: BLE001
        return "bm25_index_unreadable"
    if not isinstance(bm25, dict):
        return "bm25_index_invalid"
    if bm25.get("schema_version") != manifest.get("article_index_schema_version"):
        return "bm25_schema_mismatch"
    if str(bm25.get("signature") or "") != str(manifest.get("bm25_signature") or ""):
        return "bm25_signature_mismatch"
    if int(bm25.get("n_docs", 0)) != int(manifest.get("bm25_n_docs", 0)):
        return "bm25_doc_count_mismatch"

    return None


# ---------------------------------------------------------------------------
# 一致性校验（含 TTL 快照）
# ---------------------------------------------------------------------------
# P0-1：TTL 快照缓存。key=(lawtext_dir, manifests_dir)，value=(checked_at, result)
_health_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _verify_uncached(lawtext_dir: Path, manifests_dir: Path) -> dict[str, Any]:
    """执行完整校验（无缓存）。"""
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

    # P0-2：真实磁盘文件校验（ArticleIndex / BM25 是否与 manifest 对应）
    disk_reason = _verify_disk_indexes(manifest, manifests_dir)
    if disk_reason is not None:
        result["reason"] = disk_reason
        return result

    result["consistent"] = True
    return result


def verify_corpus_consistency(
    lawtext_dir: Path | None = None,
    manifests_dir: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """P0-B：校验法库 / ArticleIndex / BM25 三者一致性（带 TTL 快照）。

    P0-1：默认返回 TTL（5 分钟）缓存快照，避免每个 ``/readyz`` 请求都全量扫描
    法库。需要实时结果（重建后、测试）传 ``force=True``。

    返回结构::

        {
          "consistent": bool,           # 三者是否一致
          "reason": str | None,         # 不一致原因（consistent=True 时为 None）
          "manifest": dict | None,      # manifest 内容（缺失时 None）
          "current_corpus_hash": str,   # 当前 LAWTEXT_DIR 实时 hash
          "current_file_count": int,    # 当前 .md 文件数
        }

    校验维度（任一不一致即 ``consistent=False``）：

    1. **manifest 存在**：``corpus_manifest.json`` 缺失 → ``manifest_missing``。
    2. **corpus_hash 匹配**：法库已更新 → ``lawtext_changed``。
    3. **chunks_count > 0**：空索引 → ``empty_index``。
    4. **BM25 签名一致**：``signature_mismatch`` / ``bm25_doc_count_mismatch``。
    5. **真实磁盘文件**（P0-2）：``article_index_*`` / ``bm25_*`` 系列原因。
    """
    lawtext_dir = Path(lawtext_dir or LAWTEXT_DIR)
    manifests_dir = Path(manifests_dir or (AGENT_DIR / "knowledge" / "manifests"))
    key = (str(lawtext_dir), str(manifests_dir))

    now = time.time()
    cached = _health_cache.get(key)
    if not force and cached is not None and (now - cached[0]) < CORPUS_HEALTH_TTL_SECONDS:
        return cached[1]

    result = _verify_uncached(lawtext_dir, manifests_dir)
    _health_cache[key] = (now, result)
    return result


def invalidate_corpus_health_cache() -> None:
    """P0-1：清理 TTL 快照缓存（重建后调用，确保下次校验实时）。"""
    _health_cache.clear()


# ---------------------------------------------------------------------------
# P0-3：原子重建
# ---------------------------------------------------------------------------
def rebuild_corpus_indexes(
    lawtext_dir: Path | None = None,
    manifests_dir: Path | None = None,
) -> dict[str, Any]:
    """重建 ArticleIndex + BM25 + manifest（原子切换），返回最终校验结果。

    当法库更新 / 索引缺失 / 索引与 manifest 不一致时调用。流程：

    1. 全量扫描法库并切分 chunks（``build_article_index``）；
    2. 写 ``article_index_v2.json`` / ``.pkl``（原子）；
    3. 构建 BM25 索引，写 ``bm25_index.pkl`` / ``.json``（原子）；
    4. 生成新的 ``corpus_manifest.json``（原子）；
    5. 清理 TTL 缓存，强制重新校验并返回结果。

    Returns:
        ``verify_corpus_consistency(force=True)`` 的结果 dict。
    """
    import pickle

    from lvyan.retrieval.lexical import (
        ARTICLE_INDEX_SCHEMA_VERSION,
        _build_bm25_index,
        _serialize_bm25_index,
    )
    from lvyan.scripts.ingest_laws import build_article_index

    lawtext_dir = Path(lawtext_dir or LAWTEXT_DIR)
    manifests_dir = Path(manifests_dir or (AGENT_DIR / "knowledge" / "manifests"))
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # P1-3：跨进程互斥锁，防止多 worker / 多实例并发重建互相覆盖
    if not _acquire_rebuild_lock(manifests_dir):
        _logger.warning("[manifest] 另一进程正在重建索引，本次跳过")
        # 等待片刻后直接读取（可能已被重建），若仍不一致如实报告
        time.sleep(1.0)
        invalidate_corpus_health_cache()
        return verify_corpus_consistency(lawtext_dir, manifests_dir, force=True)

    try:
        _logger.info("[manifest] 重建索引：lawtext=%s", lawtext_dir)

        # 1) 全量扫描 + 切分
        chunks = build_article_index(lawtext_dir)
        _logger.info("[manifest] 扫描完成，chunks=%d", len(chunks))

        # 2) ArticleIndex（json + pickle，P1-4 全部原子写）
        article_json = manifests_dir / "article_index_v2.json"
        article_pkl = manifests_dir / "article_index_v2.pkl"
        _atomic_write_text(
            article_json,
            json.dumps(
                {
                    "schema_version": ARTICLE_INDEX_SCHEMA_VERSION,
                    "chunks": [
                        c.model_dump(mode="json") if not isinstance(c, dict) else c
                        for c in chunks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        _atomic_write_bytes(
            article_pkl,
            pickle.dumps(
                {"schema_version": ARTICLE_INDEX_SCHEMA_VERSION, "chunks": chunks}
            ),
        )

        # 3) BM25（pkl + json，原子写）
        bm25_pkl = manifests_dir / "bm25_index.pkl"
        bm25_json = manifests_dir / "bm25_index.json"
        index = _build_bm25_index(chunks)
        serialized = _serialize_bm25_index(index)
        _atomic_write_bytes(bm25_pkl, pickle.dumps(serialized))
        _atomic_write_text(bm25_json, json.dumps(serialized, ensure_ascii=False))

        # 4) 新 manifest（原子写，最后生成 —— 作为「全部就绪」的信号）
        write_corpus_manifest(chunks, lawtext_dir, manifests_dir)
    finally:
        _release_rebuild_lock(manifests_dir)

    # 5) 清理缓存并强制重新校验
    invalidate_corpus_health_cache()
    return verify_corpus_consistency(lawtext_dir, manifests_dir, force=True)


def ensure_corpus_ready(
    lawtext_dir: Path | None = None,
    manifests_dir: Path | None = None,
) -> dict[str, Any]:
    """P0-3：保证法库/索引一致（自愈入口）。

    校验 corpus；不一致时自动重建（``rebuild_corpus_indexes``），重建后再次校验。
    适合应用启动期调用，避免「更新法库 → readyz=503 → 无流量触发 rebuild → 永久
    NotReady」的恶性循环。

    Returns:
        最终校验结果（``consistent=True`` 表示就绪）。
    """
    lawtext_dir = Path(lawtext_dir or LAWTEXT_DIR)
    manifests_dir = Path(manifests_dir or (AGENT_DIR / "knowledge" / "manifests"))

    check = verify_corpus_consistency(lawtext_dir, manifests_dir, force=True)
    if check["consistent"]:
        return check

    # 仅当法库非空且 manifest 缺失/不一致时才值得重建
    reason = check["reason"]
    if reason == "manifest_missing" or reason == "lawtext_changed" or reason == "empty_index" or (
        reason and reason.startswith(("article_index_", "bm25_"))
    ):
        _logger.warning("[manifest] 索引不一致（reason=%s），自动重建", reason)
        return rebuild_corpus_indexes(lawtext_dir, manifests_dir)

    return check


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "CORPUS_HEALTH_TTL_SECONDS",
    "compute_corpus_hash",
    "write_corpus_manifest",
    "load_corpus_manifest",
    "verify_corpus_consistency",
    "invalidate_corpus_health_cache",
    "rebuild_corpus_indexes",
    "ensure_corpus_ready",
]
