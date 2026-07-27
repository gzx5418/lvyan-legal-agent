"""法规版本有效性验证器（SubTask 13.2）。

验证 ``state.statutes`` 中每条 ``Authority`` 的版本是否当前有效：

1. 调用 ``verify_statute_status`` 核验当前状态。
2. 检查 ``effective_date <= current_date``（``effective_date`` 存在时）。
3. 检查 ``expiry_date > current_date`` 或 ``expiry_date`` 为 ``None``。
4. 若 ``status == "repealed"`` 或 ``"not_yet_effective"``，标记为 ``error``。
5. 若引用了历史版本（``superseded_by`` 不为空）但未明确标注「历史适用」，
   标记为 ``warning``。

公开接口
--------
    validate_authority_status(statutes, current_date=None) -> AuthorityStatusReport
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from lvyan.retrieval.version_aware import verify_statute_status

__all__ = [
    "AuthorityStatusIssue",
    "AuthorityStatusReport",
    "validate_authority_status",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class AuthorityStatusIssue(BaseModel):
    """单条法规版本有效性问题。"""

    source_id: str
    title: str
    current_status: str
    expected_status: Literal["effective"]
    severity: Literal["error", "warning"]
    detail: str


class AuthorityStatusReport(BaseModel):
    """法规版本有效性校验报告。"""

    total_authorities: int
    effective_count: int
    issues: list[AuthorityStatusIssue]
    passed: bool  # 0 error 才算 passed


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_date(value: Any) -> date | None:
    """把任意值转换为 ``date``，无法转换时返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            try:
                return date.fromisoformat(s[:10].replace("/", "-"))
            except ValueError:
                return None
    return None


# 「历史适用」标注关键词：出现在 official_source / content_hash 等字段时认为已显式标注
_HISTORICAL_MARKERS: tuple[str, ...] = (
    "历史适用",
    "历史版本",
    "historical",
    "as_of",
    "历史法",
)


def _is_historical_application_marked(authority: Any) -> bool:
    """检查 Authority 是否明确标注「历史适用」。

    当前 ``Authority`` 模型无专门的 ``historical_marker`` 字段，因此通过扫描
    ``official_source`` 字段中的关键词判定（如「历史适用」「historical」等）。
    未标注时返回 ``False``。
    """
    official_source = str(_get(authority, "official_source", "") or "")
    return any(marker in official_source for marker in _HISTORICAL_MARKERS)


