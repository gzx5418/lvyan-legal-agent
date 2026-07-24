"""法规版本解析器单元测试。

覆盖 Task 6：
  1. YAML front matter 元数据解析（真实文件）
  2. 版本聚合——不动产登记暂行条例（多版本关系链）
  3. status 中文 -> 枚举映射
  4. 全库扫描冒烟测试（>2000 条，无异常）
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lvyan.config import LAWTEXT_DIR
from lvyan.retrieval.version_resolver import (
    LawMetadata,
    VersionGroup,
    build_version_groups,
    compute_content_hash,
    find_law_files_by_title,
    mark_current_effective,
    parse_law_metadata,
    scan_all_laws,
    status_distribution,
)


# ---------------------------------------------------------------------------
# 1. 元数据解析：真实文件
# ---------------------------------------------------------------------------
def test_parse_law_metadata_real_file():
    """解析 1978 年退休退职办法决议，验证关键字段。"""
    md_path = LAWTEXT_DIR / "法律" / "2c909fdd678bf17901678bf5a6740055.md"
    if not md_path.exists():
        pytest.skip(f"官方法律库不存在该文件：{md_path}")

    meta = parse_law_metadata(md_path)

    assert meta.source_id == "2c909fdd678bf17901678bf5a6740055"
    assert meta.title == (
        "全国人民代表大会常务委员会关于批准《国务院关于工人退休、退职的暂行办法》的决议"
    )
    assert meta.publication_date == date(1978, 5, 24)
    assert meta.effective_date == date(1978, 5, 24)
    assert meta.status == "effective"
    assert any("flk.npc.gov.cn" in url for url in meta.official_urls)
    assert meta.content_hash
    assert len(meta.content_hash) == 16  # sha256 前 16 位 hex
    assert meta.author == "全国人民代表大会常务委员会"
    assert meta.group == "法律"
    assert meta.raw_filepath.endswith("2c909fdd678bf17901678bf5a6740055.md")


# ---------------------------------------------------------------------------
# 2. 版本聚合：不动产登记暂行条例（多版本）
# ---------------------------------------------------------------------------
def test_version_aggregation_budengchan_dengji():
    """不动产登记暂行条例至少 3 个版本，聚合为 1 组并标记 current_effective。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    files = find_law_files_by_title("不动产登记暂行条例", LAWTEXT_DIR)
    assert len(files) >= 2, "不动产登记暂行条例应至少有 2 个版本文件"

    metas = [parse_law_metadata(f) for f in files]
    groups = build_version_groups(metas)

    # metas 仅含单一 title，聚合为 1 组
    assert len(groups) == 1
    group: VersionGroup = groups[0]

    assert group.title == "不动产登记暂行条例"
    assert len(group.versions) >= 2
    assert group.current_effective is not None
    assert group.current_effective.status == "effective"

    # 关系链：按时间顺序，长度 == 版本数
    assert len(group.replaces_chain) == len(group.versions)
    assert group.replaces_chain == [v.source_id for v in group.versions]

    # superseded 标记：除 current_effective 外的有效版本应被标记为取代
    assert group.current_effective.superseded is False
    superseded_count = sum(1 for v in group.versions if v.superseded)
    assert superseded_count >= 1, "应至少有一个被取代的有效版本"

    # 版本按 effective_date 升序（None 排最后）
    dates = [v.effective_date for v in group.versions]
    non_none = [d for d in dates if d is not None]
    assert non_none == sorted(non_none)
    if any(d is None for d in dates):
        assert dates[-1] is None  # None 排末尾


# ---------------------------------------------------------------------------
# 3. status 中文 -> 枚举映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("有效", "effective"),
        ("已废止", "repealed"),
        ("失效", "repealed"),
        ("尚未生效", "not_yet_effective"),
        ("未生效", "not_yet_effective"),
        ("已修改", "unknown"),  # 不在映射表内 -> unknown
    ],
)
def test_status_mapping(tmp_path: Path, raw_status: str, expected: str):
    """构造含指定 status 的 front matter，验证映射结果。"""
    md = tmp_path / f"law_{expected}_{abs(hash(raw_status))}.md"
    md.write_text(
        f"---\n"
        f"id: test-{expected}\n"
        f"title: 测试法规\n"
        f"status: {raw_status}\n"
        f"---\n\n"
        f"正文内容\n",
        encoding="utf-8",
    )
    meta = parse_law_metadata(md)
    assert meta.status == expected, f"status '{raw_status}' 应映射为 '{expected}'，实际 '{meta.status}'"


