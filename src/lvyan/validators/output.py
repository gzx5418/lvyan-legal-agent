"""输出结构校验验证器（SubTask 14.3）。

迁移并升级原 ``律言skill/scripts/validate_output.py``，重写为 Pydantic v2 风格，
并扩展为三模式（light / deep / document）结构校验 + 引用校验 + 风险声明校验 +
数字概率拦截。

校验内容
--------
1. **结构校验**：根据 ``complexity`` 模式校验必要章节是否存在。
2. **引用校验**：输出中提到的法条编号必须在 ``statutes`` 中存在。
3. **风险声明校验**：必须包含风险声明关键词。
4. **数字概率拦截**：扫描输出文本中的数字百分比 / 概率表达并标记违规。

公开接口
--------
    validate_output(text, complexity, statutes=None) -> OutputValidationResult
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

from lvyan.validators.citation import _extract_citations, _find_matching_statute

__all__ = [
    "ValidationError",
    "ValidationErrorType",
    "OutputValidationResult",
    "validate_output",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
ValidationErrorType = Literal[
    "missing_section",
    "missing_citation",
    "missing_risk_disclaimer",
    "numeric_probability",
    "invalid_citation",
]


class ValidationError(BaseModel):
    """单条输出校验错误。"""

    error_type: ValidationErrorType
    detail: str


class OutputValidationResult(BaseModel):
    """输出校验结果。

    核心字段：
        - ``passed``：是否通过校验。
        - ``errors``：校验错误列表（``ValidationError``）。
        - ``warnings``：非阻断警告列表。

    Spec 扩展字段（由 ``errors`` / ``warnings`` 派生的计算属性）：
        - ``structural_issues``：结构缺失问题文本列表。
        - ``citation_issues``：引用问题文本列表。
        - ``risk_statement_missing``：是否缺少风险声明。
        - ``numeric_probability_detected``：是否检测到数字概率。
        - ``suggestions``：改进建议（由 warning 派生）。
    """

    passed: bool
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def structural_issues(self) -> list[str]:
        """结构缺失问题（missing_section）文本列表。"""
        return [
            e.detail for e in self.errors if e.error_type == "missing_section"
        ]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def citation_issues(self) -> list[str]:
        """引用问题（invalid_citation / missing_citation）文本列表。"""
        return [
            e.detail
            for e in self.errors
            if e.error_type in ("invalid_citation", "missing_citation")
        ]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_statement_missing(self) -> bool:
        """是否缺少风险声明。"""
        return any(
            e.error_type == "missing_risk_disclaimer" for e in self.errors
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def numeric_probability_detected(self) -> bool:
        """是否检测到数字概率表达。"""
        return any(
            e.error_type == "numeric_probability" for e in self.errors
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def suggestions(self) -> list[str]:
        """改进建议（由 warning 派生）。"""
        return list(self.warnings)


# ---------------------------------------------------------------------------
# 各模式必需章节（任一关键词命中即视为该章节存在）
# ---------------------------------------------------------------------------
# Light 模式：用户目标 / 结论 / 法条 / 建议 / 风险声明（风险声明单独校验）
_LIGHT_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("用户目标", ("用户目标", "咨询目标", "您想了解")),
    ("核心法律结论", ("核心法律结论", "法律结论", "结论", "核心结论")),
    ("关键法条引用", ("关键法条引用", "法条引用", "法律依据", "法条")),
    ("行动建议", ("行动建议", "建议", "你现在可以怎么做", "您可以怎么做")),
]

# Deep 模式：全部章节（类案参考 / 法规冲突为「如有」，作为 warning 而非 error）
_DEEP_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("案件事实摘要", ("案件事实摘要", "事实摘要", "案件事实", "事实与理由")),
    ("法律关系识别", ("法律关系识别", "法律关系", "法律关系定性")),
    ("构成要件分析", ("构成要件分析", "构成要件", "要件分析")),
    ("争议焦点", ("争议焦点", "争议")),
    ("双方主张对比", ("双方主张对比", "双方主张", "原告主张", "被告主张", "主张对比")),
    ("证据对应与缺口", ("证据对应与缺口", "证据对应", "证据缺口", "证据")),
    ("裁判倾向", ("裁判倾向", "裁判趋势")),
    ("法条引用", ("法条引用", "法律依据", "法条")),
    ("行动建议", ("行动建议", "建议")),
]

# Document 模式：文书标题 / 当事人 / 事实理由 / 法律依据 / 落款
_DOCUMENT_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("文书标题", ("起诉状", "律师函", "法律意见书", "答辩状", "申请书", "协议书")),
    ("当事人", ("当事人", "原告", "被告", "申请人", "被申请人", "致：", "委托人")),
    ("事实与理由", ("事实与理由", "事实和理由", "事实陈述", "事实")),
    ("法律依据", ("法律依据", "法条", "法律根据")),
    ("落款", ("落款", "此致", "具状人", "律师：", "律师事务所", "日期", "签字")),
]

# 风险声明关键词（命中任一即视为满足）
_RISK_DISCLAIMER_MARKERS: tuple[str, ...] = (
    "免责",
    "风险提示",
    "风险声明",
    "不构成法律意见",
    "不构成正式法律意见",
    "仅供参考",
    "不构成法律建议",
)

# 标准风险声明（guardrail 自动追加时使用）
STANDARD_RISK_DISCLAIMER: str = (
    "## 风险声明\n"
    "以上内容仅供参考，不构成正式法律意见。重大事项请咨询持证律师。"
)

# 数字概率模式：百分比 / 概率 / 胜诉率
_NUMERIC_PROBABILITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+(?:\.\d+)?\s*[%％]"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:概率|胜诉率|胜率|胜诉概率)"),
    re.compile(r"(?:概率|胜诉率|胜率|胜诉概率)\s*[:：]?\s*\d+(?:\.\d+)?"),
)

# 定性标签替换表（用于 guardrail 将数字概率替换为定性标签）
_QUALITATIVE_LABELS: tuple[str, ...] = (
    "有利",
    "较有利",
    "胶着",
    "较不利",
    "信息不足",
)


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


def _check_sections(
    text: str,
    sections: list[tuple[str, tuple[str, ...]]],
) -> list[ValidationError]:
    """校验必要章节是否存在，返回缺失章节的错误列表。"""
    errors: list[ValidationError] = []
    for section_name, markers in sections:
        if not any(marker in text for marker in markers):
            errors.append(
                ValidationError(
                    error_type="missing_section",
                    detail=f"缺少必要章节：{section_name}",
                )
            )
    return errors


def _check_risk_disclaimer(text: str) -> ValidationError | None:
    """校验风险声明是否存在。"""
    if any(marker in text for marker in _RISK_DISCLAIMER_MARKERS):
        return None
    return ValidationError(
        error_type="missing_risk_disclaimer",
        detail="缺少风险声明（免责/风险提示/不构成法律意见/仅供参考 等）",
    )


def _check_numeric_probability(text: str) -> list[ValidationError]:
    """扫描数字概率表达，返回违规列表。"""
    errors: list[ValidationError] = []
    seen_spans: set[tuple[int, int]] = set()
    for pattern in _NUMERIC_PROBABILITY_PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            # 去重：同一区间可能被多模式命中
            if any(
                span[0] < existing[1] and span[1] > existing[0]
                for existing in seen_spans
            ):
                continue
            seen_spans.add(span)
            errors.append(
                ValidationError(
                    error_type="numeric_probability",
                    detail=f"输出含数字概率表达：{m.group(0)!r}（未校准前禁止）",
                )
            )
    return errors


def _check_citations(
    text: str, statutes: list[Any]
) -> list[ValidationError]:
    """校验输出中提到的法条编号是否在 statutes 中存在。

    - 若输出提到法条引用但 statutes 为空 → missing_citation（composer 漏写法条库）。
    - 若某引用在 statutes 中找不到匹配 → invalid_citation（误写/虚构）。
    """
    errors: list[ValidationError] = []
    citations = _extract_citations(text)
    if not citations:
        return errors

    if not statutes:
        for c in citations:
            errors.append(
                ValidationError(
                    error_type="missing_citation",
                    detail=(
                        f"输出引用了 {c['citation_id']}，但 statutes 为空，"
                        f"无法核对来源"
                    ),
                )
            )
        return errors

    for c in citations:
        matched = _find_matching_statute(c, statutes)
        if matched is None:
            errors.append(
                ValidationError(
                    error_type="invalid_citation",
                    detail=(
                        f"输出引用的 {c['citation_id']} 不在 state.statutes 中，"
                        f"可能为误写或虚构"
                    ),
                )
            )
    return errors


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def validate_output(
    text: str,
    complexity: str,
    statutes: list[Any] | None = None,
) -> OutputValidationResult:
    """校验最终输出文本。

    Args:
        text: 待校验的最终输出文本。
        complexity: 输出模式（``"light"`` / ``"deep"`` / ``"document"``）。
        statutes: ``state.statutes`` 列表，用于引用校验。

    Returns:
        :class:`OutputValidationResult`：含 ``passed`` / ``errors`` / ``warnings``。
    """
    if not text:
        return OutputValidationResult(
            passed=False,
            errors=[
                ValidationError(
                    error_type="missing_section",
                    detail="输出文本为空",
                )
            ],
            warnings=[],
        )

    errors: list[ValidationError] = []
    warnings: list[str] = []
    statutes_list = list(statutes or [])

    # 1. 结构校验
    if complexity == "light":
        errors.extend(_check_sections(text, _LIGHT_SECTIONS))
    elif complexity == "deep":
        errors.extend(_check_sections(text, _DEEP_SECTIONS))
        # 类案参考 / 法规冲突为「如有」：缺失时仅 warning
        if not any(kw in text for kw in ("类案参考", "类案")):
            warnings.append("未提供类案参考（可选，如有应补充）")
        if not any(kw in text for kw in ("法规冲突", "冲突提示")):
            warnings.append("未提供法规冲突提示（可选，如有应补充）")
    elif complexity == "document":
        errors.extend(_check_sections(text, _DOCUMENT_SECTIONS))
    else:
        # 未知模式按 light 校验
        errors.extend(_check_sections(text, _LIGHT_SECTIONS))

    # 2. 引用校验
    errors.extend(_check_citations(text, statutes_list))

    # 3. 风险声明校验
    risk_err = _check_risk_disclaimer(text)
    if risk_err is not None:
        errors.append(risk_err)

    # 4. 数字概率拦截
    errors.extend(_check_numeric_probability(text))

    return OutputValidationResult(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )
