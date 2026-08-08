"""P0-B 回归测试：corpus_manifest 法库/索引一致性校验（含 P0-1/P0-2/P0-3 增强）。

验证核心不变量：
1. ``compute_corpus_hash`` 对同一法库内容稳定，对文件变更敏感；
2. ``write_corpus_manifest`` + ``verify_corpus_consistency`` 在一致时返回
   ``consistent=True``；
3. 法库内容变更（模拟 submodule 升级 / 挂载卷覆盖）后 ``corpus_hash`` 不匹配
   → ``consistent=False, reason="lawtext_changed"``；
4. 空索引（构建时 submodule 未检出）→ ``consistent=False, reason="empty_index"``；
5. manifest 内部 BM25 signature 与 chunks signature 不一致 → ``consistent=False``；
6. manifest 缺失 → ``consistent=False, reason="manifest_missing"``；
7. P0-1：``verify_corpus_consistency`` 默认带 TTL 快照缓存，TTL 内不重复全库扫描；
8. P0-2：真实磁盘文件校验 —— article_index.pkl / bm25_index.pkl 与 manifest 不一致
   时返回 ``article_index_*`` / ``bm25_*`` 系列原因；
9. P0-3：``rebuild_corpus_indexes`` 原子重建三件套并重新生成 manifest；
   ``ensure_corpus_ready`` 自愈入口在索引不一致时自动重建。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from lvyan.retrieval.manifest import (
    compute_corpus_hash,
    invalidate_corpus_health_cache,
    load_corpus_manifest,
    rebuild_corpus_indexes,
    verify_corpus_consistency,
    write_corpus_manifest,
)


def _make_chunk(chunk_id: str = "law1#art1", text: str = "第一条 内容") -> Any:
    """构造一个最小的 ArticleChunk（dict 形式，兼容 _compute_chunk_signature）。"""
    from datetime import date

    from lvyan.scripts.ingest_laws import ArticleChunk

    return ArticleChunk(
        chunk_id=chunk_id,
        source_id="law1",
        title="测试法",
        article_number="第一条",
        article_text=text,
        authority_level="法律",
        status="effective",
        effective_date=date(2026, 1, 1),
        content_hash="abc123",
    )


def _make_lawtext_dir(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """在 tmp_path 下创建法库目录，写入 .md 文件。"""
    lawtext = tmp_path / "lawtext"
    lawtext.mkdir()
    files = files or {"law1.md": "# 测试法\n\n第一条 内容\n"}
    for name, content in files.items():
        (lawtext / name).write_text(content, encoding="utf-8")
    return lawtext


def _write_full_index_set(
    chunks: list[Any],
    lawtext_dir: Path,
    manifests_dir: Path,
) -> None:
    """P0-2 辅助：生成完整的索引三件套（article_index + bm25 + manifest）。

    与 ``rebuild_corpus_indexes`` 内部产物一致，供测试验证真实磁盘文件校验。
    先写 manifest 再补 index 文件（模拟预热的产物布局）。
    """
    import pickle

    from lvyan.retrieval.lexical import (
        ARTICLE_INDEX_SCHEMA_VERSION,
        _build_bm25_index,
        _serialize_bm25_index,
    )

    manifests_dir = Path(manifests_dir)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # manifest
    write_corpus_manifest(chunks, lawtext_dir, manifests_dir)

    # article_index_v2.pkl
    with open(manifests_dir / "article_index_v2.pkl", "wb") as f:
        pickle.dump(
            {"schema_version": ARTICLE_INDEX_SCHEMA_VERSION, "chunks": chunks},
            f,
        )

    # bm25_index.pkl
    index = _build_bm25_index(chunks)
    serialized = _serialize_bm25_index(index)
    with open(manifests_dir / "bm25_index.pkl", "wb") as f:
        pickle.dump(serialized, f)


# ---------------------------------------------------------------------------
# 1. compute_corpus_hash 稳定性与敏感性
# ---------------------------------------------------------------------------
def test_corpus_hash_stable_for_same_content(tmp_path):
    """同一法库内容两次计算 hash 应相同。"""
    lawtext = _make_lawtext_dir(tmp_path)
    h1 = compute_corpus_hash(lawtext)
    h2 = compute_corpus_hash(lawtext)
    assert h1 == h2
    assert h1 != "empty"


def test_corpus_hash_changes_on_content_update(tmp_path):
    """法条正文修改后 hash 应变化。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 旧内容\n"})
    h_old = compute_corpus_hash(lawtext)

    (lawtext / "law1.md").write_text("# 测试法\n\n第一条 新内容\n", encoding="utf-8")
    h_new = compute_corpus_hash(lawtext)
    assert h_old != h_new, "法条正文修改后 corpus_hash 必须变化"


