"""Composer 各渲染模式共享的纯格式化函数。"""

from __future__ import annotations

from typing import Any

from lvyan.common.helpers import get_value as _get
from lvyan.schemas.web import is_official_source_url


_STATUS_LABEL: dict[str, str] = {
    "effective": "有效",
    "repealed": "已废止",
    "not_yet_effective": "尚未生效",
    "unknown": "未知",
}

# 裁判倾向 → 中文标签
_TENDENCY_LABEL: dict[str, str] = {
    "favorable": "有利",
    "somewhat_favorable": "较有利",
    "even": "胶着",
    "somewhat_unfavorable": "较不利",
    "insufficient": "信息不足",
}

# 证据置信度 → 中文标签
_CONFIDENCE_LABEL: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

# 引用校验未通过警告
CITATION_AUDIT_WARNING: str = "⚠ 引用校验未通过，需人工复核\n\n"
_CITATION_AUDIT_WARNING = CITATION_AUDIT_WARNING


def _status_label(status: Any) -> str:
    """法规状态转中文标签。"""
    return _STATUS_LABEL.get(str(status or "unknown"), "未知")


def _tendency_label(tendency: Any) -> str:
    """裁判倾向转中文标签。"""
    return _TENDENCY_LABEL.get(str(tendency or ""), str(tendency or "未知"))


def _confidence_label(confidence: Any) -> str:
    """证据置信度转中文标签。"""
    return _CONFIDENCE_LABEL.get(str(confidence or ""), str(confidence or "未知"))


def _format_article_number(article_number: Any) -> str:
    """条文号归一化为「第X条」展示。"""
    s = str(article_number or "").strip()
    if not s:
        return ""
    if s.startswith("第"):
        s = s[1:]
    if s.endswith("条"):
        s = s[:-1]
    return f"第{s}条"


def _format_statute_brief(auth: Any) -> str:
    """法条简要引用：``《title》第X条：摘录``。"""
    title = str(_get(auth, "title", "") or "")
    article = _format_article_number(_get(auth, "article_number", None))
    text = str(_get(auth, "article_text", "") or "")
    excerpt = text[:60] + ("..." if len(text) > 60 else "")
    return f"《{title}》{article}：{excerpt}"


def _format_statute_full(auth: Any) -> str:
    """法条完整引用：``《title》第X条\n条文全文\n来源 / 有效性``。"""
    title = str(_get(auth, "title", "") or "")
    article = _format_article_number(_get(auth, "article_number", None))
    text = str(_get(auth, "article_text", "") or "")
    source_id = str(_get(auth, "source_id", "") or "")
    status = _status_label(_get(auth, "status", "unknown"))
    source = f"{title} {article}".strip()
    lines = [
        f"- 《{title}》{article}",
        f"  条文全文：{text}",
        f"  来源：{source}（source_id={source_id}）",
        f"  有效性：{status}",
    ]
    return "\n".join(lines)


def format_online_sources(sources: list[Any]) -> str:
    """生成与已校验法条严格分离的联网来源列表。"""
    if not sources:
        return ""

    def _markdown_text(value: Any) -> str:
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )

    lines = ["## 联网权威来源（供核对）"]
    for source in sources[:5]:
        title = _markdown_text(_get(source, "title", "官方来源")).replace("\n", " ")
        url = str(_get(source, "url", "") or "")
        snippet = _markdown_text(_get(source, "snippet", "")).replace("\n", " ")
        source_name = _markdown_text(_get(source, "source_name", "官方来源"))
        if not title or not is_official_source_url(url):
            continue
        lines.append(f"- [{title}]({url})（{source_name}）")
        if snippet:
            lines.append(f"  - {snippet}")
    if len(lines) == 1:
        return ""
    lines.append("\n联网资料仅供核对，不替代上列已校验的法律依据。")
    return "\n".join(lines)


def _facts_by_category(facts: list[Any]) -> dict[str, list[str]]:
    """按 category 分组事实，返回 {category: [content, ...]}。"""
    grouped: dict[str, list[str]] = {}
    for f in facts or []:
        category = str(_get(f, "category", "其他") or "其他")
        content = str(_get(f, "content", "") or "").strip()
        if content:
            grouped.setdefault(category, []).append(content)
    return grouped


def _format_timeline(timeline: list[Any]) -> str:
    """格式化时间线。"""
    if not timeline:
        return "（暂无时间线信息）"
    lines: list[str] = []
    for ev in timeline:
        date = str(_get(ev, "date", "") or "").strip()
        desc = str(_get(ev, "description", "") or "").strip()
        parties = _get(ev, "involved_parties", []) or []
        parties_str = "、".join(str(p) for p in parties if p) if parties else ""
        head = f"[{date}] " if date else ""
        tail = f"（涉及：{parties_str}）" if parties_str else ""
        lines.append(f"- {head}{desc}{tail}")
    return "\n".join(lines)


def _format_cases(cases: list[Any]) -> str:
    """格式化类案参考。"""
    if not cases:
        return "（暂无类案参考）"
    lines: list[str] = []
    for i, c in enumerate(cases, 1):
        case_number = str(_get(c, "case_number", "") or "").strip()
        court = str(_get(c, "court", "") or "").strip()
        ruling = str(_get(c, "ruling_summary", "") or "").strip()
        facts = str(_get(c, "brief_facts", "") or "").strip()
        head = f"### 类案{i}"
        if case_number:
            head += f"（{case_number}）"
        lines.append(head)
        if court:
            lines.append(f"- 法院：{court}")
        if facts:
            lines.append(f"- 简要事实：{facts}")
        if ruling:
            lines.append(f"- 裁判要旨：{ruling}")
    return "\n".join(lines)


