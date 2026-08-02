"""组装节点：按 light/deep/document 三模式组装最终意见正文。

职责
----
- 读取 ``state.complexity`` 选择输出模板：
  * light：简短咨询答复（用户目标 / 核心结论 / 关键法条 / 行动建议 / 风险声明）。
  * deep：完整案件分析报告（事实 / 法律关系 / 构成要件 / 争议焦点 / 双方主张 /
    证据对应 / 裁判倾向 / 法条引用 / 类案参考 / 法规冲突 / 行动建议 / 风险声明）。
  * document：调用 :func:`lvyan.tools.export.render_docx` 生成正式文书
    （起诉状 / 律师函 / 法律意见书 / 答辩状），DOCX 渲染失败时降级为 Markdown。
- 融合 ``reasoning_result`` / ``statutes`` / ``cases`` / ``conflicts`` /
  ``missing_facts`` / ``risk_level`` / ``citation_audit`` 写入 ``final_output``。
- 法条引用一律采用「《法律名》第X条」规范格式，并标注来源与有效性。
- 若 ``citation_audit.passed`` 为 False（强制通过场景），输出开头加显著警告。
- 若 ``risk_level == "high"``，输出末尾追加高风险声明。

公开接口
--------
    composer(state) -> dict[str, Any]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lvyan.config import AGENT_DIR
from lvyan.schemas import CaseState
from lvyan.tools.export import ExportResult, render_docx

_logger = logging.getLogger("lvyan.nodes.composer")

__all__ = ["composer"]


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# 法规状态 → 中文有效性标签
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

# 高风险声明
_HIGH_RISK_DISCLAIMER: str = (
    "\n\n---\n⚠ 高风险声明：本案风险等级较高，上述结论存在较大不确定性，"
    "建议尽快咨询持证律师并收集补强证据，切勿仅凭本意见作出不可逆决定。"
)

# 引用校验未通过警告
_CITATION_AUDIT_WARNING: str = "⚠ 引用校验未通过，需人工复核\n\n"


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
    source = f"{title} {_format_article_number(_get(auth, 'article_number', None))}".strip()
    lines = [
        f"- 《{title}》{article}",
        f"  条文全文：{text}",
        f"  来源：{source}（source_id={source_id}）",
        f"  有效性：{status}",
    ]
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


def _format_knowledge_source(
    statutes: list[Any], current_date: Any, brief: bool = False
) -> str:
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


# ---------------------------------------------------------------------------
# Light 模式
# ---------------------------------------------------------------------------
def _compose_light(state: Any) -> str:
    """Light 模式：简短咨询答复。"""
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    reasoning_result = _get(state, "reasoning_result", None)
    statutes = _get(state, "statutes", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    risk_level = str(_get(state, "risk_level", "low") or "low")
    current_date = _get(state, "current_date", None)

    # 核心法律结论（1-2 句）
    tendency = _tendency_label(_get(reasoning_result, "judicial_tendency", None))
    relationship = str(
        _get(reasoning_result, "legal_relationship", "") or ""
    ).strip()
    if relationship:
        conclusion = f"本案法律关系为「{relationship}」，当前裁判倾向：{tendency}。"
    else:
        conclusion = f"根据现有信息，当前裁判倾向：{tendency}。"

    # 关键法条引用（最多 3 条）
    statute_lines: list[str] = []
    for auth in statutes[:3]:
        statute_lines.append(f"- {_format_statute_brief(auth)}")
    if not statute_lines:
        statute_lines.append("- （暂未检索到适用法条，建议补充查询）")

    # 行动建议（最多 3 条）
    advice_lines = _format_action_advice(reasoning_result, missing_facts, risk_level)[:3]

    # 补充信息提示（让用户知道补充哪些信息可获得更精确分析）
    missing_block = _format_missing_facts(missing_facts)
    missing_section: list[str] = []
    if missing_block:
        missing_section = [
            "",
            "## 补充信息提示",
            "为获得更精确的法律分析，建议补充以下信息：",
            missing_block,
        ]

    parts: list[str] = [
        "# 日常咨询快答（轻量模式）",
        "",
        "## 用户目标",
        user_goal or "（未明确）",
        "",
        "## 核心法律结论",
        conclusion,
        "",
        "## 关键法条引用",
        *statute_lines,
        "",
        "## 行动建议",
        *[f"{i}. {a}" for i, a in enumerate(advice_lines, 1)],
        *missing_section,
        "",
        "## 风险声明",
        "以上内容仅供参考，不构成正式法律意见。重大事项请咨询持证律师。",
        "",
        "## 知识来源",
        _format_knowledge_source(statutes, current_date, brief=True),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deep 模式
# ---------------------------------------------------------------------------
def _compose_deep(state: Any) -> str:
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
    relationship = str(
        _get(reasoning_result, "legal_relationship", "") or ""
    ).strip() or "（待定法律关系）"

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
    confidence = _confidence_label(
        _get(reasoning_result, "evidence_confidence", None)
    )
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


# ---------------------------------------------------------------------------
# Document 模式
# ---------------------------------------------------------------------------
# 文书类型关键词映射（按 user_goal 命中检测）
_DOC_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("律师函", ("律师函", "发函", "催告函")),
    ("法律意见书", ("法律意见", "意见书")),
    ("答辩状", ("答辩", "答辩状")),
    ("起诉状", ("起诉", "起诉状", "立案", "诉讼")),
)


def _detect_doc_type(user_goal: str) -> str:
    """根据 user_goal 检测文书类型，默认「法律意见书」。"""
    text = user_goal or ""
    for doc_type, keywords in _DOC_TYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return doc_type
    return "法律意见书"


def _doc_title(doc_type: str) -> str:
    """文书标题。"""
    titles = {
        "起诉状": "民事起诉状",
        "律师函": "律师函",
        "法律意见书": "法律意见书",
        "答辩状": "民事答辩状",
    }
    return titles.get(doc_type, "法律意见书")


def _resolve_template(doc_type: str, case_type: Any) -> str | None:
    """根据文书类型选择 .docx 模板路径，不存在时返回 None。

    优先使用 ``templates/official`` 下的官方示范文本；找不到则返回 None
    （render_docx 会回退为空白文档）。
    """
    official_dir = AGENT_DIR / "templates" / "official"
    case_type_str = str(case_type or "") or ""

    candidates: list[Path] = []
    if doc_type == "起诉状":
        # 按案由选择对应示范文本
        if "劳动" in case_type_str:
            candidates.append(official_dir / "劳动争议纠纷起诉状_官方示范.docx")
        elif "离婚" in case_type_str or "婚姻" in case_type_str:
            candidates.append(official_dir / "离婚纠纷起诉状_官方示范.docx")
        elif "买卖" in case_type_str or "合同" in case_type_str:
            candidates.append(official_dir / "买卖合同纠纷起诉状_官方示范.docx")
        candidates.append(official_dir / "部分案件起诉状答辩状示范文本_67类_官方汇编.docx")
    elif doc_type == "答辩状":
        candidates.append(official_dir / "部分案件起诉状答辩状示范文本_67类_官方汇编.docx")

    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def _resolve_output_path(state: Any, doc_type: str) -> str:
    """计算文书输出路径：``AGENT/outputs/{run_id}-{doc_type}.docx``。"""
    run_id = str(_get(state, "run_id", "run") or "run")
    safe_run_id = "".join(c for c in run_id if c.isalnum() or c in "-_") or "run"
    outputs_dir = AGENT_DIR / "outputs"
    return str(outputs_dir / f"{safe_run_id}-{doc_type}.docx")


def _extract_parties(facts: list[Any]) -> tuple[list[str], list[str]]:
    """从事实中抽取原告/被告当事人。

    简化策略：category==「当事人」的事实，按内容是否含「原告/被告」归类。
    返回 (plaintiffs, defendants)。
    """
    plaintiffs: list[str] = []
    defendants: list[str] = []
    for f in facts or []:
        if str(_get(f, "category", "")) != "当事人":
            continue
        content = str(_get(f, "content", "") or "").strip()
        if not content:
            continue
        if "被告" in content:
            defendants.append(content)
        elif "原告" in content:
            plaintiffs.append(content)
        else:
            # 无明确角色时归原告方
            plaintiffs.append(content)
    return plaintiffs, defendants


def _build_document_markdown(state: Any, doc_type: str) -> str:
    """根据文书类型构建 Markdown 正文。"""
    facts = _get(state, "facts", []) or []
    statutes = _get(state, "statutes", []) or []
    reasoning_result = _get(state, "reasoning_result", None)
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    current_date = _get(state, "current_date", None)
    date_str = str(current_date) if current_date else "____年__月__日"

    plaintiffs, defendants = _extract_parties(facts)
    plaintiff_str = "；".join(plaintiffs) if plaintiffs else "___"
    defendant_str = "；".join(defendants) if defendants else "___"

    # 事实与理由
    fact_lines: list[str] = []
    for f in facts:
        content = str(_get(f, "content", "") or "").strip()
        if content:
            fact_lines.append(f"- {content}")
    if not fact_lines:
        fact_lines = ["- （请补充案件事实）"]
    facts_block = "\n".join(fact_lines)

    # 法律依据
    statute_lines = [_format_statute_full(a) for a in statutes]
    if not statute_lines:
        statute_lines = ["- （暂未检索到适用法条，请补充）"]
    statutes_block = "\n".join(statute_lines)

    # 主张 / 结论
    plaintiff_args = _get(reasoning_result, "plaintiff_arguments", []) or []
    claims_block = (
        "\n".join(f"- {a}" for a in plaintiff_args) if plaintiff_args else "- （请补充诉讼请求）"
    )
    tendency = _tendency_label(_get(reasoning_result, "judicial_tendency", None))
    relationship = str(
        _get(reasoning_result, "legal_relationship", "") or ""
    ).strip()

    title = _doc_title(doc_type)

    if doc_type == "律师函":
        parts = [
            f"# {title}",
            "",
            f"致：{defendant_str}",
            "",
            "## 委托说明",
            f"委托人就「{user_goal or '相关事项'}」委托本律师发函。",
            "",
            "## 事实陈述",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 正式要求",
            claims_block,
            "",
            "## 期限与后果",
            "请于收到本函之日起 15 日内履行上述事项，否则我方将依法采取进一步法律措施。",
            "",
            "律师：___",
            "律师事务所：___",
            f"日期：{date_str}",
        ]
    elif doc_type == "答辩状":
        parts = [
            f"# {title}",
            "",
            "## 当事人",
            f"- 答辩人：{defendant_str}",
            f"- 被答辩人：{plaintiff_str}",
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 答辩意见",
            claims_block,
            "",
            "此致",
            "___人民法院",
            "",
            "答辩人：___",
            f"日期：{date_str}",
        ]
    elif doc_type == "起诉状":
        parts = [
            f"# {title}",
            "",
            "## 当事人信息",
            f"- 原告：{plaintiff_str}",
            f"- 被告：{defendant_str}",
            "",
            "## 诉讼请求",
            claims_block,
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "此致",
            "___人民法院",
            "",
            "具状人：___",
            f"日期：{date_str}",
        ]
    else:  # 法律意见书
        parts = [
            f"# {title}",
            "",
            "## 委托人",
            f"- 委托人：{plaintiff_str}",
            "",
            "## 委托事项",
            user_goal or "（请补充委托事项）",
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 法律分析",
            f"- 法律关系：{relationship or '待定'}",
            f"- 裁判倾向：{tendency}",
            "",
            "## 法律意见",
            claims_block,
            "",
            "落款：___",
            f"日期：{date_str}",
        ]

    # 文书通用风险声明（保证 output_validator 风险声明校验通过）
    parts.extend(
        [
            "",
            "---",
            "⚠ 本文书由律言 Agent 自动生成，仅供参考，不构成正式法律意见，"
            "使用前请持证律师审核。",
        ]
    )
    return "\n".join(parts)


def _compose_document(state: Any) -> tuple[str, dict[str, Any]]:
    """Document 模式：调用 render_docx 生成文书，失败降级为 Markdown。

    返回 ``(output_text, document_payload)``，其中 ``document_payload`` 含
    ``template_name`` 与 ``filled_fields``，供后续 ``render_docx`` 渲染。
    """
    user_goal = str(_get(state, "user_goal", "") or "")
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []
    statutes = _get(state, "statutes", []) or []
    reasoning_result = _get(state, "reasoning_result", None)

    doc_type = _detect_doc_type(user_goal)
    markdown = _build_document_markdown(state, doc_type)
    output_path = _resolve_output_path(state, doc_type)
    template = _resolve_template(doc_type, case_type)

    # 构建 document_payload（template_name + filled_fields）
    plaintiffs, defendants = _extract_parties(facts)
    plaintiff_args = _get(reasoning_result, "plaintiff_arguments", []) or []
    fact_lines: list[str] = []
    for f in facts:
        content = str(_get(f, "content", "") or "").strip()
        if content:
            fact_lines.append(content)
    statute_refs: list[str] = []
    for auth in statutes:
        title = str(_get(auth, "title", "") or "")
        article = _format_article_number(_get(auth, "article_number", None))
        if title:
            statute_refs.append(f"《{title}》{article}")

    document_payload: dict[str, Any] = {
        "template_name": template or f"{doc_type}（无模板，Markdown 降级）",
        "filled_fields": {
            "doc_type": doc_type,
            "title": _doc_title(doc_type),
            "plaintiffs": plaintiffs,
            "defendants": defendants,
            "claims": list(plaintiff_args),
            "facts": fact_lines,
            "statutes": statute_refs,
            "user_goal": user_goal,
            "output_path": output_path,
        },
    }

    footer: str
    try:
        result: ExportResult = render_docx(markdown, output_path, template)
        if result.success:
            footer = (
                f"\n\n---\n文书文件：{result.output_path}（格式：{result.format}）"
            )
            if result.error:
                footer += f"\n⚠ {result.error}"
        else:
            footer = (
                f"\n\n---\n⚠ 文书生成失败：{result.error}\n"
                "以下为 Markdown 降级内容已保留在上方。"
            )
    except Exception as exc:  # noqa: BLE001  渲染异常不中断流程，降级为 Markdown
        footer = (
            f"\n\n---\n⚠ 文书渲染异常：{exc}\n"
            "以下为 Markdown 降级内容已保留在上方。"
        )

    return markdown + footer, document_payload


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def composer(state: CaseState) -> dict[str, Any]:
    """组装节点：按 complexity 模式组装最终意见正文。

    返回更新字典（覆盖语义）：
        - ``final_output``: str（组装后的最终输出）
        - ``document_payload``: dict | None（document 模式的文书载荷，含
          ``template_name`` + ``filled_fields``；非 document 模式为 None）
    """
    complexity = str(_get(state, "complexity", "light") or "light")
    risk_level = str(_get(state, "risk_level", "low") or "low")
    citation_audit = _get(state, "citation_audit", None)

    # 1. 按模式组装
    document_payload: dict[str, Any] | None = None
    if complexity == "deep":
        output = _compose_deep(state)
    elif complexity == "document":
        output, document_payload = _compose_document(state)
    else:
        output = _compose_light(state)

    # 2. citation_audit 未通过 → 开头加显著警告
    audit_passed = _get(citation_audit, "passed", True)
    if audit_passed is False:
        output = _CITATION_AUDIT_WARNING + output

    # 3. risk_level == high → 末尾追加高风险声明
    if risk_level == "high" and "高风险声明" not in output:
        output = output + _HIGH_RISK_DISCLAIMER

    # 4. 结构化输出：构建 LegalAnswerV1 并校验（与 final_output 并行）
    # P0-2：document 模式不构建 legal_answer，避免结构化分析页覆盖文书输出。
    #    document 模式的 Markdown 包含文书正文 + DOCX 信息，LegalAnswerV1
    #    无法承载，应让前端继续展示 Markdown。
    # P0-1：composer 在 output_guardrail 之前构建，此处的 legal_answer 是
    #    未脱敏的初稿。真正的结构化输出由 legal_answer_finalizer 节点在
    #    output_guardrail 之后重建。此处仍保留构建（供 checkpoint 恢复等
    #    非标准路径兜底），但 finalizer 会覆盖它。
    legal_answer_dict: dict[str, Any] | None = None
    if complexity != "document":
        try:
            from lvyan.nodes.answer_builder import build_legal_answer
            from lvyan.nodes.answer_validator import (
                ValidationError as AVError,
                validate_legal_answer,
            )

            cs = state if isinstance(state, CaseState) else CaseState.model_validate(state)
            answer = build_legal_answer(cs)
            validate_legal_answer(answer)
            legal_answer_dict = answer.model_dump(mode="json")
        except AVError as exc:
            _logger.warning("legal_answer 校验失败，仅返回 Markdown: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("legal_answer 构建失败，仅返回 Markdown: %s", exc)

    result: dict[str, Any] = {
        "final_output": output,
        "document_payload": document_payload,
        "legal_answer": legal_answer_dict,
    }
    return result
