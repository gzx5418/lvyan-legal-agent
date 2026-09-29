"""类案引用校验器(U-05):案号存在性与案由一致性核验。

与 :mod:`lvyan.validators.citation`(法条引用校验)同构,针对类案:
1. **案号存在性**:输出中出现的案号(如 ``(2022)京01民终123号``)必须能在
   ``state.cases`` 中找到——找不到即 fabricated(对齐法条 not_found 语义)。
   这是"类案幻觉案号"的核心防线(2025 年全球 200+ 起律师提交 AI 捏造判例
   被制裁的同类风险)。
2. **案由一致性**:被引用类案的案由与 ``state.case_type`` 偏离 → warning
   (不阻断:跨案由类比推理在实务中存在,但需提示)。

输出 :class:`CaseCitationReport`,由 citation_verifier 节点并入引用审计汇总。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from lvyan.common.helpers import get_value as _get

__all__ = ["CaseCitationIssue", "CaseCitationReport", "validate_case_citations"]

# 案号模式:(2022)京01民终123号 / （2022）京01民终123号 / 2021京01民终77号。
# 结构:年份 + 法院代字(汉字/字母/数字) + 文书种类 + 序号 + 号。
# 以"种类词必须存在"(民/刑/执/行/知/破/赔/再/初/终)作约束,
# 防"2023年收入123号""XX路123号"这类门牌/编号误报。
_CASE_NUMBER_RE = re.compile(
    r"[（(]?\d{4}[)）]?[\u4e00-\u9fffA-Za-z0-9]{0,8}"
    r"(?=[\u4e00-\u9fffA-Za-z0-9]{0,8}号)"
    r"[\u4e00-\u9fffA-Za-z0-9]*?(?:民|刑|执|行|知|破|赔|再|初|终)"
    r"[\u4e00-\u9fffA-Za-z0-9]{0,6}?\d{1,10}号"
)

# 案由关键词提取:被引用类案的 case_type 与 state.case_type 的比较用
# 简单包含/被包含判定(劳动争议 ⊂ 劳动争议纠纷 视为一致)


class CaseCitationIssue(BaseModel):
    """单条类案引用问题。"""

    citation_id: str
    issue_type: Literal["case_not_found", "case_type_mismatch"]
    severity: Literal["error", "warning"]
    detail: str


class CaseCitationReport(BaseModel):
    """类案引用校验报告。"""

    total_case_numbers: int
    not_found: int
    mismatches: int
    issues: list[CaseCitationIssue] = Field(default_factory=list)
    passed: bool  # 0 error 即通过(warning 不阻断)


def _extract_case_numbers(text: str) -> list[str]:
    """提取输出文本中的案号(去重保序)。"""
    seen: set[str] = set()
    result: list[str] = []
    for match in _CASE_NUMBER_RE.finditer(text):
        raw = match.group(0).strip()
        # 归一化括号(全/半角),便于与库内案号比对
        normalized = raw.replace("（", "(").replace("）", ")")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _normalize_case_number(value: str) -> str:
    return value.replace("（", "(").replace("）", ")").strip()


def _case_type_compatible(a: str, b: str) -> bool:
    """案由兼容判定:相等或一方为另一方前缀(劳动争议 vs 劳动争议纠纷)。"""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return True  # 缺失任一侧不判不一致
    return a == b or a.startswith(b) or b.startswith(a)


def _iter_cases(cases: list[Any]) -> list[Any]:
    return cases or []


def validate_case_citations(
    text: str,
    cases: list[Any],
    case_type: str | None = None,
) -> CaseCitationReport:
    """校验输出文本中的类案引用。

    Args:
        text: 用户可见的最终输出文本。
        cases: ``state.cases``(CaseAuthority 列表或 dict 列表)。
        case_type: 当前案件案由(triage 产出,可选)。

    Returns:
        CaseCitationReport:passed = 无 error(案号缺失);
        案由偏离为 warning,不阻断。
    """
    issues: list[CaseCitationIssue] = []
    extracted = _extract_case_numbers(text)

    # 库内案号集合(归一化)
    known_numbers: dict[str, Any] = {}
    for case in _iter_cases(cases):
        number = _get(case, "case_number", None)
        if number:
            known_numbers[_normalize_case_number(str(number))] = case

    not_found = 0
    mismatches = 0
    for number in extracted:
        matched = known_numbers.get(number)
        if matched is None:
            not_found += 1
            issues.append(
                CaseCitationIssue(
                    citation_id=number,
                    issue_type="case_not_found",
                    severity="error",
                    detail=(
                        f"案号 {number} 不在本次检索结果中,疑似虚构案例。"
                        "类案引用必须来自检索返回的真实案例。"
                    ),
                )
            )
            continue
        # 案由一致性(仅当库内类案携带 case_type 时检查)
        cited_case_type = str(_get(matched, "case_type", "") or "")
        if case_type and cited_case_type and not _case_type_compatible(cited_case_type, case_type):
            mismatches += 1
            issues.append(
                CaseCitationIssue(
                    citation_id=number,
                    issue_type="case_type_mismatch",
                    severity="warning",
                    detail=(
                        f"类案 {number} 案由({cited_case_type})与本案案由"
                        f"({case_type})不一致,跨案由类比请核实相关性。"
                    ),
                )
            )

    return CaseCitationReport(
        total_case_numbers=len(extracted),
        not_found=not_found,
        mismatches=mismatches,
        issues=issues,
        passed=not_found == 0,
    )
