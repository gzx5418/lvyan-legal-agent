"""Authority Status Validator 单元测试（SubTask 13.5）。

覆盖场景：
1. 全部有效：所有 statutes 状态 effective，日期合法 → passed=True
2. 已废止法规：status=repealed → error，passed=False
3. 尚未生效：effective_date 在未来 → error
4. 已过期：expiry_date 在过去 → error
5. 被取代的历史版本（未标注）→ warning
6. 被取代的历史版本（已标注「历史适用」）→ 无 warning
7. verify_statute_status 异常时回退到 Authority.status
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from lvyan.retrieval.version_aware import StatuteVerification
from lvyan.schemas import Authority
from lvyan.validators.authority_status import (
    validate_authority_status,
)


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
    article_text: str = "当事人一方不履行合同义务的，应当承担违约责任。",
    effective_date: date | None = date(2021, 1, 1),
    expiry_date: date | None = None,
    status: str = "effective",
    source_id: str | None = None,
    official_source: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level="法律",
        effective_date=effective_date,
        expiry_date=expiry_date,
        status=status,  # type: ignore[arg-type]
        official_source=official_source,
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _mock_verify(status: str = "effective", superseded_by: str | None = None):
    """构造 mock verify_statute_status 函数。"""

    def _mock(source_id: str, as_of: Any = None) -> StatuteVerification:
        return StatuteVerification(
            source_id=source_id,
            title="mock-title",
            current_status=status,  # type: ignore[arg-type]
            superseded_by=superseded_by,
            checked_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
        )

    return _mock


def _mock_raise(*args: Any, **kwargs: Any) -> None:
    """模拟 verify_statute_status 抛异常。"""
    raise RuntimeError("mock error")


# ---------------------------------------------------------------------------
# 1. 全部有效
# ---------------------------------------------------------------------------
def test_validate_authority_status_all_effective(
    monkeypatch: pytest.MonkeyPatch,
):
    """所有 statutes 状态 effective，日期合法 → passed=True。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    statutes = [
        _make_authority(source_id="s1"),
        _make_authority(source_id="s2", article_number="第一条"),
    ]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.total_authorities == 2
    assert report.effective_count == 2
    assert report.passed is True
    assert len(report.issues) == 0


# ---------------------------------------------------------------------------
# 2. 已废止法规
# ---------------------------------------------------------------------------
def test_validate_authority_status_repealed(
    monkeypatch: pytest.MonkeyPatch,
):
    """status=repealed → error，passed=False。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("repealed"),
    )
    statutes = [_make_authority(source_id="s1", status="repealed")]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is False
    assert report.effective_count == 0
    assert len(report.issues) == 1
    assert report.issues[0].severity == "error"
    assert report.issues[0].current_status == "repealed"


# ---------------------------------------------------------------------------
# 3. 尚未生效
# ---------------------------------------------------------------------------
def test_validate_authority_status_not_yet_effective_by_date(
    monkeypatch: pytest.MonkeyPatch,
):
    """effective_date 在未来 → error。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    statutes = [
        _make_authority(
            source_id="s1",
            effective_date=date(2027, 1, 1),  # 未来日期
        )
    ]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is False
    assert any(i.severity == "error" for i in report.issues)


def test_validate_authority_status_not_yet_effective_by_status(
    monkeypatch: pytest.MonkeyPatch,
):
    """status=not_yet_effective → error。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("not_yet_effective"),
    )
    statutes = [_make_authority(source_id="s1", status="not_yet_effective")]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is False
    assert any(
        i.current_status == "not_yet_effective" and i.severity == "error"
        for i in report.issues
    )


# ---------------------------------------------------------------------------
# 4. 已过期
# ---------------------------------------------------------------------------
def test_validate_authority_status_expired(
    monkeypatch: pytest.MonkeyPatch,
):
    """expiry_date 在过去 → error。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    statutes = [
        _make_authority(
            source_id="s1",
            effective_date=date(2010, 1, 1),
            expiry_date=date(2020, 1, 1),  # 已过期
        )
    ]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is False
    assert any("已过期" in i.detail for i in report.issues)