def test_corpus_hash_changes_on_file_count(tmp_path):
    """新增/删除法条文件后 hash 应变化。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "内容1"})
    h1 = compute_corpus_hash(lawtext)

    (lawtext / "law2.md").write_text("内容2", encoding="utf-8")
    h2 = compute_corpus_hash(lawtext)
    assert h1 != h2


def test_corpus_hash_empty_dir():
    """空目录返回 'empty'。"""
    h = compute_corpus_hash(Path("/nonexistent/path/xyz"))
    assert h == "empty"


# ---------------------------------------------------------------------------
# 2. write + verify 一致场景（含真实磁盘文件）
# ---------------------------------------------------------------------------
def test_manifest_consistent_when_corpus_matches(tmp_path):
    """完整索引三件套一致时 verify_corpus_consistency 应返回 consistent=True。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"

    _write_full_index_set(chunks, lawtext, manifests_dir)

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is True
    assert result["reason"] is None
    assert result["manifest"] is not None
    assert result["manifest"]["chunks_count"] == 1
    assert result["current_corpus_hash"] == result["manifest"]["corpus_hash"]


# ---------------------------------------------------------------------------
# 3. 法库变更 → lawtext_changed
# ---------------------------------------------------------------------------
def test_manifest_inconsistent_when_lawtext_changed(tmp_path):
    """法库内容变更后 manifest 的 corpus_hash 不匹配 → lawtext_changed。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 旧内容\n"})
    chunks = [_make_chunk(text="第一条 旧内容")]
    manifests_dir = tmp_path / "manifests"

    _write_full_index_set(chunks, lawtext, manifests_dir)

    # 模拟运行时法库已更新（submodule 升级）
    (lawtext / "law1.md").write_text("# 测试法\n\n第一条 新内容\n", encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "lawtext_changed"
    assert result["current_corpus_hash"] != result["manifest"]["corpus_hash"]


# ---------------------------------------------------------------------------
# 4. 空索引 → empty_index
# ---------------------------------------------------------------------------
def test_manifest_inconsistent_when_empty_index(tmp_path):
    """构建时 submodule 为空 → chunks_count=0 → empty_index。"""
    lawtext = _make_lawtext_dir(tmp_path)
    manifests_dir = tmp_path / "manifests"

    # 构建期法库为空，生成空索引 manifest
    write_corpus_manifest([], lawtext, manifests_dir)

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "empty_index"


# ---------------------------------------------------------------------------
# 5. 空索引 + 运行时挂载完整法库 → lawtext_changed（更极端场景）
# ---------------------------------------------------------------------------
def test_empty_index_with_mounted_full_corpus(tmp_path):
    """构建时 submodule 空 → 空索引；运行时挂载完整法库 → lawtext_changed。

    这是审查报告指出的最极端场景：构建时为空，运行时挂载完整法库，
    is_official_db_available=True 但索引为空。
    """
    build_lawtext = _make_lawtext_dir(tmp_path)  # 构建时空目录
    manifests_dir = tmp_path / "manifests"
    write_corpus_manifest([], build_lawtext, manifests_dir)

    # 运行时挂载完整法库
    runtime_lawtext = tmp_path / "runtime_lawtext"
    runtime_lawtext.mkdir()
    (runtime_lawtext / "law1.md").write_text("# 完整法\n\n第一条 内容\n", encoding="utf-8")

    result = verify_corpus_consistency(runtime_lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    # corpus_hash 不匹配（构建时空 vs 运行时有文件）
    assert result["reason"] == "lawtext_changed"


# ---------------------------------------------------------------------------
# 6. manifest 内部字段不一致
# ---------------------------------------------------------------------------
def test_manifest_inconsistent_on_signature_mismatch(tmp_path):
    """BM25 signature 与 chunks signature 不一致 → signature_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    # 篡改 manifest 的 bm25_signature
    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["bm25_signature"] = "tampered00000000"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "signature_mismatch"


def test_manifest_missing(tmp_path):
    """manifest 不存在 → manifest_missing。"""
    lawtext = _make_lawtext_dir(tmp_path)
    result = verify_corpus_consistency(lawtext, tmp_path / "no_manifests", force=True)
    assert result["consistent"] is False
    assert result["reason"] == "manifest_missing"
    assert result["manifest"] is None


