"""法规条文级切分与索引 v2 单元测试。

覆盖 Task 7：
  1. 单部法律切分（无条文的决议 -> 整文 1 chunk）
  2. 民法典大法律切分（1260 条，chapter 非空）
  3. 全库切分冒烟测试（chunk 总数远大于法规数）
  4. save_index_json 序列化与读回
  5. OpenSearch / PostgreSQL 桩优雅降级
  6. issue #11：含 date 的 chunk 可被 msgpack 序列化（LVIX 安全索引）
  7. issue #12：--limit 测试模式禁止覆盖生产索引；重复 chunk_id 告警
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import msgpack
import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.safe_index import SafeIndexStore
from lvyan.retrieval.version_resolver import find_law_files_by_title, parse_law_metadata
from lvyan.scripts import ingest_laws
from lvyan.scripts.ingest_laws import (
    ArticleChunk,
    _parse_chinese_number,
    _warn_duplicate_chunk_ids,
    build_article_index,
    chunk_law_articles,
    save_index_json,
    write_to_opensearch,
    write_to_postgres,
)


# ---------------------------------------------------------------------------
# 1. 单部法律切分（无条文的决议）
# ---------------------------------------------------------------------------
def test_chunk_single_law_decision():
    """退休退职办法决议无「第X条」格式，应整文为 1 个 chunk。"""
    md_path = LAWTEXT_DIR / "法律" / "2c909fdd678bf17901678bf5a6740055.md"
    if not md_path.exists():
        pytest.skip(f"官方法律库不存在该文件：{md_path}")

    meta = parse_law_metadata(md_path)
    chunks = chunk_law_articles(meta)

    assert len(chunks) >= 1, "决议至少应产生 1 个 chunk"
    # 无条文格式：整文为 1 个 chunk，article_number 为空
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.article_number == ""
    assert chunk.source_id == meta.source_id
    assert chunk.title == meta.title
    assert chunk.content_hash
    assert len(chunk.content_hash) == 16
    assert chunk.authority_level == "法律"
    assert chunk.status == "effective"
    assert chunk.jurisdiction == "中国大陆"
    assert chunk.official_source is not None
    assert "flk.npc.gov.cn" in chunk.official_source
    assert chunk.effective_date == date(1978, 5, 24)
    assert chunk.publication_date == date(1978, 5, 24)
    # chunk_id 含 source_id
    assert meta.source_id in chunk.chunk_id


# ---------------------------------------------------------------------------
# 2. 民法典大法律切分
# ---------------------------------------------------------------------------
def test_chunk_large_law_civil_code():
    """民法典有 1260 条，切分后 chunk 数应 > 10，且至少有一个 chapter 非空。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    files = find_law_files_by_title("中华人民共和国民法典", LAWTEXT_DIR)
    assert files, "应能找到民法典文件"

    meta = parse_law_metadata(files[0])
    chunks = chunk_law_articles(meta)

    assert len(chunks) > 10, f"民法典 chunk 数应 > 10，实际 {len(chunks)}"
    # 民法典正文每一条都应标注 chapter
    assert any(c.chapter for c in chunks), "至少应有一个 chunk 的 chapter 非空"
    # 每条 article_number 非空（民法典全部为「第X条」格式）
    assert all(c.article_number for c in chunks)
    # 验证第一条
    first = chunks[0]
    assert first.article_number == "第一条"
    assert first.chapter is not None
    assert first.content_hash
    assert first.source_id == meta.source_id


# ---------------------------------------------------------------------------
# 3. 全库切分冒烟测试
# ---------------------------------------------------------------------------
def test_smoke_build_article_index():
    """全库切分：chunk 总数应远大于法规数（2445），且无异常抛出。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    chunks = build_article_index()
    assert len(chunks) > 2445, f"全库 chunk 总数应 > 2445（法规数），实际 {len(chunks)}"

    with_article = sum(1 for c in chunks if c.article_number)
    with_chapter = sum(1 for c in chunks if c.chapter)

    print(
        f"\n[smoke] 全库切分统计：总 chunk={len(chunks)}，"
        f"有 article_number={with_article}，有 chapter={with_chapter}"
    )
    # 统计不强制断言具体数字，但应有合理比例
    assert with_article > 0
    assert with_chapter > 0


# ---------------------------------------------------------------------------
# 4. save_index_json
# ---------------------------------------------------------------------------
def test_save_index_json(tmp_path: Path):
    """对前 100 个 chunk 调用 save_index_json，读回验证结构与数量。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    chunks = build_article_index()
    assert len(chunks) >= 100, "全库 chunk 数应 >= 100"
    sample = chunks[:100]

    out = tmp_path / "article_index_v2.json"
    save_index_json(sample, out)

    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 3
    assert len(data["chunks"]) == 100

    # 验证每条记录的结构
    first = data["chunks"][0]
    assert "chunk_id" in first
    assert "source_id" in first
    assert "title" in first
    assert "article_text" in first
    assert "authority_level" in first
    assert "content_hash" in first
    assert "jurisdiction" in first
    assert first["jurisdiction"] == "中国大陆"

    # chunk_id 应在 JSON 中唯一
    chunk_ids = [item["chunk_id"] for item in data["chunks"]]
    assert len(set(chunk_ids)) == len(chunk_ids), "chunk_id 应唯一"


# ---------------------------------------------------------------------------
# 5. OpenSearch / PostgreSQL 桩优雅降级
# ---------------------------------------------------------------------------
def test_stub_degradation():
    """桩函数在服务不可达时应返回 0 且不抛异常。"""
    r1 = write_to_opensearch([])
    assert r1 == 0
    r2 = write_to_postgres([])
    assert r2 == 0

    # 传入非空列表也应优雅降级
    fake = ArticleChunk(
        chunk_id="test#第一条",
        source_id="test",
        title="测试法规",
        article_number="第一条",
        article_text="测试正文",
        authority_level="法律",
        status="effective",
        content_hash="0" * 16,
    )
    assert write_to_opensearch([fake]) == 0
    assert write_to_postgres([fake]) == 0