def _check_authority(
    authority: Any,
    current_date: date | None,
) -> list[AuthorityStatusIssue]:
    """对单条 Authority 做版本有效性校验，返回问题列表（可能为空）。"""
    issues: list[AuthorityStatusIssue] = []

    source_id = str(_get(authority, "source_id", "") or "")
    title = str(_get(authority, "title", "") or "")
    own_status = str(_get(authority, "status", "unknown") or "unknown")
    effective_date = _to_date(_get(authority, "effective_date", None))
    expiry_date = _to_date(_get(authority, "expiry_date", None))

    # 通过 verify_statute_status 获取有效性（P0-1：传入 as_of=current_date 支持历史校验）
    current_status = own_status
    superseded_by: str | None = None
    if source_id:
        try:
            verification = verify_statute_status(source_id, as_of=current_date)
        except Exception:  # noqa: BLE001  查询失败回退到 authority.status
            verification = None
        if verification is not None:
            effective_as_of = _get(verification, "is_effective_as_of", None)
            if current_date is not None and effective_as_of is True:
                current_status = "effective"
            elif current_date is not None and effective_as_of is False:
                if effective_date is not None and effective_date > current_date:
                    current_status = "not_yet_effective"
                else:
                    current_status = "repealed"
            else:
                verified_status = _get(verification, "current_status", None)
                if verified_status and verified_status != "unknown":
                    current_status = str(verified_status)
            superseded_by = _get(verification, "superseded_by", None)

    # 规则 4a：status == "repealed" 且未标注「历史适用」→ error；已标注 → warning
    if current_status == "repealed":
        if _is_historical_application_marked(authority):
            # 已标注历史适用：可作历史参照，降级为 warning（不强制失败）
            issues.append(
                AuthorityStatusIssue(
                    source_id=source_id,
                    title=title,
                    current_status=current_status,
                    expected_status="effective",
                    severity="warning",
                    detail="法规已废止但已标注「历史适用」，可作历史参照引用",
                )
            )
        else:
            # 未标注历史适用：不应作为现行有效法条引用 → error
            issues.append(
                AuthorityStatusIssue(
                    source_id=source_id,
                    title=title,
                    current_status=current_status,
                    expected_status="effective",
                    severity="error",
                    detail="法规状态为「已废止」，不应作为现行有效法条引用",
                )
            )
        return issues  # 状态已失效，后续时间检查无意义

    # 规则 4b：status == "not_yet_effective" → error
    if current_status == "not_yet_effective":
        issues.append(
            AuthorityStatusIssue(
                source_id=source_id,
                title=title,
                current_status=current_status,
                expected_status="effective",
                severity="error",
                detail="法规状态为「尚未生效」，不应作为现行有效法条引用",
            )
        )
        return issues  # 状态未生效，后续时间检查无意义

    # 规则 2：effective_date > current_date → error（尚未生效）
    if (
        current_date is not None
        and effective_date is not None
        and effective_date > current_date
    ):
        issues.append(
            AuthorityStatusIssue(
                source_id=source_id,
                title=title,
                current_status=current_status,
                expected_status="effective",
                severity="error",
                detail=(
                    f"生效日期 {effective_date.isoformat()} 晚于当前日期 "
                    f"{current_date.isoformat()}，法规尚未生效"
                ),
            )
        )
        return issues

    # 规则 3：expiry_date <= current_date → error（已过期）
    if (
        current_date is not None
        and expiry_date is not None
        and expiry_date <= current_date
    ):
        issues.append(
            AuthorityStatusIssue(
                source_id=source_id,
                title=title,
                current_status=current_status,
                expected_status="effective",
                severity="error",
                detail=(
                    f"失效日期 {expiry_date.isoformat()} 早于或等于当前日期 "
                    f"{current_date.isoformat()}，法规已过期"
                ),
            )
        )
        return issues

    # 规则 6：引用了历史版本（被取代）但未明确标注「历史适用」→ warning
    if superseded_by and not _is_historical_application_marked(authority):
        issues.append(
            AuthorityStatusIssue(
                source_id=source_id,
                title=title,
                current_status=current_status,
                expected_status="effective",
                severity="warning",
                detail=(
                    f"引用了被取代的历史版本（被 {superseded_by} 取代），"
                    f"但未明确标注「历史适用」"
                ),
            )
        )

    # 规则 7：status == "unknown" → warning（需人工复核，不强制失败）
    # 当法规状态无法确认为 effective 时，标记需人工复核其当前有效性
    if current_status == "unknown":
        issues.append(
            AuthorityStatusIssue(
                source_id=source_id,
                title=title,
                current_status=current_status,
                expected_status="effective",
                severity="warning",
                detail="法规状态为「unknown」，需人工复核其当前有效性",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def validate_authority_status(
    statutes: list[Any],
    current_date: date | None = None,
) -> AuthorityStatusReport:
    """验证 ``statutes`` 中每条 ``Authority`` 的版本有效性。

    Args:
        statutes: ``list[Authority]``（``state.statutes``）
        current_date: 当前日期，用于 ``effective_date`` / ``expiry_date`` 比对。
            ``None`` 时不做时间校验，仅按 ``status`` 字段判断。

    Returns:
        AuthorityStatusReport：含 ``total_authorities`` / ``effective_count`` /
        ``issues`` / ``passed``。``passed=True`` 当且仅当无 ``error`` 级别问题。
    """
    total = len(statutes)
    all_issues: list[AuthorityStatusIssue] = []

    for authority in statutes:
        all_issues.extend(_check_authority(authority, current_date))

    # 统计有效数 = 总数 - error 数（warning 不影响 effective_count）
    error_count = sum(1 for i in all_issues if i.severity == "error")
    effective_count = max(0, total - error_count)
    has_error = error_count > 0

    return AuthorityStatusReport(
        total_authorities=total,
        effective_count=effective_count,
        issues=all_issues,
        passed=not has_error,
    )