def test_status_mapping_empty_and_missing(tmp_path: Path):
    """status 缺失或空值均映射为 unknown。"""
    # status 字段缺失
    md1 = tmp_path / "no_status.md"
    md1.write_text("---\nid: a\ntitle: 法规A\n---\n\n正文\n", encoding="utf-8")
    assert parse_law_metadata(md1).status == "unknown"

    # status 为空值（YAML 解析为 None）
    md2 = tmp_path / "empty_status.md"
    md2.write_text("---\nid: b\ntitle: 法规B\nstatus:\n---\n\n正文\n", encoding="utf-8")
    assert parse_law_metadata(md2).status == "unknown"


def test_no_front_matter_is_robust(tmp_path: Path):
    """无 front matter 的文件不抛异常，返回 unknown 默认元数据。"""
    md = tmp_path / "plain.md"
    md.write_text("这是一份没有 front matter 的纯正文文件。\n", encoding="utf-8")
    meta = parse_law_metadata(md)
    assert meta.status == "unknown"
    assert meta.title == "plain"  # 回退到文件名 stem
    assert meta.source_id == "plain"
    assert meta.content_hash  # 对全文计算 hash


def test_compute_content_hash_deterministic():
    """相同正文产生相同 hash，且为 16 位 hex。"""
    text = "中华人民共和国民法典\n第一编 总则\n"
    h1 = compute_content_hash(text)
    h2 = compute_content_hash(text)
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_mark_current_effective_recomputes():
    """mark_current_effective 可对已有 groups 重新标记。"""
    base = LawMetadata(
        source_id="v1",
        title="示例法",
        publication_date=date(2010, 1, 1),
        effective_date=date(2010, 1, 1),
        status="effective",
        raw_filepath="/tmp/v1.md",
        content_hash="a" * 16,
    )
    newer = LawMetadata(
        source_id="v2",
        title="示例法",
        publication_date=date(2020, 1, 1),
        effective_date=date(2020, 1, 1),
        status="effective",
        raw_filepath="/tmp/v2.md",
        content_hash="b" * 16,
    )
    groups = build_version_groups([base, newer])
    assert len(groups) == 1
    group = groups[0]
    assert group.current_effective is not None
    assert group.current_effective.source_id == "v2"
    # 旧版本被标记 superseded
    assert base.superseded is True
    assert newer.superseded is False

    # 重置后用 mark_current_effective 重算
    base.superseded = False
    newer.superseded = False
    group.current_effective = None
    mark_current_effective(groups)
    assert group.current_effective is not None
    assert group.current_effective.source_id == "v2"
    assert base.superseded is True
    assert newer.superseded is False


# ---------------------------------------------------------------------------
# 4. 全库扫描冒烟测试
# ---------------------------------------------------------------------------
def test_smoke_scan_all_laws():
    """扫描 LAWTEXT_DIR 下全部 .md 文件，无异常且条目数 > 2000。"""
    if not LAWTEXT_DIR.is_dir():
        pytest.skip(f"官方法律库目录不存在：{LAWTEXT_DIR}")

    metas = scan_all_laws(LAWTEXT_DIR)
    assert len(metas) > 2000, f"全库条目数应 > 2000，实际 {len(metas)}"

    # 每条元数据基本字段非空
    for m in metas:
        assert m.title
        assert m.source_id
        assert m.content_hash
        assert m.raw_filepath

    # status 分布统计（打印但不强制断言具体数字）
    dist = status_distribution(metas)
    print(f"\n[smoke] 全库扫描共 {len(metas)} 条；status 分布：{dist}")
    assert sum(dist.values()) == len(metas)