# ---------------------------------------------------------------------------
# 辅助：中文数字解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("十", 10),
        ("二十三", 23),
        ("一百零五", 105),
        ("一千二百六十", 1260),
        ("一百零一", 101),
        ("五", 5),
        ("九十九", 99),
        ("101", 101),
    ],
)
def test_parse_chinese_number(text: str, expected: int):
    assert _parse_chinese_number(text) == expected


# ---------------------------------------------------------------------------
# 6. issue #11：msgpack 无法序列化 datetime.date
# ---------------------------------------------------------------------------
def _make_chunk_with_dates(chunk_id: str = "test#第一条") -> ArticleChunk:
    """构造含 date 字段的 ArticleChunk（复现 issue #11 的数据形态）。"""
    return ArticleChunk(
        chunk_id=chunk_id,
        source_id="test",
        title="测试法规",
        article_number="第一条",
        article_text="测试正文",
        authority_level="法律",
        status="effective",
        effective_date=date(2021, 1, 1),
        expiry_date=date(2030, 12, 31),
        content_hash="0" * 16,
        publication_date=date(2020, 5, 28),
    )


def test_model_dump_json_is_msgpack_serializable(tmp_path: Path):
    """含 date 的 chunk 经 model_dump(mode="json") 后必须能过 msgpack.packb。

    此前 ``model_dump()`` 保留原生 ``datetime.date``，msgpack 直接抛
    ``TypeError``（issue #11）。rebuild_indexes / lexical 的 LVIX 写缓存
    均依赖该路径。
    """
    chunk = _make_chunk_with_dates()
    data = {"schema_version": 3, "chunks": [chunk.model_dump(mode="json")]}

    payload = msgpack.packb(data, use_bin_type=True)

    restored = msgpack.unpackb(payload, raw=False)
    assert restored["chunks"][0]["effective_date"] == "2021-01-01"
    assert restored["chunks"][0]["publication_date"] == "2020-05-28"


def test_safe_index_store_tolerates_raw_date_objects(tmp_path: Path):
    """纵深防御：SafeIndexStore.save 遇到原生 date/datetime 也不应崩溃。

    调用方即使漏用 ``mode="json"``（旧式 ``model_dump()`` 保留 date 对象），
    ``_msgpack_default`` 回调也应将其 isoformat 字符串化。
    """
    chunk = _make_chunk_with_dates()
    lvix = tmp_path / "article_index_v3.lvix"

    SafeIndexStore.save(
        lvix,
        {"schema_version": 3, "chunks": [chunk.model_dump()]},  # 故意用旧式 dump
        corpus_hash="a" * 16,
        schema_version=3,
    )

    result = SafeIndexStore.load(lvix, expected_schema_version=3, expected_corpus_hash="a" * 16)
    assert result is not None
    assert result["data"]["chunks"][0]["effective_date"] == "2021-01-01"


# ---------------------------------------------------------------------------
# 7. issue #12：--limit 测试模式 / 重复 chunk_id 可见性
# ---------------------------------------------------------------------------
def test_main_limit_without_output_refused(monkeypatch, tmp_path: Path):
    """--limit 未显式指定 --output 时必须拒绝执行，不得写默认生产索引。"""
    prod = ingest_laws._DEFAULT_OUTPUT_PATH
    before = prod.read_bytes() if prod.exists() else None
    monkeypatch.setattr(sys, "argv", ["ingest_laws", "--limit", "5"])

    with pytest.raises(SystemExit) as excinfo:
        ingest_laws.main()

    assert excinfo.value.code == 2
    # 默认生产索引不被创建/改动
    after = prod.read_bytes() if prod.exists() else None
    assert after == before


def test_main_limit_with_output_refusal_message(monkeypatch, capsys):
    """拒绝时的报错信息应包含指引文案。"""
    monkeypatch.setattr(sys, "argv", ["ingest_laws", "--limit", "1"])

    with pytest.raises(SystemExit):
        ingest_laws.main()

    stderr = capsys.readouterr().err
    assert "--limit 测试模式必须用 --output 指定非生产路径" in stderr


def test_main_limit_with_explicit_output_succeeds(monkeypatch, tmp_path: Path):
    """--limit + 显式 --output 时正常执行并只写指定路径。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    out = tmp_path / "smoke_index.json"
    monkeypatch.setattr(sys, "argv", ["ingest_laws", "--limit", "1", "--output", str(out)])

    ingest_laws.main()

    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 3
    assert isinstance(data["chunks"], list)
    assert len(data["chunks"]) >= 1


def test_warn_duplicate_chunk_ids(caplog):
    """重复 chunk_id 入库前应触发 warning（不改写入语义，仅可见）。"""
    dup_a = _make_chunk_with_dates("dup#第一条")
    dup_b = _make_chunk_with_dates("dup#第一条")
    unique = _make_chunk_with_dates("ok#第一条")

    with caplog.at_level("WARNING", logger="lvyan.scripts.ingest_laws"):
        _warn_duplicate_chunk_ids([dup_a, dup_b, unique])

    assert "1 个重复 chunk_id" in caplog.text
    assert "dup#第一条" in caplog.text

    # 无重复时不告警
    caplog.clear()
    with caplog.at_level("WARNING", logger="lvyan.scripts.ingest_laws"):
        _warn_duplicate_chunk_ids([unique])
    assert "重复 chunk_id" not in caplog.text
