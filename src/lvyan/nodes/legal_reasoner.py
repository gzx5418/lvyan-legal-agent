"""法律推理节点：要件分析、争议焦点与裁判倾向推断。

PR2 升级：双层 Agent —— LLM 推理（JSON 模式 + 规则校验）+ 规则降级。

职责
----
- 读取 ``facts`` / ``statutes`` / ``cases`` / ``evidence_requirements`` /
  ``conflicts`` / ``disputed_facts`` / ``missing_facts`` / ``critic_feedback``。
- 基于 ``case_type`` 识别法律关系（劳动/合同/侵权/婚姻/知识产权等）。
- 基于 ``statutes`` 条文文本与案由模板提取构成要件，并标注是否被事实满足。
- 基于 ``disputed_facts`` 生成争议焦点。
- 基于事实构建原告主张；基于案由抗辩模板构建被告主张。
- 证据置信度：基于 ``evidence_requirements`` 已满足/未满足比例 + ``missing_facts``
  数量综合判断（高/中/低）。
- 裁判倾向：基于构成要件满足度 + 证据置信度 + 案例支持度综合判定
  （favorable / somewhat_favorable / even / somewhat_unfavorable / insufficient）。
- **严格禁止输出任何数字概率 / 胜诉率**（在校准流程落地前）。
- 结果写入 ``reasoning_result``（覆盖语义）。

注意：未完成裁判数据校准前不输出数字胜诉概率，仅输出定性标签。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from lvyan.schemas import CaseState, ReasoningResult

__all__ = ["legal_reasoner"]

_logger = logging.getLogger("lvyan.nodes.legal_reasoner")

# LLM 推理允许的枚举值
_ALLOWED_TENDENCIES = {
    "favorable",
    "somewhat_favorable",
    "even",
    "somewhat_unfavorable",
    "insufficient",
}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}


# ---------------------------------------------------------------------------
# 案由 → 法律关系定性
# ---------------------------------------------------------------------------
_CASE_TYPE_RELATIONSHIP: dict[str, str] = {
    "工伤认定": "工伤认定（通勤途中非本人主要责任交通事故）",
    "劳动争议": "劳动争议（劳动关系项下经济补偿/赔偿争议）",
    "合同纠纷": "合同纠纷（违约责任）",
    "侵权纠纷": "侵权责任纠纷",
    "婚姻家庭": "婚姻家庭纠纷（离婚及财产分割）",
    "知识产权": "知识产权侵权纠纷",
}


# ---------------------------------------------------------------------------
# 案由 → 构成要件模板（顺序即逻辑链）
# ---------------------------------------------------------------------------
_CASE_TYPE_ELEMENTS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    # 工伤认定（通勤事故）：劳动关系 + 上下班途中 + 交通事故伤害 + 非本人主要责任。
    "工伤认定": [
        ("劳动关系存在", ("劳动合同", "劳动关系", "入职", "工作", "工资", "社保", "工牌", "考勤")),
        (
            "上下班的合理时间、合理路线",
            ("上班", "下班", "上下班", "通勤", "途中", "路线", "住址", "单位"),
        ),
        ("交通事故造成伤害", ("交通事故", "车祸", "碰撞", "事故", "受伤", "受害")),
        (
            "本人非主要责任",
            (
                "非本人主要责任",
                "无责任",
                "无责",
                "次要责任",
                "次责",
                "同等责任",
                "同责",
            ),
        ),
    ],
    # 劳动争议-经济补偿：劳动关系 + 合法解除情形 + 工作年限 + 补偿计算
    "劳动争议": [
        ("劳动关系存在", ("劳动合同", "劳动关系", "入职", "工作", "工资", "考勤")),
        ("合法解除情形", ("解除", "辞退", "开除", "协商", "到期", "终止")),
        ("工作年限", ("工作年限", "在职", "入职", "离职", "工作多久", "连续工作")),
        ("经济补偿计算基数", ("工资", "月工资", "平均工资", "工资条", "银行流水")),
    ],
    # 合同纠纷-违约责任：合同有效 + 违约行为 + 损害后果 + 因果关系
    "合同纠纷": [
        ("合同关系成立", ("合同", "签订", "约定", "协议", "订单")),
        ("违约行为", ("违约", "未履行", "不履行", "拖欠", "逾期", "拒绝")),
        ("损害后果", ("损失", "损害", "利息", "违约金", "差价")),
        ("因果关系", ("导致", "造成", "因而", "以致")),
    ],
    # 侵权纠纷：侵权行为 + 过错 + 损害后果 + 因果关系
    "侵权纠纷": [
        ("侵权行为", ("侵权", "伤害", "损坏", "碰撞", "侵害", "公开", "散布")),
        ("过错", ("故意", "过失", "疏忽", "明知", "应知", "未尽")),
        ("损害后果", ("损失", "损害", "受伤", "医疗费", "误工费", "残疾")),
        ("因果关系", ("导致", "造成", "因而", "以致", "引起")),
    ],
    # 婚姻家庭：婚姻关系 + 法定离婚情形 + 子女抚养 + 财产分割
    "婚姻家庭": [
        ("婚姻关系存在", ("结婚", "婚姻", "夫妻", "配偶")),
        ("法定离婚情形", ("感情破裂", "分居", "家暴", "出轨", "虐待", "遗弃")),
        ("子女抚养安排", ("子女", "孩子", "抚养", "抚养权", "未成年")),
        ("财产分割", ("房产", "存款", "财产", "股权", "车辆", "共同财产")),
    ],
    # 知识产权：权利存在 + 侵权行为 + 损害后果 + 因果关系
    "知识产权": [
        ("权利合法存在", ("专利", "商标", "著作权", "注册", "登记", "证书")),
        ("侵权行为", ("侵权", "仿冒", "盗用", "抄袭", "擅自", "未经许可")),
        ("损害后果", ("损失", "损害", "利润", "维权费用")),
        ("因果关系", ("导致", "造成", "因而", "以致")),
    ],
}


# ---------------------------------------------------------------------------
# 案由 → 被告抗辩模板（基于常见抗辩条款）
# ---------------------------------------------------------------------------
_CASE_TYPE_DEFENSES: dict[str, list[str]] = {
    "工伤认定": [
        "事故并非发生在上下班的合理时间、合理路线，或存在与通勤无关的中断、绕行",
        "道路交通事故责任认定显示劳动者负主要责任或全部责任",
        "双方不存在劳动关系，或存在《工伤保险条例》第十六条规定的排除情形",
    ],
    "劳动争议": [
        "劳动者主动辞职，依《劳动合同法》第三十七条无需支付经济补偿",
        "劳动者严重违反规章制度，依第三十九条过失性辞退无需补偿",
        "已依法足额支付工资社保，不存在第三十八条违法情形",
    ],
    "合同纠纷": [
        "不可抗力导致无法履行，依《民法典》第五百九十条部分或全部免除责任",
        "对方先违约，依《民法典》第五百二十七条行使不安抗辩权",
        "诉讼时效已过，依《民法典》第一百八十八条丧失胜诉权",
    ],
    "侵权纠纷": [
        "受害人故意造成损害，依《民法典》第一千一百七十四条减轻或免除责任",
        "第三人原因导致损害，应由第三人承担侵权责任",
        "不可抗力导致损害，依《民法典》第一百八十条免除责任",
    ],
    "婚姻家庭": [
        "夫妻感情尚未破裂，不符合《民法典》第一千零七十九条离婚条件",
        "子女由己方抚养更有利于子女成长",
        "该财产为个人财产，不属于夫妻共同财产",
    ],
    "知识产权": [
        "原告权利存在瑕疵，不应获得保护",
        "被控行为属于合理使用，不构成侵权",
        "损害赔偿数额计算过高，缺乏依据",
    ],
}


# ---------------------------------------------------------------------------
# 案由 → 原告主张模板（关键词触发）
# ---------------------------------------------------------------------------
_CASE_TYPE_PLAINTIFF: dict[str, list[str]] = {
    "工伤认定": [
        "符合《工伤保险条例》第十四条第（六）项的，应当认定为工伤",
        "应以道路交通事故责任认定书等材料证明本人对事故不负主要责任",
        "用人单位未申请的，劳动者或其近亲属、工会组织可在法定期限内申请工伤认定",
    ],
    "劳动争议": [
        "用人单位违法解除劳动合同，应支付经济补偿金",
        "未签订书面劳动合同的，应支付双倍工资",
        "拖欠工资应予补足并支付经济补偿",
    ],
    "合同纠纷": [
        "对方违约应承担继续履行/采取补救措施/赔偿损失等违约责任",
        "依合同约定主张违约金",
        "要求解除合同并赔偿损失",
    ],
    "侵权纠纷": [
        "要求侵权人停止侵害、赔偿损失",
        "主张医疗费、误工费、护理费等损害赔偿",
        "造成严重精神损害的，主张精神损害赔偿",
    ],
    "婚姻家庭": [
        "请求判决离婚并分割夫妻共同财产",
        "请求确定子女抚养权及抚养费",
        "对方存在过错，请求损害赔偿",
    ],
    "知识产权": [
        "请求停止侵权并赔偿损失",
        "主张侵权人消除影响、赔礼道歉",
        "依法定赔偿或实际损失计算赔偿数额",
    ],
}


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


def _facts_text(facts: list[Any]) -> str:
    """将 facts 列表中所有 content 拼接为单一文本，供关键词匹配。"""
    parts: list[str] = []
    for f in facts or []:
        content = _get(f, "content", "")
        if content:
            parts.append(str(content))
    return " ".join(parts)


def _statutes_text(statutes: list[Any]) -> str:
    """将 statutes 列表中所有 article_text 拼接为单一文本。"""
    parts: list[str] = []
    for s in statutes or []:
        article_text = _get(s, "article_text", "")
        if article_text:
            parts.append(str(article_text))
    return " ".join(parts)


def _identify_legal_relationship(case_type: str | None, user_goal: str, statutes_text: str) -> str:
    """基于 case_type 识别法律关系定性。

    优先使用 case_type 映射；若未匹配，尝试从 user_goal / statutes 文本中
    检测关键词。最终回退到 "待定法律关系"。
    """
    if case_type and case_type in _CASE_TYPE_RELATIONSHIP:
        return _CASE_TYPE_RELATIONSHIP[case_type]

    # 关键词回退检测
    fallback_map: list[tuple[str, tuple[str, ...]]] = [
        (
            "工伤认定（通勤途中非本人主要责任交通事故）",
            ("工伤", "上下班途中", "通勤", "上班路上", "下班路上"),
        ),
        ("劳动争议（劳动关系项下争议）", ("劳动", "工资", "辞退", "经济补偿", "社保")),
        ("合同纠纷（违约责任）", ("合同", "违约", "欠款", "押金")),
        ("侵权责任纠纷", ("侵权", "赔偿", "损害", "受伤")),
        ("婚姻家庭纠纷（离婚及财产分割）", ("离婚", "抚养", "分割")),
        ("知识产权侵权纠纷", ("专利", "商标", "著作权", "侵权")),
    ]
    combined = f"{user_goal} {statutes_text}"
    for relation, keywords in fallback_map:
        if any(kw in combined for kw in keywords):
            return relation
    return "待定法律关系（需进一步查明）"


def _extract_elements(
    case_type: str | None,
    facts_text: str,
    statutes_text: str,
    evidence_requirements: list[Any],
) -> list[str]:
    """基于案由模板提取构成要件，并标注是否被事实满足。

    返回格式：``["要件名（已满足）", "要件名（未满足）", ...]``
    """
    if not case_type or case_type not in _CASE_TYPE_ELEMENTS:
        # 无模板时，尝试从 statutes 文本中提取条文提及的"要件"关键词
        generic_keywords = ("构成要件", "应当", "必须", "需要满足")
        for kw in generic_keywords:
            if kw in statutes_text:
                return [f"法定要件（待查明，依据条文：{kw}）"]
        return []

    elements_template = _CASE_TYPE_ELEMENTS[case_type]
    # 收集已满足的待证事实（evidence_requirements 中 status=met）
    met_facts_text = " ".join(
        str(_get(er, "fact_to_prove", "") or "")
        for er in (evidence_requirements or [])
        if _get(er, "current_status", "") == "met"
    )

    result: list[str] = []
    for element_name, keywords in elements_template:
        # 只能由案件事实或已经核验的证据满足要件。法规文本负责定义规则，
        # 不能反向“证明”用户没有陈述的事实。
        is_satisfied = any(kw in facts_text for kw in keywords) or any(
            kw in met_facts_text for kw in keywords
        )
        if case_type == "工伤认定" and element_name == "上下班的合理时间、合理路线":
            commute_context = any(
                kw in facts_text for kw in ("上班途中", "下班途中", "上下班途中", "通勤途中")
            )
            route_context = any(
                kw in facts_text
                for kw in ("合理路线", "日常路线", "通常路线", "从家到单位", "从单位回家")
            )
            evidence_met = any(kw in met_facts_text for kw in keywords)
            is_satisfied = (commute_context and route_context) or evidence_met
        status = "已满足" if is_satisfied else "待查明"
        result.append(f"{element_name}（{status}）")
    return result


def _count_satisfied_elements(elements: list[str]) -> tuple[int, int]:
    """统计构成要件满足情况，返回 (已满足数, 总数)。"""
    total = len(elements)
    if total == 0:
        return 0, 0
    satisfied = sum(1 for e in elements if "已满足" in e)
    return satisfied, total


def _generate_disputed_focus(disputed_facts: list[Any], case_type: str | None) -> list[str]:
    """基于 disputed_facts 生成争议焦点。

    若 disputed_facts 为空，则基于案由生成常见争议焦点。
    """
    focus: list[str] = []
    for df in disputed_facts or []:
        content = _get(df, "content", "")
        if content:
            focus.append(str(content))

    if focus:
        return focus

    # 回退：基于案由生成常见争议焦点
    fallback_focus: dict[str, list[str]] = {
        "工伤认定": [
            "是否属于上下班的合理时间、合理路线",
            "事故责任是否为本人非主要责任",
            "是否存在劳动关系及法定排除情形",
        ],
        "劳动争议": ["解除劳动合同是否合法", "经济补偿金数额计算"],
        "合同纠纷": ["是否构成违约", "违约金/损失数额"],
        "侵权纠纷": ["侵权责任是否成立", "损害赔偿数额"],
        "婚姻家庭": ["是否符合离婚条件", "财产分割方案"],
        "知识产权": ["是否构成侵权", "赔偿数额计算"],
    }
    if case_type and case_type in fallback_focus:
        return fallback_focus[case_type]
    return ["案件事实与法律适用存在争议"]


def _build_plaintiff_arguments(case_type: str | None, facts_text: str, user_goal: str) -> list[str]:
    """基于事实与案由模板构建原告主张。"""
    args: list[str] = []
    # 从案由模板中选取与事实相关的原告主张
    if case_type and case_type in _CASE_TYPE_PLAINTIFF:
        for tmpl in _CASE_TYPE_PLAINTIFF[case_type]:
            args.append(tmpl)

    # 若事实中包含具体金额/行为，追加一条具体主张
    amounts = re.findall(r"\d+(?:\.\d+)?\s*(?:万|元|块钱)", f"{facts_text} {user_goal}")
    if amounts:
        args.append(f"主张款项数额：{', '.join(amounts[:3])}")

    return args if args else ["请求依法保护合法权益"]


def _build_defendant_arguments(
    case_type: str | None, statutes_text: str, critic_feedback: list[str]
) -> list[str]:
    """基于案由抗辩模板与 statutes 构建被告主张。

    若 critic_feedback 提示"遗漏反方论点"，则强制补充全部抗辩模板。
    """
    args: list[str] = []
    if case_type and case_type in _CASE_TYPE_DEFENSES:
        args.extend(_CASE_TYPE_DEFENSES[case_type])

    # 若 statutes 中包含"抗辩"/"免除"/"减轻"等关键词，追加条文提示
    if statutes_text:
        defense_hints = []
        for kw in ("抗辩", "免除", "减轻", "不承担责任", "免责"):
            if kw in statutes_text:
                defense_hints.append(f"被告可援引含「{kw}」条款进行抗辩")
                break  # 仅追加一条提示
        args.extend(defense_hints)

    # 若 critic 反馈提示遗漏反方论点，确保至少有 1 条被告主张
    feedback_text = " ".join(critic_feedback or [])
    if "反方论点" in feedback_text or "被告主张" in feedback_text:
        if not args and case_type:
            args.extend(_CASE_TYPE_DEFENSES.get(case_type, ["被告主张不存在违法/违约行为"]))

    return args


def _build_evidence_mapping(
    disputed_focus: list[str], evidence_requirements: list[Any]
) -> list[str]:
    """为每个争议焦点建立证据对应关系。"""
    if not disputed_focus:
        return []

    mappings: list[str] = []
    ers = evidence_requirements or []
    for i, focus in enumerate(disputed_focus):
        # 尝试匹配 evidence_requirements
        matched: list[str] = []
        for er in ers:
            fact_to_prove = str(_get(er, "fact_to_prove", "") or "")
            status = _get(er, "current_status", "missing")
            evidence_types = _get(er, "evidence_types", []) or []
            # 简单关键词匹配
            if fact_to_prove and any(kw in focus for kw in fact_to_prove.split() if len(kw) >= 2):
                types_str = "/".join(evidence_types) if evidence_types else "未列明"
                matched.append(f"「{fact_to_prove}」({status}，证据：{types_str})")

        if matched:
            mappings.append(f"争议焦点{i + 1}「{focus}」→ {'; '.join(matched)}")
        else:
            # 无匹配时，提示证据缺口
            mappings.append(f"争议焦点{i + 1}「{focus}」→ 暂无直接对应证据，需补充")

    return mappings


def _compute_evidence_confidence(evidence_requirements: list[Any], missing_facts: list[Any]) -> str:
    """证据置信度：基于已满足/未满足比例 + missing_facts 数量综合判断。

    - 高：≥80% 已满足且 missing ≤ 1
    - 低：<40% 已满足或 missing ≥ 3
    - 中：其余
    """
    ers = evidence_requirements or []
    missing_count = len(missing_facts or [])

    if not ers:
        # 无证据要求时，若 missing_facts 多则低，否则中
        if missing_count >= 3:
            return "low"
        return "medium"

    total = len(ers)
    met_count = sum(1 for er in ers if _get(er, "current_status", "") == "met")
    ratio = met_count / total if total > 0 else 0.0

    if ratio >= 0.8 and missing_count <= 1:
        return "high"
    if ratio < 0.4 or missing_count >= 3:
        return "low"
    return "medium"


def _compute_judicial_tendency(
    elements: list[str],
    evidence_confidence: str,
    statutes: list[Any],
    cases: list[Any],
    missing_facts: list[Any],
) -> str:
    """裁判倾向：基于构成要件满足度 + 证据置信度 + 案例支持度综合判定。

    - 全部构成要件满足 + 证据置信度高 → favorable
    - 大部分要件满足 + 证据置信度中 → somewhat_favorable
    - 部分要件满足 + 证据置信度中 → even
    - 大部分要件未满足 + 证据置信度低 → somewhat_unfavorable
    - missing_facts 过多或 statutes 为空 → insufficient
    """
    # statutes 为空 → 信息不足
    if not statutes:
        return "insufficient"

    # missing_facts 过多（blocking 缺失 ≥ 2 或总缺失 ≥ 4）→ 信息不足
    blocking_missing = sum(
        1 for mf in (missing_facts or []) if bool(_get(mf, "is_blocking", False))
    )
    if blocking_missing >= 2 or len(missing_facts or []) >= 4:
        return "insufficient"

    satisfied, total = _count_satisfied_elements(elements)
    if total == 0:
        return "insufficient"

    ratio = satisfied / total
    has_case_support = len(cases or []) > 0

    # 全部满足 + 高 → favorable（有案例支持更强）
    if ratio >= 1.0 and evidence_confidence == "high":
        return "favorable"
    # 全部满足 + 中 → somewhat_favorable
    if ratio >= 1.0 and evidence_confidence == "medium":
        return "somewhat_favorable"
    # 大部分(≥2/3)满足 + 中/高 → somewhat_favorable
    if ratio >= 2 / 3 and evidence_confidence in ("medium", "high"):
        return "somewhat_favorable"
    # 大部分(≥2/3)满足 + 低 → even
    if ratio >= 2 / 3 and evidence_confidence == "low":
        return "even"
    # 部分(1/3~2/3)满足 + 中 → even
    if ratio >= 1 / 3 and evidence_confidence == "medium":
        return "even"
    # 部分(1/3~2/3)满足 + 高 → somewhat_favorable（证据强但要件未全满足）
    if ratio >= 1 / 3 and evidence_confidence == "high":
        return "somewhat_favorable"
    # 部分(1/3~2/3)满足 + 低 → somewhat_unfavorable
    if ratio >= 1 / 3 and evidence_confidence == "low":
        return "somewhat_unfavorable"
    # 大部分(<1/3)未满足 + 低 → somewhat_unfavorable
    if ratio < 1 / 3 and evidence_confidence == "low":
        return "somewhat_unfavorable"
    # 大部分未满足 + 中 → even（证据尚可但要件严重不足）
    if ratio < 1 / 3 and evidence_confidence == "medium":
        return "even"

    # 有案例支持时略微上调
    if has_case_support and ratio >= 1 / 2:
        return "somewhat_favorable"

    return "insufficient"


def _adjust_for_critic_feedback(tendency: str, critic_feedback: list[str]) -> str:
    """根据 critic 反馈调整裁判倾向（防止过度推断）。

    若 critic 反馈提示"过度推断"，将倾向下调一档。
    """
    if not critic_feedback:
        return tendency

    feedback_text = " ".join(critic_feedback)
    if "过度推断" not in feedback_text:
        return tendency

    # 下调一档
    downgrade_map: dict[str, str] = {
        "favorable": "somewhat_favorable",
        "somewhat_favorable": "even",
        "even": "somewhat_unfavorable",
        "somewhat_unfavorable": "insufficient",
        "insufficient": "insufficient",
    }
    return downgrade_map.get(tendency, tendency)


def _identify_key_factors(
    elements: list[str],
    evidence_confidence: str,
    conflicts: list[Any],
    missing_facts: list[Any],
    statutes: list[Any],
    cases: list[Any],
) -> list[str]:
    """列出影响裁判倾向的关键事实/证据/法规冲突。"""
    factors: list[str] = []

    # 未满足的构成要件
    for e in elements:
        if "未满足" in e:
            factors.append(f"构成要件未满足：{e}")

    # 证据置信度
    if evidence_confidence == "low":
        factors.append("证据置信度低，关键证据缺口较大")
    elif evidence_confidence == "medium":
        factors.append("证据置信度中等，部分证据尚待补强")

    # 法规冲突
    for c in conflicts or []:
        desc = _get(c, "description", "")
        if desc:
            factors.append(f"法规冲突待处理：{desc}")

    # 缺失事实
    blocking_missing = [
        _get(mf, "fact_key", "")
        for mf in (missing_facts or [])
        if bool(_get(mf, "is_blocking", False))
    ]
    if blocking_missing:
        factors.append(f"关键事实缺失：{', '.join(str(k) for k in blocking_missing if k)}")

    # 法规与案例支持情况
    if not statutes:
        factors.append("未检索到适用法规，法律依据不足")
    if not cases:
        factors.append("未检索到类案支持，裁判预测参考有限")

    return factors if factors else ["案件事实清楚、法律适用明确"]


def _assert_no_numeric_probability(result: ReasoningResult) -> None:
    """内部断言：确保 ReasoningResult 中不包含任何数字概率/百分比。

    检查策略：
    1. 校验 ReasoningResult.model_fields 中无概率类字段名。
    2. 将 result 序列化为 JSON 字符串，检索是否存在百分比/概率关键词模式
       （如 "70%", "60%-80%", "胜诉率" 等）。
    3. 若发现违规，抛出 AssertionError。

    注意：此函数为安全守卫，在 legal_reasoner 返回前自检。
    不拦截普通金额数字（如 "10万元"），仅拦截概率/百分比表达。
    """
    # 1. 字段名检查
    forbidden_field_substrings = ("prob", "rate", "percent", "odds", "win", "chance")
    for name in type(result).model_fields.keys():
        low = name.lower()
        for sub in forbidden_field_substrings:
            assert sub not in low, (
                f"ReasoningResult 字段 {name} 含敏感子串 {sub}，违反「禁止数字概率」约束"
            )

    # 2. 序列化文本检查：百分比/概率关键词模式
    payload = result.model_dump_json()

    # 百分比模式：数字后紧跟 % 或 ％（含中文百分号）
    # 概率区间：60%-80% / 60%~80%
    # 概率关键词：胜诉率 / 胜诉概率 / 胜率 / 概率
    probability_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\d+(?:\.\d+)?\s*%"),
        re.compile(r"\d+(?:\.\d+)?\s*％"),
        re.compile(r"\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?\s*[%％]"),
        re.compile(r"(胜诉率|胜诉概率|胜率|概率)"),
    )
    for pattern in probability_patterns:
        assert not pattern.search(payload), (
            f"ReasoningResult 序列化文本含数字概率模式 {pattern.pattern}，"
            f"违反「禁止数字概率」约束；payload={payload}"
        )


# ---------------------------------------------------------------------------
# LLM 增强推理（PR2）
# ---------------------------------------------------------------------------
def _try_llm_reasoning(state: CaseState) -> ReasoningResult | None:
    """尝试用 LLM 生成法律推理结果。

    Returns:
        ``ReasoningResult`` 或 ``None``（LLM 不可用/输出无效时）。
    """
    from lvyan.llm import chat_json, llm_available

    if not llm_available():
        return None

    user_goal = _get(state, "user_goal", "") or ""
    if not user_goal.strip():
        return None

    case_type = _get(state, "case_type", None) or "待定"
    facts = _get(state, "facts", []) or []
    statutes = _get(state, "statutes", []) or []
    cases = _get(state, "cases", []) or []
    evidence_requirements = _get(state, "evidence_requirements", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    critic_feedback = _get(state, "critic_feedback", []) or []

    # 构造上下文摘要（控制 token 量）
    facts_summary = "; ".join(str(_get(f, "content", "")) for f in facts[:10]) or "暂无"
    statutes_summary = (
        "; ".join(
            f"{_get(s, 'title', '')}{_get(s, 'article_number', '')}: {str(_get(s, 'article_text', ''))[:80]}"
            for s in statutes[:5]
        )
        or "暂无"
    )
    cases_summary = (
        "; ".join(str(_get(c, "title", _get(c, "case_title", ""))) for c in cases[:3]) or "暂无"
    )
    er_summary = f"共{len(evidence_requirements)}项，已满足{sum(1 for er in evidence_requirements if _get(er, 'current_status', '') == 'met')}项"
    missing_summary = "; ".join(str(_get(mf, "question", "")) for mf in missing_facts[:3]) or "无"
    critic_summary = "; ".join(critic_feedback[:2]) or "无"
    attachment_ctx = _get(state, "relevant_attachment_context", "") or ""
    attachment_block = f"\n相关材料摘要：\n{attachment_ctx}\n" if attachment_ctx.strip() else ""
    conversation_summary = _get(state, "conversation_summary", "") or ""
    history_block = (
        f"\n此前对话摘要：\n{conversation_summary}\n" if conversation_summary.strip() else ""
    )

    system_prompt = (
        "你是法律推理助手。根据案情事实与检索到的法规，进行法律推理分析。"
        "只输出 JSON，不要解释。"
        "严禁输出任何数字概率、百分比或胜诉率，只输出定性判断。"
    )
    user_prompt = (
        f"案由：{case_type}\n"
        f"用户目标：{user_goal}\n{attachment_block}{history_block}"
        f"已知事实：{facts_summary}\n"
        f"检索法规：{statutes_summary}\n"
        f"类案参考：{cases_summary}\n"
        f"证据情况：{er_summary}\n"
        f"缺失事实：{missing_summary}\n"
        f"评审反馈：{critic_summary}\n\n"
        "请进行法律推理，输出 JSON：\n"
        '{"legal_relationship": "法律关系定性", '
        '"elements": ["要件1（已满足/未满足）"], '
        '"disputed_focus": ["争议焦点1"], '
        '"plaintiff_arguments": ["原告主张1"], '
        '"defendant_arguments": ["被告抗辩1"], '
        '"evidence_mapping": ["争议焦点 → 证据"], '
        '"judicial_tendency": "favorable|somewhat_favorable|even|somewhat_unfavorable|insufficient", '
        '"evidence_confidence": "high|medium|low", '
        '"key_factors": ["关键因素1"]}\n\n'
        "要求：\n"
        "1. judicial_tendency 必须是上述五种之一\n"
        "2. evidence_confidence 必须是 high/medium/low 之一\n"
        "3. 禁止输出任何百分比或概率数字\n"
        "4. 构成要件标注（已满足）或（未满足）\n"
        "5. 基于事实和法律客观分析，不要编造"
    )

    result = chat_json(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1500,
    )
    if result is None:
        return None

    # 规则校验
    tendency = str(result.get("judicial_tendency", "")).strip()
    if tendency not in _ALLOWED_TENDENCIES:
        return None
    confidence_val = str(result.get("evidence_confidence", "")).strip()
    if confidence_val not in _ALLOWED_CONFIDENCE:
        confidence_val = "medium"

    # 列表字段校验
    def _str_list(key: str) -> list[str]:
        val = result.get(key, [])
        if not isinstance(val, list):
            return []
        return [str(v).strip() for v in val if v and str(v).strip()]

    reasoning = ReasoningResult(
        legal_relationship=str(result.get("legal_relationship", "")).strip() or None,
        elements=_str_list("elements"),
        disputed_focus=_str_list("disputed_focus"),
        plaintiff_arguments=_str_list("plaintiff_arguments"),
        defendant_arguments=_str_list("defendant_arguments"),
        evidence_mapping=_str_list("evidence_mapping"),
        judicial_tendency=tendency,  # type: ignore[arg-type]
        evidence_confidence=confidence_val,  # type: ignore[arg-type]
        key_factors=_str_list("key_factors"),
    )

    # 自检：禁止数字概率
    try:
        _assert_no_numeric_probability(reasoning)
    except AssertionError as exc:
        _logger.warning("LLM 推理输出含数字概率，丢弃: %s", exc)
        return None

    _logger.info("LLM 法律推理成功: tendency=%s", tendency)
    return reasoning


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def legal_reasoner(state: CaseState) -> dict[str, Any]:
    """法律推理节点。

    PR2：优先用 LLM 推理（JSON 模式 + 规则校验 + 概率守卫），
    失败时降级到规则+模板。

    返回更新字典（覆盖语义）：
        - ``reasoning_result``: ReasoningResult
        - ``confidence``: high / medium / low / insufficient（与证据置信度对齐）
    """
    statutes = _get(state, "statutes", []) or []

    # --- 优先 LLM 推理 ---
    llm_result = _try_llm_reasoning(state)
    if llm_result is not None:
        confidence: str
        if not statutes:
            confidence = "insufficient"
        else:
            confidence = llm_result.evidence_confidence
        return {
            "reasoning_result": llm_result,
            "confidence": confidence,
        }

    # --- 降级：规则+模板 ---
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []
    disputed_facts = _get(state, "disputed_facts", []) or []
    cases = _get(state, "cases", []) or []
    evidence_requirements = _get(state, "evidence_requirements", []) or []
    conflicts = _get(state, "conflicts", []) or []
    missing_facts = _get(state, "missing_facts", []) or []
    critic_feedback = _get(state, "critic_feedback", []) or []

    facts_text = _facts_text(facts)
    statutes_text = _statutes_text(statutes)

    legal_relationship = _identify_legal_relationship(case_type, user_goal, statutes_text)
    # 用户当前陈述本身也是事实来源；法规文本仍严格隔离，仅用于定义规则。
    factual_text = f"{user_goal} {facts_text}".strip()
    elements = _extract_elements(case_type, factual_text, statutes_text, evidence_requirements)
    disputed_focus = _generate_disputed_focus(disputed_facts, case_type)
    plaintiff_arguments = _build_plaintiff_arguments(case_type, facts_text, user_goal)
    defendant_arguments = _build_defendant_arguments(case_type, statutes_text, critic_feedback)
    evidence_mapping = _build_evidence_mapping(disputed_focus, evidence_requirements)
    evidence_confidence = _compute_evidence_confidence(evidence_requirements, missing_facts)
    judicial_tendency = _compute_judicial_tendency(
        elements, evidence_confidence, statutes, cases, missing_facts
    )
    judicial_tendency = _adjust_for_critic_feedback(judicial_tendency, critic_feedback)
    key_factors = _identify_key_factors(
        elements, evidence_confidence, conflicts, missing_facts, statutes, cases
    )

    result = ReasoningResult(
        legal_relationship=legal_relationship,
        elements=elements,
        disputed_focus=disputed_focus,
        plaintiff_arguments=plaintiff_arguments,
        defendant_arguments=defendant_arguments,
        evidence_mapping=evidence_mapping,
        judicial_tendency=judicial_tendency,  # type: ignore[arg-type]
        evidence_confidence=evidence_confidence,  # type: ignore[arg-type]
        key_factors=key_factors,
    )

    _assert_no_numeric_probability(result)

    if not statutes:
        confidence = "insufficient"
    else:
        confidence = evidence_confidence

    return {
        "reasoning_result": result,
        "confidence": confidence,
    }
