"""法规条文级切分与索引 v2 单元测试。

覆盖 Task 7：
  1. 单部法律切分（无条文的决议 -> 整文 1 chunk）
  2. 民法典大法律切分（1260 条，chapter 非空）
  3. 全库切分冒烟测试（chunk 总数远大于法规数）
  4. save_index_json 序列化与读回
  5. OpenSearch / PostgreSQL 桩优雅降级
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.version_resolver import find_law_files_by_title, parse_law_metadata
from lvyan.scripts.ingest_laws import (
    ArticleChunk,
    _parse_chinese_number,
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
