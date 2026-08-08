"""管辖权与案件类型分诊节点。

基于规则+模板的管辖分流实现，后续接入 LLM 增强判断。

职责
----
- 根据 ``user_goal`` 判定 ``jurisdiction``（中国大陆 / 港澳台·涉外）。
- 识别 ``case_type``（劳动争议 / 合同纠纷 / 侵权纠纷 / 婚姻家庭 / 知识产权）。
- 分级 ``complexity``（light / deep / document）。
- 检测紧急期限与人身安全风险，设置 ``risk_level``。
- 对涉外案件追加非阻断 ``MissingFact``，提示用户咨询涉外律师。
"""

from __future__ import annotations

from typing import Any
import re

from lvyan.schemas import CaseState, MissingFact

__all__ = ["jurisdiction_triage"]


# ---------------------------------------------------------------------------
# 关键词词表
# ---------------------------------------------------------------------------
# 港澳台 / 涉外关键词
_FOREIGN_KEYWORDS: tuple[str, ...] = (
    "香港",
    "澳门",
    "台湾",
    "港澳台",
    "涉外",
    "跨境",
    "外国法",
    "美国法",
    "欧盟",
    "GDPR",
)

# 案由 → 关键词映射（顺序即匹配优先级）
_CASE_TYPE_KEYWORDS: dict[str, list[str]] = {
    # 通勤交通事故工伤认定的法律要件和普通劳动争议不同，必须优先分流，
    # 不能落入经济补偿/解除劳动合同模板。
    "工伤认定": ["工伤", "上下班途中", "通勤", "上班路上", "下班路上"],
    "劳动争议": ["辞退", "工资", "劳动合同", "劳动仲裁", "经济补偿", "解除劳动合同"],
    "合同纠纷": ["合同", "违约", "押金", "租赁", "买卖", "欠款", "拖欠"],
    "侵权纠纷": ["赔偿", "损害", "交通事故", "医疗", "受伤", "人身损害"],
    "婚姻家庭": ["离婚", "抚养", "继承", "赡养", "扶养"],
    "知识产权": ["专利", "商标", "著作权", "侵权"],
}

# 紧急期限关键词
_URGENCY_KEYWORDS: tuple[str, ...] = (
    "诉讼时效",
    "仲裁",
    "期限",
    "到期",
    "过期",
)

# 人身安全风险关键词
_SAFETY_KEYWORDS: tuple[str, ...] = (
    "人身安全",
    "暴力",
    "威胁",
    "恐吓",
)

# 深度分析关键词（触发 deep 模式）
_COMPLEXITY_DEEP_KEYWORDS: tuple[str, ...] = (
    "起诉",
    "律师函",
    "胜诉",
    "裁判",
    "证据是否充分",
)

# 文书生成关键词（触发 document 模式）
_COMPLEXITY_DOCUMENT_KEYWORDS: tuple[str, ...] = (
    "起草",
    "起诉状",
    "合同审查",
    "律师函",
    "文书",
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


def _detect_case_type(user_goal: str, conversation_summary: str = "") -> str | None:
    """根据当前问题匹配案由；必要时继承同一会话的上下文。

    当前问题始终优先。例如用户明确转问合同问题时，不会被上一轮工伤咨询
    覆盖；仅当它是“需要什么材料”这类省略主语的追问时，才从会话摘要补足案由。
    """
    if not user_goal:
        return None
    for case_type, keywords in _CASE_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in user_goal:
                return case_type
    if conversation_summary:
        # 只继承最近一条包含案由线索的用户消息。助手回答可能包含多个案由或
        # 泛化法律术语，不能作为主题来源；按时间倒序可正确处理会话中的转题。
        user_messages = re.findall(
            r"(?:【用户】|(?:^|\n)用户[：:])(.+?)(?=(?:\n\n【(?:用户|助手)】)|(?:\n(?:用户|助手)[：:])|\Z)",
            conversation_summary,
            flags=re.DOTALL,
        )
        for message in reversed(user_messages):
            for case_type, keywords in _CASE_TYPE_KEYWORDS.items():
                if any(kw in message for kw in keywords):
                    return case_type
    return None


def _detect_complexity(user_goal: str) -> str:
    """复杂度分级：document > deep > light。

    先检 document（起草/起诉状 等），避免被 deep 的 "起诉" 子串误命中
    "起诉状"。
    """
    if not user_goal:
        return "light"
    # document 优先（"起诉状" 含 "起诉"，需先判 document）
    for kw in _COMPLEXITY_DOCUMENT_KEYWORDS:
        if kw in user_goal:
            return "document"
    for kw in _COMPLEXITY_DEEP_KEYWORDS:
        if kw in user_goal:
            return "deep"
    return "light"


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def jurisdiction_triage(state: CaseState) -> dict[str, Any]:
    """管辖权与案件类型分诊节点。

    规则+模板实现，后续接入 LLM 增强判断。

    返回更新字典：
        - ``jurisdiction``: 中国大陆 / 港澳台·涉外
        - ``case_type``: 案由（劳动争议 / 合同纠纷 / ...），未匹配为 None
        - ``complexity``: light / deep / document
        - ``risk_level``: low / medium / high
        - ``missing_facts``: 涉外案件追加非阻断风险提示
    """
    # TODO: 接入 LLM 增强抽取/判断
    user_goal = _get(state, "user_goal", "") or ""

    # --- 管辖判断 ---
    is_foreign = any(kw in user_goal for kw in _FOREIGN_KEYWORDS)
    if is_foreign:
        jurisdiction = "港澳台/涉外"
        risk_level = "high"
        missing_facts: list[MissingFact] = [
            MissingFact(
                fact_key="foreign_jurisdiction_consult",
                question=(
                    "本事项涉及港澳台/涉外因素，建议咨询具备涉外执业资质的律师。"
                    "是否需要为您提示涉外法律适用与司法协助相关风险？"
                ),
                reason="涉外/港澳台案件涉及法律适用选择与司法协助，需专业涉外律师介入",
                is_blocking=False,
            )
        ]
    else:
        jurisdiction = "中国大陆"
        risk_level = "low"
        missing_facts = []

    # --- 案由识别 ---
    case_type = _detect_case_type(
        user_goal,
        str(_get(state, "conversation_summary", "") or ""),
    )

    # --- 紧急期限与人身安全检测 ---
    has_urgency = any(kw in user_goal for kw in _URGENCY_KEYWORDS)
    has_safety_risk = any(kw in user_goal for kw in _SAFETY_KEYWORDS)

    if has_safety_risk:
        risk_level = "high"
    elif has_urgency and risk_level != "high":
        risk_level = "medium"

    # --- 复杂度分级 ---
    # 回答形态只由当前问题意图决定；不采纳前端或调用方预先写入的模式，
    # 避免“选了深度”就把一句追问强制渲染为完整报告。
    complexity = _detect_complexity(user_goal)

    return {
        "jurisdiction": jurisdiction,
        "case_type": case_type,
        "complexity": complexity,
        "risk_level": risk_level,
        "missing_facts": missing_facts,
    }