# ---------------------------------------------------------------------------
# 5. 被取代的历史版本（未标注）→ warning
# ---------------------------------------------------------------------------
def test_validate_authority_status_superseded_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    """superseded_by 不为空且未标注「历史适用」→ warning。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective", superseded_by="new-version-id"),
    )
    statutes = [_make_authority(source_id="s1")]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    # warning 不影响 passed
    assert report.passed is True
    assert any(i.severity == "warning" for i in report.issues)
    assert any("历史版本" in i.detail for i in report.issues)


# ---------------------------------------------------------------------------
# 6. 被取代的历史版本（已标注）→ 无 warning
# ---------------------------------------------------------------------------
def test_validate_authority_status_historical_marked(
    monkeypatch: pytest.MonkeyPatch,
):
    """official_source 含「历史适用」→ 不产生 warning。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective", superseded_by="new-version-id"),
    )
    statutes = [
        _make_authority(
            source_id="s1",
            official_source="历史适用 as_of 2018-06-30",
        )
    ]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is True
    # 不应有 warning（已标注历史适用）
    assert all(i.severity != "warning" for i in report.issues)


# ---------------------------------------------------------------------------
# 7. verify_statute_status 异常时回退
# ---------------------------------------------------------------------------
def test_validate_authority_status_fallback_on_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    """verify_statute_status 抛异常时回退到 Authority.status。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_raise,
    )
    # Authority.status = "repealed" → 应检测到 error
    statutes = [_make_authority(source_id="s1", status="repealed")]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    assert report.passed is False
    assert any(i.current_status == "repealed" for i in report.issues)


def test_validate_authority_status_empty():
    """空 statutes 列表 → passed=True, total=0。"""
    report = validate_authority_status([], current_date=date(2026, 7, 23))
    assert report.total_authorities == 0
    assert report.passed is True


def test_validate_authority_status_no_current_date(
    monkeypatch: pytest.MonkeyPatch,
):
    """current_date=None 时仅按 status 判断。"""
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("effective"),
    )
    statutes = [_make_authority(source_id="s1")]
    report = validate_authority_status(statutes, current_date=None)
    assert report.passed is True


# ---------------------------------------------------------------------------
# 8. status == "unknown" → warning（需人工复核，不强制失败）
# ---------------------------------------------------------------------------
def test_validate_authority_status_unknown_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    """status=unknown → warning，passed=True（不强制失败）。

    覆盖 spec 13.2：「如果 status == "unknown"，标记为需要人工复核（warning，
    不强制失败）」。
    """
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("unknown"),
    )
    statutes = [_make_authority(source_id="s1", status="unknown")]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    # warning 不影响 passed
    assert report.passed is True
    assert any(
        i.current_status == "unknown" and i.severity == "warning"
        for i in report.issues
    )
    assert any("人工复核" in i.detail for i in report.issues)


# ---------------------------------------------------------------------------
# 9. repealed 且已标注「历史适用」→ warning（豁免失败）
# ---------------------------------------------------------------------------
def test_validate_authority_status_repealed_with_historical_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    """repealed 且 official_source 含「历史适用」→ warning，passed=True。

    覆盖 spec 13.2：「如果 status == "repealed" 且未明确标注"历史适用"，则失败」
    ——即已标注历史适用时不应失败。
    """
    monkeypatch.setattr(
        "lvyan.validators.authority_status.verify_statute_status",
        _mock_verify("repealed"),
    )
    statutes = [
        _make_authority(
            source_id="s1",
            status="repealed",
            official_source="历史适用 as_of 2018-06-30",
        )
    ]
    report = validate_authority_status(statutes, current_date=date(2026, 7, 23))
    # 已标注历史适用 → warning，不强制失败
    assert report.passed is True
    assert any(
        i.current_status == "repealed" and i.severity == "warning"
        for i in report.issues
    )
    assert any("历史适用" in i.detail for i in report.issues)