def _format_conflicts(conflicts: list[Any]) -> str:
    """格式化法规冲突。"""
    if not conflicts:
        return "（暂无法规冲突）"
    lines: list[str] = []
    for c in conflicts:
        desc = str(_get(c, "description", "") or "").strip()
        resolution = str(_get(c, "resolution", "") or "").strip()
        line = f"- {desc}" if desc else "- 存在未描述的法规冲突"
        if resolution:
            line += f"（处理建议：{resolution}）"
        lines.append(line)
    return "\n".join(lines)


def _format_missing_facts(missing_facts: list[Any]) -> str:
    """格式化缺失事实。"""
    if not missing_facts:
        return ""
    lines: list[str] = []
    for mf in missing_facts:
        question = str(_get(mf, "question", "") or "").strip()
        reason = str(_get(mf, "reason", "") or "").strip()
        blocking = bool(_get(mf, "is_blocking", False))
        flag = "（关键阻断）" if blocking else ""
        line = f"- {question}{flag}" if question else "- 存在缺失事实"
        if reason:
            line += f"：{reason}"
        lines.append(line)
    return "\n".join(lines)


def _format_action_advice(
    reasoning_result: Any, missing_facts: list[Any], risk_level: str
) -> list[str]:
    """基于推理结果与缺失事实生成 3-5 条行动建议。"""
    advice: list[str] = []
    tendency = str(_get(reasoning_result, "judicial_tendency", "") or "")
    if tendency in ("favorable", "somewhat_favorable"):
        advice.append("依据现有证据及时主张权利，注意诉讼时效与举证期限。")
    elif tendency == "even":
        advice.append("事实与证据尚有争议，建议先行补强证据再决定是否起诉。")
    elif tendency == "somewhat_unfavorable":
        advice.append("当前证据偏向不利，建议优先补强关键证据或考虑和解。")
    else:
        advice.append("信息不足，建议先补充关键事实再决定后续行动。")

    # 缺失事实 → 补证建议
    if missing_facts:
        advice.append("针对上述缺失事实向律师补充材料或向对方主张举证。")

    # 证据置信度
    confidence = str(_get(reasoning_result, "evidence_confidence", "") or "")
    if confidence == "low":
        advice.append("证据置信度较低，建议收集书面合同、转账记录、聊天记录等补强证据。")

    # 高风险
    if risk_level == "high":
        advice.append("本案风险等级较高，建议尽快咨询持证律师。")

    # 兜底
    if len(advice) < 3:
        advice.append("保留所有相关证据原件，避免自行与对方达成口头协议。")
    if len(advice) < 3:
        advice.append("如需进一步分析，可提供更详细的事实与证据材料。")

    return advice[:5]


def _format_date(value: Any) -> str:
    """把日期值格式化为 ``YYYY-MM-DD`` 字符串，无法转换时返回空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:10]
    try:
        return value.isoformat()[:10]
    except Exception:  # noqa: BLE001
        return str(value)[:10]


def _format_knowledge_source(statutes: list[Any], current_date: Any, brief: bool = False) -> str:
    """格式化知识来源章节（含法规版本与生效日期）。

    Args:
        statutes: 法规权威条目列表。
        current_date: 检索时间。
        brief: True 时仅列出简要来源（light 模式），False 时列出每条法规版本详情（deep 模式）。
    """
    date_str = _format_date(current_date) or "未知时间"
    lines: list[str] = [f"- 检索时间：{date_str}", "- 数据来源：律言 Agent 法规库"]

    if not statutes:
        lines.append("- 法规版本：（暂无法规版本信息）")
        return "\n".join(lines)

    if brief:
        # Light 模式：仅汇总前 3 条法规版本
        for auth in statutes[:3]:
            title = str(_get(auth, "title", "") or "")
            status = _status_label(_get(auth, "status", "unknown"))
            eff = _format_date(_get(auth, "effective_date", None))
            ver = f"《{title}》" if title else "未知法规"
            if eff:
                ver += f"（{status}，{eff} 生效）"
            else:
                ver += f"（{status}）"
            lines.append(f"- 法规版本：{ver}")
    else:
        # Deep 模式：列出每条法规的版本与生效日期
        lines.append("- 法规版本信息：")
        for auth in statutes:
            title = str(_get(auth, "title", "") or "")
            article = _format_article_number(_get(auth, "article_number", None))
            status = _status_label(_get(auth, "status", "unknown"))
            eff = _format_date(_get(auth, "effective_date", None))
            pub = _format_date(_get(auth, "publication_date", None))
            source = str(_get(auth, "official_source", "") or "") or "律言 Agent 法规库"
            head = f"  - 《{title}》{article}" if title else "  - 未知法规"
            detail_parts = [status]
            if eff:
                detail_parts.append(f"{eff} 生效")
            if pub:
                detail_parts.append(f"{pub} 公布")
            lines.append(f"{head}（{'，'.join(detail_parts)}，来源：{source}）")

    return "\n".join(lines)


def _format_jurisdiction(jurisdiction: Any, case_type: Any) -> str:
    """格式化案件管辖与案由章节。"""
    jur = str(jurisdiction or "").strip() or "未明确"
    ct = str(case_type or "").strip() or "未明确"
    return f"- 管辖地域：{jur}\n- 案由：{ct}"


_format_online_sources = format_online_sources
