"""计算工具：诉讼时效、赔偿金额、证据清单、时间线。

本模块是 SubTask 15.4 的实现，提供四个标准工具：

  - ``calculate_legal_deadline(event_date, deadline_type, ...)``：诉讼时效 /
    期限计算。
  - ``calculate_claim_amount(claim_type, principal, ...)``：常见赔偿金额计算。
  - ``generate_evidence_checklist(case_type, facts=...)``：按案类型生成证据清单。
  - ``build_case_timeline(events)``：构建案件时间线并标注时效关键节点。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from lvyan.tools.base import ToolResult

# ---------------------------------------------------------------------------
# 常量与规则表
# ---------------------------------------------------------------------------

# 期限类型 -> 天数映射（中国大陆常见诉讼时效与法定期限）
_DEADLINE_RULES: dict[str, int] = {
    "labor_arbitration": 365,        # 劳动仲裁时效 1 年
    "civil_litigation": 1095,        # 民事诉讼时效 3 年
    "civil_litigation_short": 365,   # 特殊短期诉讼时效 1 年（身体伤害等）
    "civil_litigation_long": 1460,   # 最长诉讼时效 4 年（部分情形）
    "administrative_reconsideration": 60,   # 行政复议 60 日
    "administrative_litigation": 180,       # 行政诉讼 6 个月
    "appeal_judgment": 15,          # 不服一审判决上诉期 15 日
    "appeal_ruling": 10,            # 不服一审裁定上诉期 10 日
    "appeal_administrative_judgment": 15,   # 行政诉讼上诉期 15 日
    "contract_quality_objection": 730,      # 质量异议最长 2 年
    "consumer_complaint": 1095,     # 消费者投诉时效参照民事诉讼 3 年
    "insurance_claim": 1460,        # 保险理赔时效参照 4 年（部分险种）
}
"""期限类型 -> 天数映射表。"""

# 期限类型的中文说明（用于 warning 文本）
_DEADLINE_LABELS: dict[str, str] = {
    "labor_arbitration": "劳动仲裁时效（1 年）",
    "civil_litigation": "民事诉讼时效（3 年）",
    "civil_litigation_short": "民事诉讼短期时效（1 年）",
    "civil_litigation_long": "民事诉讼最长时效（4 年）",
    "administrative_reconsideration": "行政复议申请期限（60 日）",
    "administrative_litigation": "行政诉讼起诉期限（6 个月）",
    "appeal_judgment": "不服一审判决上诉期（15 日）",
    "appeal_ruling": "不服一审裁定上诉期（10 日）",
    "appeal_administrative_judgment": "行政诉讼上诉期（15 日）",
    "contract_quality_objection": "质量异议期（最长 2 年）",
    "consumer_complaint": "消费者投诉时效（3 年）",
    "insurance_claim": "保险理赔时效（4 年）",
}

# 案类型 -> 证据清单（每项为 (name, purpose, status) 三元组）
_EVIDENCE_CHECKLIST: dict[str, list[tuple[str, str, str]]] = {
    "劳动争议": [
        ("劳动合同", "证明劳动关系存在及约定内容", "required"),
        ("工资流水/工资条", "证明工资标准与拖欠事实", "required"),
        ("辞退/解除通知", "证明解除事由与时间", "required"),
        ("社保缴纳记录", "证明用工事实", "recommended"),
        ("考勤记录", "证明出勤与加班情况", "recommended"),
        ("工牌/工作证", "证明劳动关系", "recommended"),
        ("同事证言", "佐证劳动关系或违法解除", "optional"),
        ("劳动仲裁申请书", "立案材料", "required"),
    ],
    "民间借贷": [
        ("借条/借款合同", "证明借贷合意", "required"),
        ("银行转账记录", "证明交付事实", "required"),
        ("催款记录（微信/短信）", "证明催告事实与时效中断", "recommended"),
        ("利息约定证据", "证明利息约定", "recommended"),
        ("借款人身份信息", "起诉立案必要", "required"),
        ("担保合同（如有）", "证明担保责任", "optional"),
    ],
    "房屋租赁": [
        ("租赁合同", "证明租赁关系及约定", "required"),
        ("押金收据/转账记录", "证明押金支付", "required"),
        ("房屋交接单", "证明交房时房屋状态", "required"),
        ("维修记录/沟通记录", "证明维修义务履行情况", "recommended"),
        ("租金支付凭证", "证明履约情况", "recommended"),
        ("退房通知", "证明提前退租通知事实", "optional"),
    ],
    "买卖合同": [
        ("买卖合同", "证明买卖关系及约定", "required"),
        ("交付凭证（签收单/物流单）", "证明交付事实", "required"),
        ("付款凭证", "证明付款履行情况", "required"),
        ("质量异议通知", "证明质量异议提出时间", "recommended"),
        ("验收记录", "证明验收情况", "recommended"),
        ("违约金计算依据", "证明违约金主张合理性", "optional"),
    ],
    "离婚": [
        ("结婚证", "证明婚姻关系", "required"),
        ("财产清单及权属证明", "证明夫妻共同财产范围", "required"),
        ("子女出生证明", "证明抚养权归属考量", "required"),
        ("感情破裂证据（分居/家暴/出轨）", "证明离婚法定事由", "recommended"),
        ("债务凭证", "证明共同债务", "optional"),
        ("收入证明", "证明抚养能力", "recommended"),
    ],
    "交通事故": [
        ("事故责任认定书", "证明责任划分", "required"),
        ("医疗费票据", "证明医疗支出", "required"),
        ("误工证明及工资减少证明", "证明误工损失", "required"),
        ("护理证明", "证明护理依赖", "recommended"),
        ("修车发票/定损单", "证明车损", "recommended"),
        ("伤残鉴定意见", "证明伤残等级", "optional"),
        ("交通费票据", "证明交通支出", "optional"),
    ],
    "侵权": [
        ("侵权行为证据（视频/照片/证人）", "证明侵权事实", "required"),
        ("损害结果证据（医疗/财务）", "证明损害结果", "required"),
        ("因果关系证据", "证明行为与损害的因果关系", "required"),
        ("侵权人身份信息", "起诉立案必要", "required"),
        ("过错程度证据", "证明侵权人过错", "recommended"),
    ],
}
"""案类型 -> 证据清单映射。"""

# 赔偿计算规则：claim_type -> (formula_label, calc_fn)
# calc_fn(principal, months, wage) -> (calculated_amount, breakdown, formula_text, notes)


# ---------------------------------------------------------------------------
# 返回模型
# ---------------------------------------------------------------------------
class DeadlineResult(ToolResult):
    """诉讼时效/期限计算结果。"""

    event_date: str
    deadline_type: str
    deadline_days: int = 0
    deadline_date: str = ""
    expires_soon: bool = False  # 7 天内到期
    warning: str | None = None


class ClaimAmountResult(ToolResult):
    """赔偿金额计算结果。"""

    claim_type: str
    principal: float = 0.0
    calculated_amount: float = 0.0
    formula: str = ""
    breakdown: dict[str, float] = Field(default_factory=dict)
    notes: str | None = None


class EvidenceItem(BaseModel):
    """证据清单中的单项证据。"""

    name: str
    purpose: str
    status: Literal["required", "recommended", "optional"] = "required"
    obtained: bool = False


class EvidenceChecklistResult(ToolResult):
    """证据清单生成结果。"""

    case_type: str
    required_evidence: list[EvidenceItem] = Field(default_factory=list)
    missing_evidence: list[EvidenceItem] = Field(default_factory=list)


class TimelineItem(BaseModel):
    """时间线上的单个事件节点。"""

    date: str
    description: str
    involved_parties: list[str] = Field(default_factory=list)
    is_key_date: bool = False
    note: str | None = None


class TimelineResult(ToolResult):
    """案件时间线构建结果。"""

    events: list[TimelineItem] = Field(default_factory=list)
    deadline_warnings: list[str] = Field(default_factory=list)
    earliest_event: str | None = None
    latest_event: str | None = None


# ---------------------------------------------------------------------------
# 公开工具
# ---------------------------------------------------------------------------
def calculate_legal_deadline(
    event_date: str,
    deadline_type: str,
    jurisdiction: str = "中国大陆",
) -> DeadlineResult:
    """计算诉讼时效或法定期限的到期日。

    Args:
        event_date: 起算事件日期（"YYYY-MM-DD"）。
        deadline_type: 期限类型，参见 ``_DEADLINE_RULES`` 的 key。
        jurisdiction: 管辖区域，默认「中国大陆」。

    Returns:
        DeadlineResult：含 deadline_days / deadline_date / expires_soon / warning。
    """
    if not event_date or not deadline_type:
        return DeadlineResult(
            tool_name="calculate_legal_deadline",
            success=False,
            error="event_date 与 deadline_type 均不能为空",
            event_date=event_date or "",
            deadline_type=deadline_type or "",
        )

    try:
        start = date.fromisoformat(event_date)
    except ValueError:
        return DeadlineResult(
            tool_name="calculate_legal_deadline",
            success=False,
            error=f"event_date 格式错误，应为 YYYY-MM-DD：{event_date}",
            event_date=event_date,
            deadline_type=deadline_type,
        )

    days = _DEADLINE_RULES.get(deadline_type)
    if days is None:
        return DeadlineResult(
            tool_name="calculate_legal_deadline",
            success=False,
            error=f"不支持的 deadline_type：{deadline_type}，"
                  f"可选：{', '.join(sorted(_DEADLINE_RULES.keys()))}",
            event_date=event_date,
            deadline_type=deadline_type,
        )

    deadline = start + timedelta(days=days)
    today = date.today()
    remaining = (deadline - today).days
    expires_soon = 0 <= remaining <= 7

    label = _DEADLINE_LABELS.get(deadline_type, deadline_type)
    if remaining < 0:
        warning = f"⚠️ {label} 已于 {deadline.isoformat()} 经过（已超期 {-remaining} 天），可能丧失胜诉权。"
    elif expires_soon:
        warning = f"⚠️ {label} 将于 {deadline.isoformat()} 到期（仅剩 {remaining} 天），请尽快主张权利。"
    else:
        warning = f"{label} 截止日 {deadline.isoformat()}（剩余 {remaining} 天）。"

    return DeadlineResult(
        tool_name="calculate_legal_deadline",
        success=True,
        event_date=event_date,
        deadline_type=deadline_type,
        deadline_days=days,
        deadline_date=deadline.isoformat(),
        expires_soon=expires_soon,
        warning=warning,
    )


def calculate_claim_amount(
    claim_type: str,
    principal: float,
    months: int = 0,
    wage: float | None = None,
) -> ClaimAmountResult:
    """计算常见赔偿金额。

    Args:
        claim_type: 赔偿类型，见下方规则实现。
        principal: 主张本金（如商品价款、欠薪本金）。
        months: 工作年限（经济补偿 N 中的 N），用于经济补偿类计算。
        wage: 月工资，用于经济补偿类计算。

    Returns:
        ClaimAmountResult：含 calculated_amount / formula / breakdown / notes。
    """
    if not claim_type:
        return ClaimAmountResult(
            tool_name="calculate_claim_amount",
            success=False,
            error="claim_type 不能为空",
            claim_type=claim_type or "",
            principal=principal,
        )

    try:
        principal_f = float(principal)
    except (TypeError, ValueError):
        return ClaimAmountResult(
            tool_name="calculate_claim_amount",
            success=False,
            error=f"principal 必须为数值：{principal}",
            claim_type=claim_type,
            principal=0.0,
        )

    rules = {
        "economic_compensation": _calc_economic_compensation,
        "double_compensation": _calc_double_compensation,
        "double_wage": _calc_double_wage,
        "overtime_weekday": _calc_overtime_weekday,
        "overtime_weekend": _calc_overtime_weekend,
        "overtime_holiday": _calc_overtime_holiday,
        "consumer_triple": _calc_consumer_triple,
        "consumer_tenfold": _calc_consumer_tenfold,
        "liquidated_damages": _calc_liquidated_damages,
    }

    calc_fn = rules.get(claim_type)
    if calc_fn is None:
        return ClaimAmountResult(
            tool_name="calculate_claim_amount",
            success=False,
            error=f"不支持的 claim_type：{claim_type}，"
                  f"可选：{', '.join(sorted(rules.keys()))}",
            claim_type=claim_type,
            principal=principal_f,
        )

    try:
        amount, breakdown, formula, notes = calc_fn(principal_f, months, wage)
    except Exception as exc:  # noqa: BLE001
        return ClaimAmountResult(
            tool_name="calculate_claim_amount",
            success=False,
            error=f"计算失败：{exc}",
            claim_type=claim_type,
            principal=principal_f,
        )

    return ClaimAmountResult(
        tool_name="calculate_claim_amount",
        success=True,
        claim_type=claim_type,
        principal=principal_f,
        calculated_amount=round(amount, 2),
        formula=formula,
        breakdown={k: round(v, 2) for k, v in breakdown.items()},
        notes=notes,
    )


def generate_evidence_checklist(
    case_type: str,
    facts: list[dict] | None = None,
) -> EvidenceChecklistResult:
    """按案类型生成证据清单。

    Args:
        case_type: 案类型，如「劳动争议」「民间借贷」。
        facts: 已知事实列表（每项为 dict，可含 ``obtained_evidence`` 字段标记
            已持有的证据名称列表），用于在清单中标记 obtained。

    Returns:
        EvidenceChecklistResult：含 required_evidence / missing_evidence。
    """
    if not case_type:
        return EvidenceChecklistResult(
            tool_name="generate_evidence_checklist",
            success=False,
            error="case_type 不能为空",
            case_type=case_type or "",
        )

    template = _EVIDENCE_CHECKLIST.get(case_type)
    if template is None:
        return EvidenceChecklistResult(
            tool_name="generate_evidence_checklist",
            success=False,
            error=f"不支持的 case_type：{case_type}，"
                  f"可选：{', '.join(sorted(_EVIDENCE_CHECKLIST.keys()))}",
            case_type=case_type,
        )

    obtained_set: set[str] = set()
    if facts:
        for fact in facts:
            if isinstance(fact, dict):
                obtained = fact.get("obtained_evidence") or fact.get("evidence")
                if isinstance(obtained, list):
                    for name in obtained:
                        if isinstance(name, str):
                            obtained_set.add(name)

    items: list[EvidenceItem] = []
    missing: list[EvidenceItem] = []
    for name, purpose, status in template:
        obtained = _fuzzy_obtained(name, obtained_set)
        item = EvidenceItem(
            name=name,
            purpose=purpose,
            status=status,  # type: ignore[arg-type]
            obtained=obtained,
        )
        items.append(item)
        if status == "required" and not obtained:
            missing.append(item)

    return EvidenceChecklistResult(
        tool_name="generate_evidence_checklist",
        success=True,
        case_type=case_type,
        required_evidence=items,
        missing_evidence=missing,
    )


def build_case_timeline(events: list[dict]) -> TimelineResult:
    """构建案件时间线并标注诉讼时效关键节点。

    Args:
        events: 事件列表，每个事件为 dict，需含 ``date``（YYYY-MM-DD）、
            ``description``，可选 ``involved_parties``（list[str]）、
            ``is_key_date``（bool）、``note``（str）。

    Returns:
        TimelineResult：events 按日期升序，含 deadline_warnings。
    """
    if not events:
        return TimelineResult(
            tool_name="build_case_timeline",
            success=True,
            events=[],
            deadline_warnings=[],
        )

    items: list[TimelineItem] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        d = raw.get("date") or ""
        desc = raw.get("description") or ""
        parties = raw.get("involved_parties") or []
        if not isinstance(parties, list):
            parties = []
        is_key = bool(raw.get("is_key_date", False))
        note = raw.get("note")
        if d and desc:
            items.append(
                TimelineItem(
                    date=str(d),
                    description=str(desc),
                    involved_parties=[str(p) for p in parties],
                    is_key_date=is_key,
                    note=note if isinstance(note, str) else None,
                )
            )

    # 排序：优先按 date ISO 解析；解析失败的按字符串排序，置于末尾
    def _sort_key(item: TimelineItem) -> tuple[int, Any]:
        try:
            return (0, date.fromisoformat(item.date))
        except ValueError:
            return (1, item.date)

    items.sort(key=_sort_key)

    # 标注诉讼时效关键节点：以最早关键事件为起算点，标注 1/3 年时效到期日
    warnings: list[str] = []
    today = date.today()
    for item in items:
        try:
            ev_date = date.fromisoformat(item.date)
        except ValueError:
            continue
        # 仅对关键事件计算时效提醒
        if item.is_key_date:
            for days, label in [
                (365, "劳动仲裁时效 1 年"),
                (1095, "民事诉讼时效 3 年"),
            ]:
                deadline = ev_date + timedelta(days=days)
                remaining = (deadline - today).days
                if remaining < 0:
                    warnings.append(
                        f"{item.date}（{item.description}）：{label} 已于 {deadline.isoformat()} 经过"
                    )
                elif remaining <= 30:
                    warnings.append(
                        f"{item.date}（{item.description}）：{label} 将于 {deadline.isoformat()} 到期（剩 {remaining} 天）"
                    )

    earliest = items[0].date if items else None
    latest = items[-1].date if items else None

    return TimelineResult(
        tool_name="build_case_timeline",
        success=True,
        events=items,
        deadline_warnings=warnings,
        earliest_event=earliest,
        latest_event=latest,
    )


# ---------------------------------------------------------------------------
# 内部辅助：赔偿计算规则
# ---------------------------------------------------------------------------
def _calc_economic_compensation(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """经济补偿金 N 月工资（N = 工作年限，满 6 个月按 1 年算，不满 6 个月按 0.5 年算）。

    principal 在本类型中不用，wage 必填。
    """
    if wage is None or wage <= 0:
        raise ValueError("经济补偿需要 wage（月工资）")
    n = max(months, 0)
    amount = n * wage
    breakdown = {"months": float(n), "wage": float(wage), "amount": amount}
    formula = f"N × 月工资 = {n} × {wage} = {amount}"
    notes = "N = 工作年限；满 1 年算 1 个月，满 6 个月不满 1 年算 1 个月，不满 6 个月算半个月。"
    return amount, breakdown, formula, notes


def _calc_double_compensation(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """违法解除劳动合同赔偿金 = 经济补偿金 × 2。"""
    if wage is None or wage <= 0:
        raise ValueError("违法解除赔偿金需要 wage（月工资）")
    n = max(months, 0)
    base = n * wage
    amount = base * 2
    breakdown = {
        "months": float(n),
        "wage": float(wage),
        "economic_compensation": base,
        "multiplier": 2.0,
        "amount": amount,
    }
    formula = f"2N × 月工资 = 2 × {n} × {wage} = {amount}"
    notes = "依据《劳动合同法》第87条，违法解除按经济补偿标准的二倍支付。"
    return amount, breakdown, formula, notes


def _calc_double_wage(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """未签书面劳动合同的双倍工资差额（最多 11 个月）。"""
    if wage is None or wage <= 0:
        raise ValueError("双倍工资需要 wage（月工资）")
    n = max(months, 0)
    capped = min(n, 11)
    amount = capped * wage
    breakdown = {"months": float(capped), "wage": float(wage), "amount": amount}
    formula = f"min(未签月数, 11) × 月工资 = {capped} × {wage} = {amount}"
    notes = "依据《劳动合同法》第82条，未签书面劳动合同超 1 个月付双倍工资，最多 11 个月。"
    return amount, breakdown, formula, notes


def _calc_overtime_weekday(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """工作日延长工作时间加班费 = 时薪 × 1.5 × 加班小时。

    principal 视为加班小时数；wage 视为月工资（用于推算时薪）。
    """
    hours = principal
    if wage is None or wage <= 0:
        raise ValueError("加班费需要 wage（月工资）用于推算时薪")
    hourly_rate = wage / 21.75 / 8  # 月计薪天数 21.75，日 8 小时
    amount = hourly_rate * 1.5 * hours
    breakdown = {
        "hourly_rate": hourly_rate,
        "multiplier": 1.5,
        "hours": float(hours),
        "amount": amount,
    }
    formula = f"时薪 × 1.5 × 加班小时 = {hourly_rate:.2f} × 1.5 × {hours} = {amount:.2f}"
    notes = "依据《劳动法》第44条，工作日延长工作时间加班费不低于工资 150%。"
    return amount, breakdown, formula, notes


def _calc_overtime_weekend(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """休息日加班费 = 时薪 × 2 × 加班小时（未补休时）。"""
    hours = principal
    if wage is None or wage <= 0:
        raise ValueError("加班费需要 wage（月工资）用于推算时薪")
    hourly_rate = wage / 21.75 / 8
    amount = hourly_rate * 2 * hours
    breakdown = {
        "hourly_rate": hourly_rate,
        "multiplier": 2.0,
        "hours": float(hours),
        "amount": amount,
    }
    formula = f"时薪 × 2 × 加班小时 = {hourly_rate:.2f} × 2 × {hours} = {amount:.2f}"
    notes = "依据《劳动法》第44条，休息日加班且未补休的付不低于工资 200%。"
    return amount, breakdown, formula, notes


def _calc_overtime_holiday(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """法定节假日加班费 = 时薪 × 3 × 加班小时。"""
    hours = principal
    if wage is None or wage <= 0:
        raise ValueError("加班费需要 wage（月工资）用于推算时薪")
    hourly_rate = wage / 21.75 / 8
    amount = hourly_rate * 3 * hours
    breakdown = {
        "hourly_rate": hourly_rate,
        "multiplier": 3.0,
        "hours": float(hours),
        "amount": amount,
    }
    formula = f"时薪 × 3 × 加班小时 = {hourly_rate:.2f} × 3 × {hours} = {amount:.2f}"
    notes = "依据《劳动法》第44条，法定节假日加班付不低于工资 300%。"
    return amount, breakdown, formula, notes


def _calc_consumer_triple(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """消费者三倍赔偿 = 商品价款 × 3。"""
    amount = principal * 3
    breakdown = {"principal": principal, "multiplier": 3.0, "amount": amount}
    formula = f"商品价款 × 3 = {principal} × 3 = {amount}"
    notes = "依据《消费者权益保护法》第55条，经营者欺诈赔偿 = 商品价款 × 3，不足 500 元按 500 元计。"
    return amount, breakdown, formula, notes


def _calc_consumer_tenfold(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """食品安全十倍赔偿 = 商品价款 × 10。"""
    amount = principal * 10
    breakdown = {"principal": principal, "multiplier": 10.0, "amount": amount}
    formula = f"商品价款 × 10 = {principal} × 10 = {amount}"
    notes = "依据《食品安全法》第148条，生产不符合食品安全标准的食品赔偿 = 价款 × 10，不足 1000 元按 1000 元计。"
    return amount, breakdown, formula, notes


def _calc_liquidated_damages(
    principal: float, months: int, wage: float | None
) -> tuple[float, dict[str, float], str, str | None]:
    """违约金（principal 即为约定违约金数额，原样返回）。"""
    amount = principal
    breakdown = {"principal": principal, "amount": amount}
    formula = f"约定违约金 = {amount}"
    notes = "违约金过高（超实际损失 30%）可申请法院调减；过低可申请增加。"
    return amount, breakdown, formula, notes


def _fuzzy_obtained(name: str, obtained_set: set[str]) -> bool:
    """模糊匹配证据名称是否在已持有集合中。

    任一方包含另一方即视为命中（如「劳动合同」与「劳动合同副本」）。
    """
    if not name or not obtained_set:
        return False
    if name in obtained_set:
        return True
    for ob in obtained_set:
        if not ob:
            continue
        if ob in name or name in ob:
            return True
    return False


__all__ = [
    "DeadlineResult",
    "ClaimAmountResult",
    "EvidenceItem",
    "EvidenceChecklistResult",
    "TimelineItem",
    "TimelineResult",
    "calculate_legal_deadline",
    "calculate_claim_amount",
    "generate_evidence_checklist",
    "build_case_timeline",
    "_DEADLINE_RULES",
    "_EVIDENCE_CHECKLIST",
]
