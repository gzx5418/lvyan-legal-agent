"""rebuild_indexes / sync_sources 脚本健壮性测试（issue #16 第 4 项）。

覆盖：
  1. JSON 缓存损坏时降级走「从法规源现场构建」，而非裸崩
  2. main() 中 article 重建失败不阻断 bm25 重建（独立 try）
  3. main() 接受外部传入的参数对象（sync_sources 复用自身 args）
  4. sync_sources --rebuild 把 --lawtext-dir 传递给重建入口（不再 sys.argv hack）
  5. lexical._load_article_chunks 支持显式 directory 参数（不再固定 AGENT_DIR）
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from lvyan.scripts import rebuild_indexes, sync_sources
from lvyan.scripts.ingest_laws import ArticleChunk, save_index_json


def _fake_chunks() -> list[ArticleChunk]:
    """构造含 date 字段的 chunks（顺带覆盖 issue #11 的 msgpack 序列化路径）。"""
    return [
        ArticleChunk(
            chunk_id="law1#第一条",
            source_id="law1",
            title="测试法",
            article_number="第一条",
            article_text="第一条 测试内容。",
            authority_level="法律",
            status="effective",
            effective_date=date(2021, 1, 1),
            content_hash="a" * 16,
        ),
        ArticleChunk(
            chunk_id="law1#第二条",
            source_id="law1",
            title="测试法",
            article_number="第二条",
            article_text="第二条 更多内容。",
            authority_level="法律",
            status="effective",
            effective_date=date(2021, 1, 1),
            content_hash="b" * 16,
        ),
    ]


