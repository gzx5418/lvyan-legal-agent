"""组装节点：按 light/deep/document 三模式组装最终意见正文。

职责
----
- 读取 ``state.complexity`` 选择输出模板：
  * light：简短咨询答复（用户目标 / 核心结论 / 关键法条 / 行动建议 / 风险声明）。
  * deep：完整案件分析报告（事实 / 法律关系 / 构成要件 / 争议焦点 / 双方主张 /
    证据对应 / 裁判倾向 / 法条引用 / 类案参考 / 法规冲突 / 行动建议 / 风险声明）。
  * document：构建文书 Markdown 草稿 + document_payload（template_name +
    filled_fields + output_path）。P0-1 修复：composer 不再直接渲染 DOCX，
    真正的 DOCX 渲染由 legal_answer_finalizer 在 output_guardrail 之后执行，
    确保最终文件与经过引用校验 / 隐私脱敏 / HITL 编辑后的正文一致。
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


# ---------------------------------------------------------------------------
# Light 模式
# ---------------------------------------------------------------------------
def _detect_light_intents(user_goal: str) -> list[str]:
    """识别一个或多个轻量咨询意图，复合问题不再被第一个关键词截断。"""
    intents: list[str] = []
    patterns = (
        ("materials", ("材料", "文件", "证据", "准备", "提交")),
        ("deadline", ("多久", "期限", "几天", "何时", "什么时候", "多长时间")),
        ("procedure", ("怎么申请", "如何申请", "怎么办", "怎么做", "流程", "办理")),
    )
    for intent, words in patterns:
        if any(word in user_goal for word in words):
            intents.append(intent)
    return intents or ["direct"]


def _light_conclusion(
    reasoning_result: Any,
    case_type: str,
    missing_facts: list[Any],
    user_goal: str = "",
) -> str:
    """形成可直接回答用户的问题，而非仅复述法律关系名称。"""
    if case_type == "工伤认定":
        suffix = ""
        if missing_facts:
            suffix = "但仍需以劳动关系、通勤情况和事故责任认定材料进一步确认。"
        return (
            "有可能属于工伤：在上下班的合理时间、合理路线上，"
            "因交通事故受伤且本人不负主要责任的，通常应当认定为工伤。" + suffix
        )

    if case_type == "合同纠纷":
        if "押金" in user_goal or "房东" in user_goal:
            return (
                "房东不能无合同依据、无实际损失地扣留押金。租赁关系结束、房屋已交还且"
                "应付费用结清后，原则上应按约返还；房东主张抵扣，应说明合同依据、具体项目"
                "和实际损失。是否能够追回，主要取决于租赁合同、押金支付凭证和房屋交接证据。"
            )
        return "这属于合同纠纷；一方未按有效合同约定履行义务的，另一方通常可以要求继续履行、采取补救措施或赔偿损失。"
    if case_type == "劳动争议":
        return (
            "是否可以获得工资、经济补偿或赔偿，取决于劳动关系、用人单位处理理由和程序。"
            "应先固定劳动合同、工资记录、考勤及解除通知等证据。"
        )
    if case_type == "侵权纠纷":
        return (
            "造成他人人身或财产损害且行为、过错、损害和因果关系能够证明的，通常应承担相应侵权责任。"
        )
    if case_type == "婚姻家庭":
        return "婚姻家庭问题需分别核实婚姻关系、子女、共同财产和债务；法院会结合具体事实依法处理。"
    if case_type == "知识产权":
        return "权利有效且能够证明对方未经许可实施受控行为的，可以要求停止侵害并依法主张损失。"

    relationship = str(_get(reasoning_result, "legal_relationship", "") or "").strip()
    tendency = _tendency_label(_get(reasoning_result, "judicial_tendency", None))
    if relationship:
        return (
            f"按现有信息，这属于「{relationship}」；目前判断为{tendency}，仍取决于关键事实和证据。"
        )
    return f"现有信息不足以作出确定判断；目前判断为{tendency}，建议先核实关键事实。"


def _format_light_evidence_requirements(requirements: list[Any]) -> list[str]:
    """将证据矩阵压缩成用户可直接照着准备的材料清单。"""
    lines: list[str] = []
    for requirement in requirements[:6]:
        types = _get(requirement, "evidence_types", []) or []
        name = "、".join(str(item) for item in types if item) or "相关证明材料"
        purpose = str(_get(requirement, "fact_to_prove", "") or "").strip()
        lines.append(f"- {name}" + (f"：用于{purpose}" if purpose else ""))
    return lines


def _light_action_advice(
    case_type: str,
    reasoning_result: Any,
    missing_facts: list[Any],
    risk_level: str,
    user_goal: str = "",
) -> list[str]:
    """按案由提供少量可执行建议，避免轻量答复出现通用诉讼话术。"""
    if case_type == "工伤认定":
        return [
            "先取得道路交通事故责任认定书，并保留病历、诊断证明、现场资料。",
            "书面告知单位并请其申请工伤认定；同时备好劳动关系证明。",
            "单位未申请的，可在事故伤害发生之日起1年内自行申请工伤认定。",
        ]
    if case_type == "合同纠纷":
        first = (
            "整理租赁合同、押金支付凭证、缴费记录、退租通知和房屋交接照片。"
            if ("押金" in user_goal or "房东" in user_goal)
            else "整理合同、付款记录、履行记录和双方沟通原件。"
        )
        return [
            first,
            "向对方发送可留痕的书面通知，写明请求、金额、依据和合理履行期限。",
            "逾期仍不处理的，按合同争议解决条款选择调解、仲裁或诉讼。",
        ]
    if case_type == "劳动争议":
        return [
            "保存劳动合同、工资流水、考勤、工作沟通和解除或辞退通知。",
            "向单位书面提出具体请求，并保留送达记录。",
            "协商不成的，在仲裁时效内向有管辖权的劳动人事争议仲裁委员会申请仲裁。",
        ]
    if case_type == "侵权纠纷":
        return [
            "立即固定现场、行为过程、损害结果和身份信息，必要时报警或就医。",
            "整理费用票据、鉴定材料及收入损失证明，书面提出赔偿请求。",
            "无法协商时，可依法申请调解、鉴定或提起诉讼。",
        ]
    if case_type == "婚姻家庭":
        return [
            "整理身份、婚姻、子女、财产和债务材料；有人身危险时优先报警并寻求保护。",
            "明确对子女抚养、财产分割和债务承担的具体方案。",
            "无法达成协议的，可依法申请调解或向有管辖权的法院起诉。",
        ]
    if case_type == "知识产权":
        return [
            "固定权属证书、作品底稿、侵权页面、交易记录和时间信息。",
            "对易灭失的网络证据及时公证或使用可靠电子存证。",
            "结合证据选择平台投诉、行政处理、律师函或诉讼，并避免自行实施报复性操作。",
        ]
    return _format_action_advice(reasoning_result, missing_facts, risk_level)[:3]


def _light_deadline_advice(case_type: str) -> str:
    """给出与案由匹配的期限提示；无法确定时明确依事实核算。"""
    deadlines = {
        "工伤认定": "单位通常应在事故伤害发生之日起30日内申请；单位未申请的，劳动者等可在1年内申请。",
        "劳动争议": "劳动争议申请仲裁的时效通常为1年；拖欠劳动报酬在劳动关系存续期间适用规则不同，应结合离职时间核算。",
        "合同纠纷": "合同请求权的诉讼时效通常为3年，自知道或者应当知道权利受损及义务人之日起计算；合同另有履行期限的，先按约定判断。",
        "侵权纠纷": "侵权损害赔偿请求的诉讼时效通常为3年，自知道或者应当知道权利受损及义务人之日起计算。",
        "婚姻家庭": "离婚请求本身通常不适用普通诉讼时效，但财产、损害赔偿等具体请求可能有期限，应单独核算。",
        "知识产权": "侵害知识产权的民事请求通常适用3年诉讼时效；持续侵权和行政投诉期限需结合行为状态判断。",
    }
    return deadlines.get(
        case_type, "具体期限取决于请求权性质和权利受损时间，建议根据事实单独核算。"
    )


def _compose_light(state: Any) -> str:
    """Light 模式：按提问意图生成简答、材料清单、期限或办事步骤。"""
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    reasoning_result = _get(state, "reasoning_result", None)
    statutes = _get(state, "statutes", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    evidence_requirements = _get(state, "evidence_requirements", []) or []
    risk_level = str(_get(state, "risk_level", "low") or "low")
    case_type = str(_get(state, "case_type", "") or "")
    intents = _detect_light_intents(user_goal)
    intent = intents[0]
    conclusion = _light_conclusion(reasoning_result, case_type, missing_facts, user_goal)

    statute_lines: list[str] = []
    for auth in statutes[:3]:
        statute_lines.append(f"- {_format_statute_brief(auth)}")
    if not statute_lines:
        statute_lines.append("- （暂未检索到适用法条，建议补充查询）")

    actions = _light_action_advice(
        case_type, reasoning_result, missing_facts, risk_level, user_goal
    )
    materials = _format_light_evidence_requirements(evidence_requirements)
    if not materials:
        materials = [f"- {str(_get(item, 'question', '') or '')}" for item in missing_facts[:4]]

    if len(intents) > 1:
        parts = ["# 问题答复", "", "## 直接回答", conclusion]
        if "materials" in intents:
            parts.extend(["", "## 需要准备的材料", *materials])
        if "deadline" in intents:
            parts.extend(["", "## 关键期限", _light_deadline_advice(case_type)])
        parts.extend(
            [
                "",
                "## 下一步",
                *[f"{index}. {action}" for index, action in enumerate(actions, 1)],
            ]
        )
    elif intent == "materials":
        parts = [
            "# 需要准备的材料",
            "",
            "结合当前问题，建议优先准备：",
            *materials,
            "",
            "## 为什么需要这些材料",
            conclusion,
            "",
            "## 下一步",
            *[f"{index}. {action}" for index, action in enumerate(actions, 1)],
        ]
    elif intent == "procedure":
        parts = [
            "# 办理步骤",
            "",
            "## 你可以这样做",
            *[f"{index}. {action}" for index, action in enumerate(actions, 1)],
            "",
            "## 适用条件",
            conclusion,
        ]
    elif intent == "deadline":
        parts = [
            "# 关键期限",
            "",
            conclusion,
            "",
            _light_deadline_advice(case_type),
            "",
            "## 现在应做的事",
            *[f"{index}. {action}" for index, action in enumerate(actions, 1)],
        ]
    else:
        key_questions = [str(_get(item, "question", "") or "") for item in missing_facts[:3]]
        parts = [
            "# 简要答复",
            "",
            "## 直接回答",
            conclusion,
            "",
            "## 还需要确认",
            *([f"- {question}" for question in key_questions] or ["- 暂无额外关键事实需要补充。"]),
            "",
            "## 下一步",
            *[f"{index}. {action}" for index, action in enumerate(actions, 1)],
        ]

    parts.extend(
        [
            "",
            "## 法律依据",
            *statute_lines,
            "",
            "## 风险提示",
            "以上内容仅供参考，不构成正式法律意见。重大事项请咨询持证律师。",
        ]
    )
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
    relationship = str(_get(reasoning_result, "legal_relationship", "") or "").strip()

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
            "⚠ 本文书由律言 Agent 自动生成，仅供参考，不构成正式法律意见，使用前请持证律师审核。",
        ]
    )
    return "\n".join(parts)


def _compose_document(state: Any) -> tuple[str, dict[str, Any]]:
    """Document 模式：构建文书 Markdown 草稿 + document_payload。

    P0-1 修复：本函数 **不再渲染 DOCX**。composer 在 citation_verifier /
    output_guardrail 之前执行，若此时落盘 DOCX，后续的引用修复、隐私脱敏、
    HITL 编辑都不会反映到已生成的文件中，导致最终文件与页面展示内容不一致。

    现在仅生成 Markdown 草稿 + 文书载荷（含 output_path / template_name /
    filled_fields），真正的 ``render_docx`` 由 legal_answer_finalizer 在
    output_guardrail 之后基于最终 ``final_output`` 执行。

    返回 ``(markdown, document_payload)``。
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
        "doc_type": doc_type,
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
            "template": template,
        },
    }

    return markdown, document_payload


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def composer(state: CaseState) -> dict[str, Any]:
    """组装节点：按 complexity 模式组装最终意见正文。

    返回更新字典（覆盖语义）：
        - ``final_output``: str（组装后的最终输出）
        - ``document_payload``: dict | None（document 模式的文书载荷，含
          ``template_name`` + ``filled_fields``；非 document 模式为 None）
        - ``document_file``: None（composer 不再渲染文件；由 finalizer 写入）
        - ``legal_answer``: dict | None（结构化输出初稿，由 finalizer 覆盖）
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
        # P0-1：清空旧 document_file，确保重试 composer 时不会残留过期文件引用。
        # 真正的文件由 legal_answer_finalizer 在 guardrail 之后写入。
        "document_file": None,
        "legal_answer": legal_answer_dict,
    }
    return result
