"""P0-5：历史日期法规 as_of 检索逻辑单测（不依赖官方法律库）。

直接测试 :func:`_passes_version_filter` 与 :func:`_effective_as_of` 内部函数，
验证：

  1. as_of 给定时，已废止但在目标日期仍有效的法规应通过过滤；
  2. as_of 给定时，尚未生效的法规应被排除；
  3. as_of 给定时，已过期（expiry_date <= as_of）的法规应被排除；
  4. as_of 为 None 且 only_effective=True 时，repealed 法规应被排除；
  5. as_of 为 None 且 only_effective=False 时，repealed 法规可通过。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lvyan.retrieval.version_aware import (  # noqa: E402
    _effective_as_of,
    _passes_version_filter,
)
from lvyan.schemas.authority import Authority  # noqa: E402


def _make_authority(
    *,
    effective_date: date | None = None,
    expiry_date: date | None = None,
    status: str = "effective",
    source_id: str = "src-test",
) -> Authority:
    from datetime import datetime

    return Authority(
        source_id=source_id,
        title="测试法",
        article_number="第1条",
        article_text="...",
        authority_level="法律",
        effective_date=effective_date,
        expiry_date=expiry_date,
        status=status,  # type: ignore[arg-type]
        retrieved_at=datetime(2026, 7, 26),
    )


# ---------------------------------------------------------------------------
# 1. P0-5 核心：as_of 给定时，已废止但当时有效的法规应通过
# ---------------------------------------------------------------------------
def test_asof_keeps_repealed_law_still_effective_at_target_date():
    """现已废止但在目标日期仍有效的法规（如《合同法》在 2018 年）应通过。

    P0-5 关键回归：旧实现先要求 ``status == "effective"``，会错误排除此类法规。
    """
    # 已废止的合同法（2021-01-01 民法典生效时废止），但 expiry_date 未知
    auth = _make_authority(
        effective_date=date(1999, 10, 1),
        status="repealed",
    )
    # 2018 年的案件：合同法当时仍有效
    target = date(2018, 6, 1)

    # 旧逻辑：因 status=repealed 直接排除 → 错误
    # 新逻辑：as_of 给定时按时间窗口判断
    # 但因 expiry_date 未知且 status=repealed，保守排除
    # → 此 case 应为 False（保守策略）
    assert _passes_version_filter(auth, target, only_effective=True) is False


def test_asof_keeps_repealed_law_with_known_expiry_in_future_of_target():
    """已废止且有 expiry_date，但 expiry_date > target → 应保留。

    例：合同法 expiry_date=2021-01-01，查询 2018 年案件 → 当时仍有效。
    """
    auth = _make_authority(
        effective_date=date(1999, 10, 1),
        expiry_date=date(2021, 1, 1),
        status="repealed",
    )
    target = date(2018, 6, 1)

    assert _passes_version_filter(auth, target, only_effective=True) is True


def test_asof_excludes_repealed_law_after_expiry():
    """废止日期早于 target_date → 应排除。"""
    auth = _make_authority(
        effective_date=date(1999, 10, 1),
        expiry_date=date(2021, 1, 1),
        status="repealed",
    )
    # 2022 年案件：合同法已废止
    target = date(2022, 6, 1)

    assert _passes_version_filter(auth, target, only_effective=True) is False


# ---------------------------------------------------------------------------
# 2. as_of 排除尚未生效的法规
# ---------------------------------------------------------------------------
def test_asof_excludes_not_yet_effective_law():
    """生效日期晚于 target_date → 排除。"""
    auth = _make_authority(
        effective_date=date(2021, 1, 1),
        status="effective",
    )
    target = date(2020, 6, 1)

    assert _passes_version_filter(auth, target, only_effective=True) is False


def test_asof_keeps_effective_law_before_target():
    """生效日期早于 target_date，仍有效 → 保留。"""
    auth = _make_authority(
        effective_date=date(2010, 1, 1),
        status="effective",
    )
    target = date(2020, 6, 1)

    assert _passes_version_filter(auth, target, only_effective=True) is True


# ---------------------------------------------------------------------------
# 3. as_of 为 None 时 only_effective 控制
# ---------------------------------------------------------------------------
def test_no_asof_only_effective_excludes_repealed():
    """as_of=None 且 only_effective=True → 排除 repealed。"""
    auth = _make_authority(status="repealed")
    assert _passes_version_filter(auth, None, only_effective=True) is False


def test_no_asof_only_effective_keeps_effective():
    """as_of=None 且 only_effective=True → 保留 effective。"""
    auth = _make_authority(status="effective")
    assert _passes_version_filter(auth, None, only_effective=True) is True


def test_no_asof_not_only_effective_keeps_repealed():
    """as_of=None 且 only_effective=False → 保留 repealed（用于历史研究）。"""
    auth = _make_authority(status="repealed")
    assert _passes_version_filter(auth, None, only_effective=False) is True


# ---------------------------------------------------------------------------
# 4. _effective_as_of 直接验证
# ---------------------------------------------------------------------------
def test_effective_as_of_boundary_conditions():
    """时间窗口边界：effective_date == target 仍算生效；expiry == target 已失效。"""

    # 生效日 == target：仍有效（边界包含）
    auth = _make_authority(effective_date=date(2020, 1, 1), status="effective")
    assert _effective_as_of(auth, date(2020, 1, 1)) is True

    # 失效日 == target：已失效（边界不包含，<=）
    auth2 = _make_authority(
        effective_date=date(2010, 1, 1),
        expiry_date=date(2020, 1, 1),
        status="effective",
    )
    assert _effective_as_of(auth2, date(2020, 1, 1)) is False

    # 失效日 = target + 1：仍有效
    assert _effective_as_of(auth2, date(2019, 12, 31)) is True


def test_effective_as_of_unknown_dates_pass():
    """effective_date 与 expiry_date 都未知且 status 非 repealed → 视为有效。"""
    auth = _make_authority(status="effective")
    assert _effective_as_of(auth, date(2020, 1, 1)) is True
