"""完整案件分析报告渲染。"""

from __future__ import annotations

from typing import Any

from lvyan.common.helpers import get_value as _get
from lvyan.nodes.composer_common import (
    _confidence_label,
    _facts_by_category,
    _format_action_advice,
    _format_cases,
    _format_conflicts,
    _format_jurisdiction,
    _format_knowledge_source,
    _format_missing_facts,
    _format_statute_full,
    _format_timeline,
    _tendency_label,
)


def compose_deep(state: Any) -> str:
    """Deep 模式：完整案件分析报告。"""
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    facts = _get(state, "facts", []) or []
    timeline = _get(state, "timeline", []) or []
    statutes = _get(state, "statutes", []) or []
    cases = _get(state, "cases", []) or []
    conflicts = _get(state, "conflicts", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    reasoning_result = _get(state, "reasoning_result", None)
    risk_level = str(_get(state, "risk_level", "low") or "low")
    jurisdiction = _get(state, "jurisdiction", None)
    case_type = _get(state, "case_type", None)
    current_date = _get(state, "current_date", None)

    facts_grouped = _facts_by_category(facts)

    # 案件事实摘要
    fact_section_lines: list[str] = []
    for category in ("当事人", "时间", "金额", "行为", "证据", "其他"):
        items = facts_grouped.get(category, [])
        if items:
            fact_section_lines.append(f"- {category}：" + "；".join(items))
    if not fact_section_lines:
        fact_section_lines.append("- （暂无结构化事实）")

    # 法律关系
    relationship = (
        str(_get(reasoning_result, "legal_relationship", "") or "").strip() or "（待定法律关系）"
    )

    # 构成要件
    elements = _get(reasoning_result, "elements", []) or []
    if elements:
        element_lines = [f"- {e}" for e in elements]
    else:
        element_lines = ["- （暂未识别构成要件）"]

    # 争议焦点
    disputed_focus = _get(reasoning_result, "disputed_focus", []) or []
    if disputed_focus:
        focus_lines = [f"- {f}" for f in disputed_focus]
    else:
        focus_lines = ["- （暂无明显争议焦点）"]

    # 双方主张对比
    plaintiff = _get(reasoning_result, "plaintiff_arguments", []) or []
    defendant = _get(reasoning_result, "defendant_arguments", []) or []
    plaintiff_lines = [f"- {p}" for p in plaintiff] or ["- （暂无）"]
    defendant_lines = [f"- {d}" for d in defendant] or ["- （暂无）"]

    # 证据对应与缺口
    evidence_mapping = _get(reasoning_result, "evidence_mapping", []) or []
    gap_lines = [f"- {m}" for m in evidence_mapping]
    missing_block = _format_missing_facts(missing_facts)
    if missing_block:
        gap_lines.append("- 证据缺口：")
        gap_lines.append(f"  {missing_block.replace(chr(10), chr(10) + '  ')}")
    if not gap_lines:
        gap_lines = ["- （暂无证据对应信息）"]

    # 裁判倾向 + 证据置信度（定性标签，禁止数字概率）
    tendency = _tendency_label(_get(reasoning_result, "judicial_tendency", None))
    confidence = _confidence_label(_get(reasoning_result, "evidence_confidence", None))
    key_factors = _get(reasoning_result, "key_factors", []) or []
    key_factor_lines = [f"- {k}" for k in key_factors] or ["- （暂无）"]

    # 法条引用（全部）
    statute_lines = [_format_statute_full(a) for a in statutes]
    if not statute_lines:
        statute_lines = ["- （暂未检索到适用法条）"]

    # 类案参考
    cases_block = _format_cases(cases)
    # 法规冲突
    conflicts_block = _format_conflicts(conflicts)

    # 行动建议
    advice_lines = _format_action_advice(reasoning_result, missing_facts, risk_level)

    parts: list[str] = [
        "# 案件深度分析报告",
        "",
        "## 用户问题与事实摘要",
        user_goal or "（未明确）",
        "",
        *fact_section_lines,
        "",
        "时间线：",
        _format_timeline(timeline),
        "",
        "## 案件管辖与案由",
        _format_jurisdiction(jurisdiction, case_type),
        "",
        "## 法律关系分析",
        f"- {relationship}",
        "",
        "## 构成要件分析",
        *element_lines,
        "",
        "## 争议焦点",
        *focus_lines,
        "",
        "## 双方主张对比",
        "### 原告主张",
        *plaintiff_lines,
        "### 被告主张",
        *defendant_lines,
        "",
        "## 证据分析与缺口",
        *gap_lines,
        "",
        "## 裁判倾向分析",
        f"- 裁判倾向：{tendency}",
        f"- 证据置信度：{confidence}",
        "### 关键影响因素",
        *key_factor_lines,
        "",
        "## 法条详引",
        *statute_lines,
        "",
        "## 类案参考",
        cases_block,
        "",
        "## 法规冲突提示",
        conflicts_block,
        "",
        "## 行动建议",
        *[f"{i}. {a}" for i, a in enumerate(advice_lines, 1)],
        "",
        "## 风险声明",
        "以上内容仅供参考，不构成正式法律意见。重大事项请咨询持证律师。",
        "",
        "## 知识来源",
        _format_knowledge_source(statutes, current_date, brief=False),
    ]
    return "\n".join(parts)


_compose_deep = compose_deep
