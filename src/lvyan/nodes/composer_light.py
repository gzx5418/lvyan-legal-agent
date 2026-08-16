"""轻量法律咨询答复渲染。"""

from __future__ import annotations

from typing import Any

from lvyan.common.helpers import get_value as _get
from lvyan.nodes.composer_common import (
    _format_action_advice,
    _format_statute_brief,
    _tendency_label,
)
from lvyan.nodes.triage import is_personal_information_dispute


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
    conversation_summary: str = "",
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
        if is_personal_information_dispute(user_goal, conversation_summary):
            return (
                "健康信息通常属于敏感个人信息。公司如无合法处理依据，或者未取得有效同意而"
                "向无关人员公开、传播该信息，可能侵犯个人信息权益和隐私；是否构成违法，"
                "还要核实公开范围、处理目的、信息来源以及是否存在法定例外。"
            )
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
    conversation_summary: str = "",
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
        if is_personal_information_dispute(user_goal, conversation_summary):
            return [
                "立即保存公开页面、群聊、邮件、录屏、链接和发布时间；不要只保留转发后的图片。",
                "向公司书面要求停止公开、删除或更正信息，并要求说明处理目的、范围和依据，保留送达及答复记录。",
                "协商不成时，可向个人信息保护职责部门投诉，或依法主张停止侵害、赔礼道歉和赔偿。",
            ]
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


def compose_light(state: Any) -> str:
    """Light 模式：按提问意图生成简答、材料清单、期限或办事步骤。"""
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    reasoning_result = _get(state, "reasoning_result", None)
    statutes = _get(state, "statutes", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    evidence_requirements = _get(state, "evidence_requirements", []) or []
    conversation_summary = str(_get(state, "conversation_summary", "") or "")
    risk_level = str(_get(state, "risk_level", "low") or "low")
    case_type = str(_get(state, "case_type", "") or "")
    intents = _detect_light_intents(user_goal)
    intent = intents[0]
    conclusion = _light_conclusion(
        reasoning_result, case_type, missing_facts, user_goal, conversation_summary
    )

    statute_lines: list[str] = []
    for auth in statutes[:3]:
        statute_lines.append(f"- {_format_statute_brief(auth)}")
    if not statute_lines:
        statute_lines.append("- （暂未检索到适用法条，建议补充查询）")

    actions = _light_action_advice(
        case_type, reasoning_result, missing_facts, risk_level, user_goal, conversation_summary
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


_compose_light = compose_light
