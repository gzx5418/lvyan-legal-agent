"""P0-B 回归测试：corpus_manifest 法库/索引一致性校验。

验证核心不变量：
1. ``compute_corpus_hash`` 对同一法库内容稳定，对文件变更敏感；
2. ``write_corpus_manifest`` + ``verify_corpus_consistency`` 在一致时返回
   ``consistent=True``；
3. 法库内容变更（模拟 submodule 升级 / 挂载卷覆盖）后 ``corpus_hash`` 不匹配
   → ``consistent=False, reason="lawtext_changed"``；
4. 空索引（构建时 submodule 未检出）→ ``consistent=False, reason="empty_index"``；
5. BM25 signature 与 chunks signature 不一致 → ``consistent=False``；
6. manifest 缺失 → ``consistent=False, reason="manifest_missing"``；
7. ``_load_article_chunks`` 在 manifest 不一致时跳过缓存、现场重建。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lvyan.retrieval.manifest import (
    MANIFEST_SCHEMA_VERSION,
    compute_corpus_hash,
    load_corpus_manifest,
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
# 2. write + verify 一致场景
# ---------------------------------------------------------------------------
def test_manifest_consistent_when_corpus_matches(tmp_path):
    """法库未变更时 verify_corpus_consistency 应返回 consistent=True。"""
    lawtext = _make_lawtext_dir(tmp_path, {"law1.md": "# 测试法\n\n第一条 内容\n"})
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"

    write_corpus_manifest(chunks, lawtext, manifests_dir)

    result = verify_corpus_consistency(lawtext, manifests_dir)
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

    write_corpus_manifest(chunks, lawtext, manifests_dir)

    # 模拟运行时法库已更新（submodule 升级）
    (lawtext / "law1.md").write_text("# 测试法\n\n第一条 新内容\n", encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir)
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

    # 运行时法库仍为空（corpus_hash 匹配），但 chunks_count=0
    result = verify_corpus_consistency(lawtext, manifests_dir)
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

    result = verify_corpus_consistency(runtime_lawtext, manifests_dir)
    assert result["consistent"] is False
    # corpus_hash 不匹配（构建时空 vs 运行时有文件）
    assert result["reason"] == "lawtext_changed"


# ---------------------------------------------------------------------------
# 6. signature 不匹配 → signature_mismatch
# ---------------------------------------------------------------------------
def test_manifest_inconsistent_on_signature_mismatch(tmp_path):
    """BM25 signature 与 chunks signature 不一致 → signature_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    write_corpus_manifest(chunks, lawtext, manifests_dir)

    # 篡改 manifest 的 bm25_signature
    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["bm25_signature"] = "tampered00000000"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir)
    assert result["consistent"] is False
    assert result["reason"] == "signature_mismatch"


# ---------------------------------------------------------------------------
# 7. manifest 缺失 → manifest_missing
# ---------------------------------------------------------------------------
def test_manifest_missing(tmp_path):
    """manifest 不存在 → manifest_missing。"""
    lawtext = _make_lawtext_dir(tmp_path)
    result = verify_corpus_consistency(lawtext, tmp_path / "no_manifests")
    assert result["consistent"] is False
    assert result["reason"] == "manifest_missing"
    assert result["manifest"] is None


# ---------------------------------------------------------------------------
# 8. manifest schema 版本不匹配
# ---------------------------------------------------------------------------
def test_manifest_schema_mismatch(tmp_path):
    """manifest_schema_version 不匹配 → manifest_schema_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    write_corpus_manifest(chunks, lawtext, manifests_dir)

    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["manifest_schema_version"] = 999
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir)
    assert result["consistent"] is False
    assert result["reason"] == "manifest_schema_mismatch"


# ---------------------------------------------------------------------------
# 9. bm25_n_docs 与 chunks_count 不匹配
# ---------------------------------------------------------------------------
def test_bm25_doc_count_mismatch(tmp_path):
    """bm25_n_docs != chunks_count → bm25_doc_count_mismatch。"""
    lawtext = _make_lawtext_dir(tmp_path)
    chunks = [_make_chunk()]
    manifests_dir = tmp_path / "manifests"
    write_corpus_manifest(chunks, lawtext, manifests_dir)

    manifest_path = manifests_dir / "corpus_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["bm25_n_docs"] = 999
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    result = verify_corpus_consistency(lawtext, manifests_dir)
    assert result["consistent"] is False
    assert result["reason"] == "bm25_doc_count_mismatch"


# ---------------------------------------------------------------------------
# 10. _load_article_chunks 在 manifest 不一致时跳过缓存
# ---------------------------------------------------------------------------
def test_load_article_chunks_skips_cache_on_mismatch(tmp_path, monkeypatch):
    """manifest 不一致时 _load_article_chunks 应跳过 pickle/JSON 缓存、现场重建。

    模拟：pickle 缓存存在但 manifest 缺失（或法库已变更）。
    _load_article_chunks 应忽略缓存，调用 build_article_index 重建。
    """
    from lvyan.retrieval import lexical

    # 构造一个陈旧的 pickle 缓存
    manifests_dir = lexical.AGENT_DIR / "knowledge" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    # 写入一个 pickle 缓存（schema_version 匹配，含旧 chunks）
    import pickle

    stale_chunks = [_make_chunk(chunk_id="stale#art1", text="旧内容")]
    with open(lexical._ARTICLE_INDEX_PKL, "wb") as f:
        pickle.dump(
            {"schema_version": lexical.ARTICLE_INDEX_SCHEMA_VERSION, "chunks": stale_chunks},
            f,
        )

    # 确保没有 manifest（或 manifest 不一致）→ cache_trusted=False
    manifest_path = manifests_dir / "corpus_manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()

    # mock build_article_index 返回新 chunks
    rebuilt_chunks = [_make_chunk(chunk_id="fresh#art1", text="新内容")]
    call_count = {"build": 0}

    def _fake_build():
        call_count["build"] += 1
        return rebuilt_chunks

    # 重置全局缓存
    monkeypatch.setattr(lexical, "_GLOBAL_CHUNKS_CACHE", None)
    # mock build_article_index（在函数内部 lazy import）
    import lvyan.scripts.ingest_laws as ingest_mod

    monkeypatch.setattr(ingest_mod, "build_article_index", _fake_build)
    # mock save_index_json 避免写文件
    monkeypatch.setattr(ingest_mod, "save_index_json", lambda *a, **kw: None)

    try:
        result = lexical._load_article_chunks()
    finally:
        # 清理测试产生的 pickle
        if lexical._ARTICLE_INDEX_PKL.exists():
            lexical._ARTICLE_INDEX_PKL.unlink()

    assert call_count["build"] == 1, "manifest 不一致时应调用 build_article_index 重建"
    assert result is rebuilt_chunks, "应返回重建的 chunks 而非陈旧缓存"


# ---------------------------------------------------------------------------
# 11. load_corpus_manifest 缺失/损坏
# ---------------------------------------------------------------------------
def test_load_manifest_returns_none_when_missing(tmp_path):
    assert load_corpus_manifest(tmp_path / "nonexistent") is None


def test_load_manifest_returns_none_when_corrupt(tmp_path):
    manifest_path = tmp_path / "corpus_manifest.json"
    manifest_path.write_text("{invalid json", encoding="utf-8")
    assert load_corpus_manifest(tmp_path) is None