def test_manifest_schema_mismatch(tmp_path):
    """manifest_schema_version 不匹配 → manifest_schema_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["manifest_schema_version"] = 999
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "manifest_schema_mismatch"


def test_bm25_doc_count_mismatch(tmp_path):
    """bm25_n_docs != chunks_count → bm25_doc_count_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["bm25_n_docs"] = 999
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "bm25_doc_count_mismatch"


# ---------------------------------------------------------------------------
# 7. P0-2：真实磁盘文件校验
# ---------------------------------------------------------------------------
def test_article_index_missing(tmp_path):
    """manifest 存在但 article_index_v2.pkl 缺失 → article_index_missing。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    # 只写 manifest，不写 index 文件
    write_corpus_manifest(chunks, lawtext, manifests_dir)

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "article_index_missing"


def test_article_index_signature_mismatch(tmp_path):
    """磁盘 article_index.pkl 的 chunks 与 manifest 不匹配 → signature 不一致。"""
    import pickle

    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk(text="第一条 内容")]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    # 用不同内容的 chunks 覆盖 article_index_v2.pkl（法库变了但索引没重建）
    stale_chunks = [_make_chunk(chunk_id="stale#art1", text="旧内容")]
    from lvyan.retrieval.lexical import ARTICLE_INDEX_SCHEMA_VERSION

    with open(manifests_dir / "article_index_v2.pkl", "wb") as f:
        pickle.dump(
            {"schema_version": ARTICLE_INDEX_SCHEMA_VERSION, "chunks": stale_chunks},
            f,
        )

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "article_index_signature_mismatch"


def test_bm25_index_missing(tmp_path):
    """article_index 存在但 bm25_index.pkl 缺失 → bm25_index_missing。"""

    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    (manifests_dir / "bm25_index.pkl").unlink()

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "bm25_index_missing"


def test_bm25_signature_mismatch(tmp_path):
    """磁盘 bm25_index.pkl 的 signature 与 manifest 不匹配 → bm25_signature_mismatch。"""
    import pickle

    from lvyan.retrieval.lexical import _build_bm25_index, _serialize_bm25_index

    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk(text="第一条 内容")]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    # 用不同 chunks 构建 BM25（模拟索引与 manifest 错位）
    stale_chunks = [_make_chunk(chunk_id="stale#art1", text="旧内容")]
    stale_index = _serialize_bm25_index(_build_bm25_index(stale_chunks))
    with open(manifests_dir / "bm25_index.pkl", "wb") as f:
        pickle.dump(stale_index, f)

    result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert result["consistent"] is False
    assert result["reason"] == "bm25_signature_mismatch"


# ---------------------------------------------------------------------------
# 8. P0-1：TTL 快照缓存
# ---------------------------------------------------------------------------
def test_verify_uses_ttl_cache(monkeypatch, tmp_path):
    """verify_corpus_consistency 默认带 TTL 缓存：TTL 内不重复全库扫描。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    # 首次调用 → 缓存 miss → 计算 hash
    r1 = verify_corpus_consistency(lawtext, manifests_dir)
    assert r1["consistent"] is True

    # 法库被修改，但 TTL 未过期 → 缓存快照仍返回旧结果（不重新扫描）
    (lawtext / "law1.md").write_text("# 测试法\n\n第一条 新内容\n", encoding="utf-8")
    r2 = verify_corpus_consistency(lawtext, manifests_dir)
    assert r2["consistent"] is True, "TTL 内应返回缓存快照，不重新扫描法库"

    # force=True → 绕过缓存 → 检测到法库变更
    r3 = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert r3["consistent"] is False
    assert r3["reason"] == "lawtext_changed"


def test_invalidate_cache(tmp_path):
    """invalidate_corpus_health_cache 后下次校验实时。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    _write_full_index_set(chunks, lawtext, manifests_dir)

    verify_corpus_consistency(lawtext, manifests_dir)
    # 法库变更 + 重建索引三件套 + 清缓存
    (lawtext / "law1.md").write_text("# 测试法\n\n第一条 新内容\n", encoding="utf-8")
    _write_full_index_set([_make_chunk(text="第一条 新内容")], lawtext, manifests_dir)
    invalidate_corpus_health_cache()

    result = verify_corpus_consistency(lawtext, manifests_dir)
    assert result["consistent"] is True, "清缓存后应使用新索引实时校验"


# ---------------------------------------------------------------------------
# 9. P0-3：rebuild_corpus_indexes 原子重建
# ---------------------------------------------------------------------------
def test_rebuild_corpus_indexes_heals_mismatch(tmp_path):
    """rebuild 应重建索引三件套并重新生成 manifest，最终 consistent=True。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    manifests_dir = tmp_path / "manifests"

    # 法库存在但无任何索引 → ensure 触发重建
    result = rebuild_corpus_indexes(lawtext, manifests_dir)
    assert result["consistent"] is True, f"重建后应一致，实际 reason={result['reason']}"

    # 三件套都应存在
    assert (manifests_dir / "corpus_manifest.json").is_file()
    assert (manifests_dir / "article_index_v2.pkl").is_file()
    assert (manifests_dir / "bm25_index.pkl").is_file()

    # 新 manifest 与磁盘文件一致
    verify_result = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert verify_result["consistent"] is True


