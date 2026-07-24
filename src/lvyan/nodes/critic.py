"""评审节点：对推理结果做对抗性自检。

基于规则+模板的评审实现，后续接入 LLM 增强判断。

职责
----
- 检查 ``reasoning_result`` 是否存在以下三类问题：
  1. **遗漏反方论点**：当 statutes 非空时，defendant_arguments 必须至少 1 条。
  2. **过度推断**：裁判倾向是否与构成要件满足度匹配
     （如 3/5 要件未满足却标注 favorable 则过度推断）。
  3. **法规冲突未处理**：state.conflicts 为空但 statutes 中存在同一法律关系的
     多版本/层级冲突。
- 生成 ``CriticReport``（pydantic 模型），序列化为 dict 写入 ``state["critic_report"]``。
- 路由策略（由 ``route_after_critic`` 实现）：
  * 通过 → citation_verifier
  * 不通过且 iteration < MAX_LEGAL_REASONER_ITERATIONS → 回退 legal_reasoner，
    iteration += 1，追加 critic_feedback
  * 不通过且 iteration >= MAX_LEGAL_REASONER_ITERATIONS → 强制通过，
    risk_level="high"，附警告"自动 critic 未通过，需人工复核"
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from lvyan.config import settings
from lvyan.schemas import CaseState

__all__ = ["critic", "CriticReport", "MAX_LEGAL_REASONER_ITERATIONS"]


# 最大 legal_reasoner 回退迭代次数（从配置读取，默认 2）
MAX_LEGAL_REASONER_ITERATIONS: int = settings.max_legal_reasoner_iterations


# ---------------------------------------------------------------------------
# CriticReport 模型
# ---------------------------------------------------------------------------
class CriticReport(BaseModel):
    """Critic 评审报告。

    - ``passed``：是否通过自检（True 表示可进入下一节点）。
    - ``issues``：发现的问题列表（空列表表示无问题）。
    - ``suggestions``：针对每个问题的改进建议。
    - ``forced_pass``：是否因达到最大迭代次数而强制通过（True 时 risk_level
      应已被设为 "high"）。
    - ``warning``：强制通过时的警告文案。
    """

    passed: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    forced_pass: bool = False
    warning: str | None = None


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


def _count_satisfied_elements(elements: list[str]) -> tuple[int, int]:
    """统计构成要件满足情况，返回 (已满足数, 总数)。

    elements 格式为 ``["要件名（已满足）", "要件名（未满足）"]``。
    """
    total = len(elements)
    if total == 0:
        return 0, 0
    satisfied = sum(1 for e in elements if "已满足" in e)
    return satisfied, total


# ---------------------------------------------------------------------------
# 检查 1：遗漏反方论点
# ---------------------------------------------------------------------------
def _check_missing_defendant_arguments(
    reasoning_result: Any, statutes: list[Any]
) -> tuple[str | None, str | None]:
    """检查 defendant_arguments 是否为空或过于简略。

    当 statutes 非空时，必须有至少 1 条被告主张。
    返回 (issue, suggestion)；无问题时返回 (None, None)。
    """
    if not statutes:
        # statutes 为空时不检查（无法律依据时难以构建抗辩）
        return None, None

    defendant_args = _get(reasoning_result, "defendant_arguments", []) or []
    if len(defendant_args) == 0:
        return (
            "遗漏反方论点：defendant_arguments 为空，但 statutes 非空时应至少提供 1 条被告主张",
            "基于 statutes 中的抗辩/免除/减轻责任条款，补充至少 1 条被告抗辩主张",
        )
    return None, None


# ---------------------------------------------------------------------------
# 检查 2：过度推断
# ---------------------------------------------------------------------------
def _check_over_inference(
    reasoning_result: Any,
) -> tuple[str | None, str | None]:
    """检查裁判倾向是否与构成要件满足度匹配。

    规则：
    - 若 ≥3 个要件未满足（或满足率 < 40%）却标注 favorable/somewhat_favorable → 过度推断。
    - 若全部要件满足却标注 somewhat_unfavorable/insufficient → 过度保守（也视为问题）。
    返回 (issue, suggestion)；无问题时返回 (None, None)。
    """
    elements = _get(reasoning_result, "elements", []) or []
    tendency = _get(reasoning_result, "judicial_tendency", "insufficient")

    if not elements:
        # 无构成要件时不检查过度推断
        return None, None

    satisfied, total = _count_satisfied_elements(elements)
    if total == 0:
        return None, None

    unsatisfied = total - satisfied
    satisfaction_rate = satisfied / total

    # 过度推断：满足率低却标注倾向原告
    # 规则：≥3 个要件未满足，或满足率 < 40%
    if (
        unsatisfied >= 3 or satisfaction_rate < 0.4
    ) and tendency in ("favorable", "somewhat_favorable"):
        return (
            f"过度推断：{unsatisfied}/{total} 个构成要件未满足"
            f"（满足率 {satisfaction_rate:.0%}），却标注裁判倾向为「{tendency}」",
            "下调裁判倾向至 even 或更低，确保与构成要件满足度匹配",
        )

    # 过度保守：全部满足却标注不利
    if satisfaction_rate >= 1.0 and tendency in (
        "somewhat_unfavorable",
        "insufficient",
    ):
        return (
            f"过度保守：全部 {total} 个构成要件已满足，却标注裁判倾向为「{tendency}」",
            "上调裁判倾向至 somewhat_favorable 或 favorable",
        )

    return None, None


# ---------------------------------------------------------------------------
# 检查 3：法规冲突未处理
# ---------------------------------------------------------------------------
def _check_unhandled_statute_conflicts(
    statutes: list[Any], conflicts: list[Any]
) -> tuple[str | None, str | None]:
    """检查 state.conflicts 是否为空但 statutes 中存在多版本/层级冲突。

    规则：按 title 聚合 statutes，若同一 title 存在多个不同 effective_date
    或多个不同 authority_level，且 conflicts 为空，则视为冲突未处理。
    返回 (issue, suggestion)；无问题时返回 (None, None)。
    """
    if not statutes:
        return None, None

    # 已有 conflicts 时不检查（已处理）
    if conflicts:
        return None, None

    # 按 title 聚合，检测多版本/多层级
    title_dates: dict[str, set[Any]] = {}
    title_levels: dict[str, set[str]] = {}
    for auth in statutes:
        title = str(_get(auth, "title", "") or "").strip()
        if not title:
            continue
        eff_date = _get(auth, "effective_date", None)
        level = str(_get(auth, "authority_level", "") or "").strip()
        title_dates.setdefault(title, set()).add(eff_date)
        if level:
            title_levels.setdefault(title, set()).add(level)

    conflict_titles: list[str] = []
    for title, dates in title_dates.items():
        # 多版本：同一 title 有多种不同的 effective_date（None 与具体日期也视为差异）
        real_dates = {d for d in dates if d is not None}
        if len(real_dates) >= 2 or (len(dates) >= 2 and None in dates and real_dates):
            conflict_titles.append(title)
            continue
        # 多层级：同一 title 有多种不同的 authority_level
        levels = title_levels.get(title, set())
        if len(levels) >= 2:
            conflict_titles.append(title)

    if conflict_titles:
        titles_str = "、".join(conflict_titles[:3])
        return (
            f"法规冲突未处理：statutes 中「{titles_str}」存在多版本/多层级，"
            f"但 state.conflicts 为空",
            "由 authority_resolver 检测并填充 conflicts，或在 reasoning 中明确标注冲突处理",
        )

    return None, None


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def critic(state: CaseState) -> dict[str, Any]:
    """评审节点：对推理结果做对抗性自检。

    规则+模板实现，后续接入 LLM 增强判断。

    返回更新字典（覆盖语义）：
        - ``critic_report``: dict（CriticReport 序列化）
        - ``critic_feedback``: list[str]（反馈给 legal_reasoner 的问题清单）
        - ``iteration``: int（不通过时 +1）
        - ``risk_level``: str（强制通过时设为 "high"）
    """
    # TODO: 接入 LLM 增强评审
    reasoning_result = _get(state, "reasoning_result", None)
    statutes = _get(state, "statutes", []) or []
    conflicts = _get(state, "conflicts", []) or []
    iteration = _get(state, "iteration", 0)
    existing_feedback = _get(state, "critic_feedback", []) or []

    issues: list[str] = []
    suggestions: list[str] = []

    # 若无 reasoning_result，直接不通过
    if reasoning_result is None:
        issues.append("reasoning_result 为空，无法进行评审")
        suggestions.append("先执行 legal_reasoner 节点生成推理结果")
    else:
        # --- 检查 1：遗漏反方论点 ---
        issue, suggestion = _check_missing_defendant_arguments(
            reasoning_result, statutes
        )
        if issue:
            issues.append(issue)
            suggestions.append(suggestion)

        # --- 检查 2：过度推断 ---
        issue, suggestion = _check_over_inference(reasoning_result)
        if issue:
            issues.append(issue)
            suggestions.append(suggestion)

        # --- 检查 3：法规冲突未处理 ---
        issue, suggestion = _check_unhandled_statute_conflicts(statutes, conflicts)
        if issue:
            issues.append(issue)
            suggestions.append(suggestion)

    # --- 决定是否通过 ---
    passed = len(issues) == 0

    if passed:
        # 通过：清空 feedback
        report = CriticReport(
            passed=True,
            issues=[],
            suggestions=[],
            forced_pass=False,
            warning=None,
        )
        return {
            "critic_report": report.model_dump(),
            "critic_feedback": [],
        }

    # 不通过：检查是否已达最大迭代次数
    if iteration < MAX_LEGAL_REASONER_ITERATIONS:
        # 回退 legal_reasoner：iteration += 1，追加 critic_feedback
        report = CriticReport(
            passed=False,
            issues=issues,
            suggestions=suggestions,
            forced_pass=False,
            warning=None,
        )
        # 合并已有 feedback 与新 issues（去重）
        new_feedback: list[str] = list(existing_feedback)
        for issue in issues:
            if issue not in new_feedback:
                new_feedback.append(issue)
        return {
            "critic_report": report.model_dump(),
            "critic_feedback": new_feedback,
            "iteration": iteration + 1,
        }

    # 已达最大迭代次数：强制通过，标记高风险
    warning = "自动 critic 未通过，需人工复核"
    report = CriticReport(
        passed=True,  # 强制通过
        issues=issues,
        suggestions=suggestions,
        forced_pass=True,
        warning=warning,
    )
    # 强制通过时仍保留 feedback 供下游参考
    new_feedback: list[str] = list(existing_feedback)
    for issue in issues:
        if issue not in new_feedback:
            new_feedback.append(issue)
    return {
        "critic_report": report.model_dump(),
        "critic_feedback": new_feedback,
        "risk_level": "high",
    }