# ---------------------------------------------------------------------------
# 1. JSON 缓存损坏 → 降级现场构建
# ---------------------------------------------------------------------------
def test_rebuild_article_index_corrupt_json_falls_back(tmp_path, monkeypatch, caplog):
    """article_index JSON 损坏时应告警并走现场构建，最终仍写出有效 LVIX。"""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    # 写入损坏的 JSON 缓存
    (manifests_dir / "article_index_v2.json").write_text("{not valid json", encoding="utf-8")

    # 模拟「从法规源现场构建」：替换 ingest_laws.build_article_index
    import lvyan.scripts.ingest_laws as ingest_mod

    monkeypatch.setattr(ingest_mod, "build_article_index", lambda lawtext_dir=None: _fake_chunks())

    with caplog.at_level(logging.WARNING, logger=rebuild_indexes.__name__):
        ok = rebuild_indexes.rebuild_article_index(manifests_dir, force=True)

    assert ok is True
    assert "JSON 缓存读取失败" in caplog.text

    # LVIX 写入成功且可加载（chunks 含 date，同时验证 issue #11 修复）
    from lvyan.retrieval.safe_index import SafeIndexStore

    lvix = manifests_dir / "article_index_v3.lvix"
    assert lvix.is_file()
    result = SafeIndexStore.load(lvix, expected_schema_version=3)
    assert result is not None
    assert len(result["data"]["chunks"]) == 2
    # 损坏的 JSON 应已被现场构建结果覆盖为合法内容
    data = json.loads((manifests_dir / "article_index_v2.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 3


def test_rebuild_bm25_index_corrupt_json_falls_back(tmp_path, monkeypatch, caplog):
    """bm25 JSON 损坏时应告警并降级从 article chunks 构建。"""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "bm25_index.json").write_text("", encoding="utf-8")  # 空 JSON → 解析失败

    # 模拟显式目录下的 chunks 加载
    from lvyan.retrieval import lexical

    monkeypatch.setattr(lexical, "_load_article_chunks", lambda *a, **kw: _fake_chunks())

    with caplog.at_level(logging.WARNING, logger=rebuild_indexes.__name__):
        ok = rebuild_indexes.rebuild_bm25_index(manifests_dir, force=True)

    assert ok is True
    assert "JSON 缓存读取失败" in caplog.text
    assert (manifests_dir / "bm25_index_v3.lvix").is_file()

    data = json.loads((manifests_dir / "bm25_index.json").read_text(encoding="utf-8"))
    assert data["n_docs"] == 2


# ---------------------------------------------------------------------------
# 2. article 重建失败不阻断 bm25 重建
# ---------------------------------------------------------------------------
def test_main_article_failure_does_not_block_bm25(tmp_path, monkeypatch):
    """rebuild_article_index 抛异常时，rebuild_bm25_index 仍应被执行。"""
    called: dict[str, object] = {}

    def _boom(*a, **kw):
        called["article"] = True
        raise RuntimeError("article 炸了")

    def _ok(*a, **kw):
        called["bm25"] = True
        return True

    monkeypatch.setattr(rebuild_indexes, "rebuild_article_index", _boom)
    monkeypatch.setattr(rebuild_indexes, "rebuild_bm25_index", _ok)

    rc = rebuild_indexes.main(argparse.Namespace(manifests_dir=tmp_path, force=True))

    assert called == {"article": True, "bm25": True}
    assert rc == 1  # article 失败 → 非零退出，但 bm25 已执行


# ---------------------------------------------------------------------------
# 3. main 接受外部参数对象（缺字段回退默认）
# ---------------------------------------------------------------------------
def test_main_accepts_external_namespace(monkeypatch):
    """外部 Namespace 缺 --manifests-dir/--force 时应回退默认值并透传 lawtext_dir。"""
    from lvyan.config import AGENT_DIR

    recorded: list[tuple] = []

    def _record_article(manifests_dir, *, force=False, lawtext_dir=None):
        recorded.append(("article", manifests_dir, force, lawtext_dir))
        return True

    def _record_bm25(manifests_dir, *, force=False, lawtext_dir=None):
        recorded.append(("bm25", manifests_dir, force, lawtext_dir))
        return True

    monkeypatch.setattr(rebuild_indexes, "rebuild_article_index", _record_article)
    monkeypatch.setattr(rebuild_indexes, "rebuild_bm25_index", _record_bm25)

    custom_lawtext = Path("/custom/lawtext")
    rc = rebuild_indexes.main(argparse.Namespace(lawtext_dir=custom_lawtext))

    assert rc == 0
    assert len(recorded) == 2
    for name, manifests_dir, force, lawtext_dir in recorded:
        assert manifests_dir == AGENT_DIR / "knowledge" / "manifests"
        assert force is False  # sync 之外的调用方未显式 force → 保持默认
        assert lawtext_dir == custom_lawtext


# ---------------------------------------------------------------------------
# 4. sync_sources --rebuild 传递自身参数
# ---------------------------------------------------------------------------
def test_sync_sources_rebuild_forwards_args(tmp_path, monkeypatch):
    """--rebuild 应把 --lawtext-dir 等参数传给 rebuild_main，而非 sys.argv hack 静默丢弃。"""
    import lvyan.scripts.rebuild_indexes as rebuild_mod

    captured: list[object] = []

    def _fake_rebuild_main(args=None):
        captured.append(args)
        return 0

    monkeypatch.setattr(rebuild_mod, "main", _fake_rebuild_main)

    lawtext_dir = tmp_path / "lawtext"
    lawtext_dir.mkdir()
    report = tmp_path / "version_quality.json"

    rc = sync_sources.main(
        [
            "--lawtext-dir",
            str(lawtext_dir),
            "--report",
            str(report),
            "--rebuild",
        ]
    )

    assert rc == 0
    assert report.is_file()
    assert len(captured) == 1
    forwarded = captured[0]
    assert isinstance(forwarded, argparse.Namespace)
    assert forwarded.lawtext_dir == lawtext_dir
    assert forwarded.force is True  # 保持 --rebuild 原有强制重建语义
    assert forwarded.manifests_dir is None  # 未显式指定 → rebuild 内部回退默认


# ---------------------------------------------------------------------------
# 5. _load_article_chunks 显式 directory 参数
# ---------------------------------------------------------------------------
def test_load_article_chunks_with_explicit_directory(tmp_path, monkeypatch):
    """传入 directory 时应读写该目录下的缓存，而非固定 AGENT_DIR 默认路径。"""
    from lvyan.retrieval import lexical
    import lvyan.retrieval.manifest as manifest_mod

    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    # 预置合法 JSON 缓存
    save_index_json(_fake_chunks(), manifests_dir / "article_index_v2.json")

    # 隔离：manifest 校验桩（缓存可信）+ 清空模块级缓存
    monkeypatch.setattr(
        manifest_mod,
        "ensure_corpus_ready",
        lambda *a, **kw: {"consistent": True, "reason": "", "manifest": {}},
    )
    monkeypatch.setattr(lexical, "_GLOBAL_CHUNKS_CACHE", None)

    chunks = lexical._load_article_chunks(manifests_dir)

    assert len(chunks) == 2
    assert chunks[0].effective_date == date(2021, 1, 1)
    # 命中 JSON 缓存后应把 LVIX 写入同一显式目录
    assert (manifests_dir / "article_index_v3.lvix").is_file()