def test_rebuild_after_lawtext_change(tmp_path):
    """法库更新后 rebuild 应重建索引并对齐新法库。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    manifests_dir = tmp_path / "manifests"
    rebuild_corpus_indexes(lawtext, manifests_dir)
    assert verify_corpus_consistency(lawtext, manifests_dir, force=True)["consistent"]

    # 法库更新（新增法条文件）
    (lawtext / "law2.md").write_text("# 测试法2\n\n第一条 新内容\n", encoding="utf-8")

    # 重建前：不一致
    before = verify_corpus_consistency(lawtext, manifests_dir, force=True)
    assert before["consistent"] is False
    assert before["reason"] == "lawtext_changed"

    # 重建后：一致
    result = rebuild_corpus_indexes(lawtext, manifests_dir)
    assert result["consistent"] is True
    assert verify_corpus_consistency(lawtext, manifests_dir, force=True)["consistent"] is True


def test_load_article_chunks_heals_via_ensure_corpus_ready(tmp_path, monkeypatch):
    """_load_article_chunks 在 manifest 不一致时应通过 ensure_corpus_ready 自愈。

    模拟：法库存在但索引缺失（manifest 缺失）。_load_article_chunks 应触发
    自动重建，返回真实构建的 chunks，而非空缓存。
    """
    import lvyan.config as cfg_mod
    from lvyan.retrieval import lexical

    # 构造临时法库，patch config 让 manifest 校验/重建都指向隔离目录。
    # manifests_dir 由 AGENT_DIR / "knowledge" / "manifests" 推导。
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    manifests_dir = tmp_path / "knowledge" / "manifests"
    monkeypatch.setattr(cfg_mod, "LAWTEXT_DIR", lawtext)
    monkeypatch.setattr(cfg_mod, "AGENT_DIR", tmp_path)

    # rebuild 内部依赖 lexical._ARTICLE_INDEX_* 常量（在 import 时基于 AGENT_DIR 固化），
    # 需同步 patch 到隔离路径
    monkeypatch.setattr(lexical, "_ARTICLE_INDEX_FILE", manifests_dir / "article_index_v2.json")
    monkeypatch.setattr(lexical, "_ARTICLE_INDEX_PKL", manifests_dir / "article_index_v2.pkl")
    monkeypatch.setattr(lexical, "_BM25_INDEX_FILE", manifests_dir / "bm25_index.json")
    monkeypatch.setattr(lexical, "_BM25_INDEX_PKL", manifests_dir / "bm25_index.pkl")
    monkeypatch.setattr(lexical, "_GLOBAL_CHUNKS_CACHE", None)
    # 确保 manifest 模块的路径常量也指向隔离目录
    import lvyan.retrieval.manifest as manifest_mod

    monkeypatch.setattr(manifest_mod, "LAWTEXT_DIR", lawtext)
    monkeypatch.setattr(manifest_mod, "AGENT_DIR", tmp_path)

    try:
        result = lexical._load_article_chunks()
        assert isinstance(result, list)
        # 自愈后应成功加载真实 chunks（非空，因为法库有内容）
        assert len(result) >= 1
        # 隔离目录应已生成三件套
        assert (manifests_dir / "corpus_manifest.json").is_file()
        assert (manifests_dir / "article_index_v2.pkl").is_file()
        assert (manifests_dir / "bm25_index.pkl").is_file()
    finally:
        invalidate_corpus_health_cache()
        # 恢复 lexical 的路径常量（避免影响后续测试）
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# 10. load_corpus_manifest 缺失/损坏
# ---------------------------------------------------------------------------
def test_load_manifest_returns_none_when_missing(tmp_path):
    assert load_corpus_manifest(tmp_path / "nonexistent") is None


def test_load_manifest_returns_none_when_corrupt(tmp_path):
    manifest_path = tmp_path / "corpus_manifest.json"
    manifest_path.write_text("{invalid json", encoding="utf-8")
    assert load_corpus_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# 11. P1-3：rebuild 互斥锁（owner 校验 + 有界等待）
# ---------------------------------------------------------------------------
def test_rebuild_lock_roundtrip(tmp_path):
    """获取锁 → 锁文件写入 owner 令牌 → 释放只删自己的锁。"""
    from lvyan.retrieval.manifest import _acquire_rebuild_lock, _release_rebuild_lock

    manifests_dir = tmp_path / "manifests"
    owner = _acquire_rebuild_lock(manifests_dir)
    assert owner is not None, "空目录应能获取锁"
    lock_path = manifests_dir / ".rebuild.lock"
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="utf-8") == owner

    # 同一目录再次获取应失败（互斥）
    assert _acquire_rebuild_lock(manifests_dir) is None

    _release_rebuild_lock(manifests_dir, owner)
    assert not lock_path.exists()


def test_release_does_not_delete_foreign_lock(tmp_path):
    """释放时若锁已被其他进程接管（内容不同），绝不删除他人锁。"""
    from lvyan.retrieval.manifest import _acquire_rebuild_lock, _release_rebuild_lock

    manifests_dir = tmp_path / "manifests"
    owner_a = _acquire_rebuild_lock(manifests_dir)
    lock_path = manifests_dir / ".rebuild.lock"

    # 模拟进程 B 超时接管：删除 A 的锁并写入自己的 owner
    lock_path.unlink()
    owner_b = _acquire_rebuild_lock(manifests_dir)
    assert owner_b is not None and owner_b != owner_a

    # 进程 A 结束释放 → 不得删除 B 的锁
    _release_rebuild_lock(manifests_dir, owner_a)
    assert lock_path.exists(), "释放他人锁的竞态：A 不应删除 B 的锁"
    assert lock_path.read_text(encoding="utf-8") == owner_b

    _release_rebuild_lock(manifests_dir, owner_b)
    assert not lock_path.exists()


def test_rebuild_lock_steals_stale_lock(tmp_path):
    """超过 stale 阈值的锁会被接管（崩溃残留自愈）。"""
    import os
    import time as _time

    from lvyan.retrieval.manifest import (
        _REBUILD_LOCK_STALE_SECONDS,
        _acquire_rebuild_lock,
    )

    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    lock_path = manifests_dir / ".rebuild.lock"
    lock_path.write_text("999:deadbeef", encoding="utf-8")
    # 伪造过期 mtime（超过 stale 阈值）
    old = _time.time() - _REBUILD_LOCK_STALE_SECONDS - 60
    os.utime(lock_path, (old, old))

    owner = _acquire_rebuild_lock(manifests_dir)
    assert owner is not None, "过期残留锁应被接管"
    assert lock_path.read_text(encoding="utf-8") == owner


def test_wait_for_rebuild_lock_returns_when_lock_cleared(tmp_path):
    """锁在超时前消失 → _wait_for_rebuild_lock 尽快返回。"""
    import threading
    import time

    from lvyan.retrieval.manifest import _acquire_rebuild_lock, _wait_for_rebuild_lock

    manifests_dir = tmp_path / "manifests"
    owner = _acquire_rebuild_lock(manifests_dir)
    assert owner is not None

    def clear_later():
        time.sleep(0.3)
        lock_path = manifests_dir / ".rebuild.lock"
        if lock_path.read_text(encoding="utf-8") == owner:
            lock_path.unlink()

    t = threading.Thread(target=clear_later, daemon=True)
    t.start()
    _wait_for_rebuild_lock(manifests_dir, timeout=5.0)
    t.join(timeout=2.0)
    assert not (manifests_dir / ".rebuild.lock").exists()


def test_rebuild_waits_and_verifies_when_locked(tmp_path, monkeypatch):
    """另一进程持锁时 rebuild 应等待并返回现有索引的 verify 结果，而非强制重建。"""
    import lvyan.retrieval.manifest as manifest_mod

    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    manifests_dir = tmp_path / "manifests"
    # 先建好一致的三件套
    rebuild_corpus_indexes(lawtext, manifests_dir)

    acquired: list[bool] = []
    waited: list[bool] = []

    def fake_acquire(_dir):
        acquired.append(True)
        return None  # 模拟另一进程持锁

    def fake_wait(_dir, timeout=0):
        waited.append(True)

    monkeypatch.setattr(manifest_mod, "_acquire_rebuild_lock", fake_acquire)
    monkeypatch.setattr(manifest_mod, "_wait_for_rebuild_lock", fake_wait)

    result = rebuild_corpus_indexes(lawtext, manifests_dir)
    assert acquired, "应尝试获取锁"
    assert waited, "未抢到锁时应等待其他进程重建完成"
    assert result["consistent"] is True
