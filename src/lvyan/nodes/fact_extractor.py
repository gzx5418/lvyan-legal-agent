"""事实抽取节点：从用户输入与上传文档中抽取结构化事实与时间线。

PR2 升级：双层 Agent —— LLM 抽取（JSON 模式 + 规则校验）+ 规则降级。

抽取维度
--------
- 金额：正则匹配 "X万" / "X元" / "X块钱"
- 时间：正则匹配 "去年" / "X月" / "X年X月" / "前几天" / "最近" 等
- 当事人：检测 "我" / "公司" / "房东" / "租客" / "对方" / "老板" 等
- 行为：检测 "辞退" / "解除" / "违约" / "赔偿" / "受伤" / "签订" 等

并构建时间线、评估缺失事实（按案由推断应收集但未提及的关键事实）。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from lvyan.schemas import CaseState, Fact, MissingFact, TimelineEvent

__all__ = ["fact_extractor"]

_logger = logging.getLogger("lvyan.nodes.fact_extractor")

# LLM 抽取允许的 category 值
_ALLOWED_CATEGORIES = {"金额", "时间", "当事人", "行为", "其他"}


# ---------------------------------------------------------------------------
# 关键词与正则
# ---------------------------------------------------------------------------
# 金额：5万 / 5万元 / 5000元 / 5000块钱 / 1.5万
_AMOUNT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d+(?:\.\d+)?\s*万(?:元|块)?(?:钱)?)"),
    re.compile(r"(\d+(?:\.\d+)?\s*元)"),
    re.compile(r"(\d+(?:\.\d+)?\s*块钱)"),
)

# 时间模式（按特异性递减排列，先匹配更具体的，去重避免子串重复）
_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:去|今|前)年\d{1,2}月"),
    re.compile(r"(?:去|今|前)年"),
    re.compile(r"\d{4}年\d{1,2}月"),
    re.compile(r"\d{4}年"),
    re.compile(r"\d{1,2}月\d{1,2}日?"),
    re.compile(r"(?:前几天|前天|昨天|今天|明天|后天|上周|本周|下周|最近|此前|当时)"),
)

# 当事人关键词
_PARTY_KEYWORDS: tuple[str, ...] = (
    "我", "公司", "房东", "租客", "对方", "老板", "用人单位",
    "雇主", "雇员", "甲方", "乙方", "原告", "被告", "丈夫", "妻子",
)

# 行为关键词
_ACTION_KEYWORDS: tuple[str, ...] = (
    "辞退", "解除", "违约", "赔偿", "受伤", "签订",
    "入职", "离职", "欠款", "拖欠", "解雇", "开除", "解约",
    "起诉", "仲裁", "调解",
)

# 各案由的应收集事实清单：(fact_key, question, reason, is_blocking)
# is_blocking 全部为 False：不阻断主流程，让 composer 输出基于现有信息的法律分析，
# 同时在末尾提示用户补充关键事实以获得更精确的结论。
_REQUIRED_FACTS_BY_CASE_TYPE: dict[str, list[tuple[str, str, str, bool]]] = {
    "劳动争议": [
        (
            "labor_contract",
            "是否与用人单位签订过书面劳动合同？",
            "劳动合同是认定劳动关系与经济补偿计算的关键证据",
            False,
        ),
        (
            "employment_duration",
            "您的在职时长（入职与离职时间）？",
            "在职时长影响经济补偿/赔偿金数额计算",
            False,
        ),
        (
            "salary_payment",
            "工资以何种方式发放（银行转账/现金）及月均工资数额？",
            "工资发放方式与数额是计算补偿基数的基础",
            False,
        ),
    ],
    "合同纠纷": [
        (
            "contract_signed_form",
            "合同是书面签订还是口头约定？",
            "合同形式影响合同成立与举证方式",
            False,
        ),
        (
            "contract_performed_part",
            "您已履行了哪些合同义务？",
            "已履行部分决定违约救济范围",
            False,
        ),
        (
            "breach_content",
            "对方违约的具体内容是什么？",
            "违约内容是判断违约责任成立与否的核心",
            False,
        ),
    ],
    "侵权纠纷": [
        (
            "damage_consequence",
            "造成了哪些损害后果（人身/财产/精神）？",
            "损害后果是侵权赔偿数额计算的基础",
            False,
        ),
        (
            "reported_or_medical",
            "是否报警或就医？是否有记录？",
            "报警/就医记录是关键证据",
            False,
        ),
    ],
    "婚姻家庭": [
        (
            "has_children",
            "是否有未成年子女？子女年龄？",
            "子女抚养是离婚案件的核心争议之一",
            False,
        ),
        (
            "property_status",
            "夫妻共同财产情况（房产/存款/股权等）？",
            "财产分割需要明确范围",
            False,
        ),
    ],
    "知识产权": [
        (
            "ip_registered",
            "权利是否已注册/登记（专利/商标）？",
            "注册情况影响权利基础与侵权认定",
            False,
        ),
        (
            "infringement_act",
            "对方具体侵权行为是什么？",
            "侵权行为是判定侵权责任的前提",
            False,
        ),
    ],
}

# fact_key → 已提及检测关键词（用于判断事实是否已出现在已有 facts 中）
_FACT_KEY_HINTS: dict[str, tuple[str, ...]] = {
    "labor_contract": ("劳动合同", "签合同", "合同"),
    "employment_duration": ("入职", "在职", "工作多久", "工作年限"),
    "salary_payment": ("工资", "银行", "转账", "现金"),
    "contract_signed_form": ("书面", "口头", "签订"),
    "contract_performed_part": ("履行", "已交", "已付"),
    "breach_content": ("违约", "未履行", "不履行"),
    "damage_consequence": ("损害", "受伤", "损失"),
    "reported_or_medical": ("报警", "就医", "医院", "出警"),
    "has_children": ("子女", "孩子", "未成年"),
    "property_status": ("房产", "存款", "财产", "股权"),
    "ip_registered": ("注册", "登记", "证书"),
    "infringement_act": ("侵权", "仿冒", "盗用"),
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


def _short_id() -> str:
    """生成 8 位短 id（uuid4 hex 前缀）。"""
    return uuid.uuid4().hex[:8]


def _extract_amounts(text: str) -> list[Fact]:
    """抽取金额事实。"""
    facts: list[Fact] = []
    seen: set[str] = set()
    for pattern in _AMOUNT_PATTERNS:
        for m in pattern.finditer(text):
            content = m.group(1).strip()
            if content in seen:
                continue
            seen.add(content)
            facts.append(
                Fact(
                    fact_id=_short_id(),
                    category="金额",
                    content=content,
                    source="extracted",
                    confidence=0.8,
                )
            )
    return facts


def _extract_times(text: str) -> list[tuple[str, int]]:
    """抽取时间，返回 (时间描述, 起始位置) 列表。

    按模式特异性递减匹配，重叠匹配去重（后匹配的若与已匹配范围重叠则跳过）。
    """
    results: list[tuple[str, int]] = []
    matched_ranges: list[tuple[int, int]] = []

    for pattern in _TIME_PATTERNS:
        for m in pattern.finditer(text):
            # 检查是否与已匹配范围重叠
            overlaps = any(
                not (m.end() <= start or m.start() >= end)
                for start, end in matched_ranges
            )
            if overlaps:
                continue
            matched_ranges.append((m.start(), m.end()))
            results.append((m.group(0), m.start()))

    return results


def _extract_parties(text: str) -> list[Fact]:
    """抽取当事人事实。"""
    facts: list[Fact] = []
    seen: set[str] = set()
    for kw in _PARTY_KEYWORDS:
        if kw in text and kw not in seen:
            seen.add(kw)
            facts.append(
                Fact(
                    fact_id=_short_id(),
                    category="当事人",
                    content=kw,
                    source="extracted",
                    confidence=0.6,
                )
            )
    return facts


def _extract_actions(text: str) -> list[Fact]:
    """抽取行为事实。"""
    facts: list[Fact] = []
    seen: set[str] = set()
    for kw in _ACTION_KEYWORDS:
        if kw in text and kw not in seen:
            seen.add(kw)
            facts.append(
                Fact(
                    fact_id=_short_id(),
                    category="行为",
                    content=kw,
                    source="extracted",
                    confidence=0.7,
                )
            )
    return facts


def _build_timeline(text: str) -> list[TimelineEvent]:
    """构建时间线：将每个时间点与其后的行为/描述配对为事件。"""
    events: list[TimelineEvent] = []
    for time_str, start_pos in _extract_times(text):
        # 取时间之后到下一个标点之间的文本作为描述片段
        after = text[start_pos + len(time_str):]
        segment_parts: list[str] = []
        for ch in after:
            if ch in "，。、,.;；！!？?":
                break
            segment_parts.append(ch)
        segment = "".join(segment_parts).strip()

        # 尝试在描述片段中找行为动词
        description: str
        matched_action = None
        for action in _ACTION_KEYWORDS:
            if action in segment:
                matched_action = action
                break
        if matched_action:
            description = f"{time_str}：{matched_action}"
        elif segment:
            description = f"{time_str}：{segment}"
        else:
            description = time_str

        events.append(
            TimelineEvent(
                event_id=_short_id(),
                date=time_str,
                description=description,
                involved_parties=[],
            )
        )

    return events


def _fact_already_mentioned(fact_key: str, text: str) -> bool:
    """检查事实是否已在已有 facts 文本中提及。"""
    hints = _FACT_KEY_HINTS.get(fact_key, ())
    return any(h in text for h in hints)


def _assess_missing_facts(
    case_type: str | None, facts: list[Fact]
) -> list[MissingFact]:
    """根据案由评估缺失事实。

    对每个案由预设的关键事实清单，检查已有 facts 是否已提及；
    未提及的生成 MissingFact，关键事实标 is_blocking=True。
    """
    if not case_type or case_type not in _REQUIRED_FACTS_BY_CASE_TYPE:
        return []

    required = _REQUIRED_FACTS_BY_CASE_TYPE[case_type]
    # 把已有 facts 的 content 拼接成文本用于命中检查（兼容 Fact 对象与 dict）
    existing_text = " ".join(
        str(_get(f, "content", "")) for f in (facts or [])
    )

    missing: list[MissingFact] = []
    for fact_key, question, reason, is_blocking in required:
        if _fact_already_mentioned(fact_key, existing_text):
            continue
        missing.append(
            MissingFact(
                fact_key=fact_key,
                question=question,
                reason=reason,
                is_blocking=is_blocking,
            )
        )
    return missing


# ---------------------------------------------------------------------------
# LLM 增强抽取（PR2）
# ---------------------------------------------------------------------------
def _try_llm_extract_facts(
    user_goal: str, case_type: str | None
) -> tuple[list[Fact], list[TimelineEvent]] | None:
    """尝试用 LLM 抽取结构化事实与时间线。

    Returns:
        ``(facts, timeline)`` 或 ``None``（LLM 不可用/输出无效时）。
    """
    from lvyan.llm import chat_json, llm_available

    if not llm_available() or not user_goal.strip():
        return None

    case_hint = f"案由：{case_type}" if case_type else "案由待定"
    system_prompt = (
        "你是法律事实抽取助手。从用户描述中抽取结构化事实与时间线。"
        "只输出 JSON，不要解释。"
    )
    user_prompt = (
        f"{case_hint}\n用户描述：{user_goal}\n\n"
        "请抽取事实并输出 JSON，格式：\n"
        '{"facts": [{"category": "金额|时间|当事人|行为|其他", '
        '"content": "具体内容", "confidence": 0.0-1.0}], '
        '"timeline": [{"date": "时间描述", "description": "事件描述"}]}\n\n'
        "要求：\n"
        "1. category 必须是上述五种之一\n"
        "2. content 简洁准确，不超过 50 字\n"
        "3. confidence 反映抽取确信度\n"
        "4. 不要编造未提及的事实\n"
        "5. 时间线按时间顺序排列"
    )

    result = chat_json(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=1200,
    )
    if result is None:
        return None

    # 规则校验 + 转换
    raw_facts = result.get("facts", [])
    if not isinstance(raw_facts, list):
        return None

    facts: list[Fact] = []
    seen_contents: set[str] = set()
    for rf in raw_facts:
        if not isinstance(rf, dict):
            continue
        category = str(rf.get("category", "其他")).strip()
        content = str(rf.get("content", "")).strip()
        if not content or content in seen_contents:
            continue
        # category 校验：不在允许集合中则归为"其他"
        if category not in _ALLOWED_CATEGORIES:
            category = "其他"
        try:
            confidence = float(rf.get("confidence", 0.7))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.7
        seen_contents.add(content)
        facts.append(
            Fact(
                fact_id=_short_id(),
                category=category,
                content=content,
                source="llm",
                confidence=confidence,
            )
        )

    # 时间线转换
    timeline: list[TimelineEvent] = []
    raw_timeline = result.get("timeline", [])
    if isinstance(raw_timeline, list):
        for rt in raw_timeline:
            if not isinstance(rt, dict):
                continue
            date_str = str(rt.get("date", "")).strip()
            desc = str(rt.get("description", "")).strip()
            if not date_str and not desc:
                continue
            timeline.append(
                TimelineEvent(
                    event_id=_short_id(),
                    date=date_str,
                    description=desc or date_str,
                    involved_parties=[],
                )
            )

    if not facts and not timeline:
        return None

    _logger.info("LLM 事实抽取成功: %d facts, %d timeline", len(facts), len(timeline))
    return facts, timeline


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def fact_extractor(state: CaseState) -> dict[str, Any]:
    """事实抽取节点。

    PR2：优先用 LLM 抽取（JSON 模式 + 规则校验），失败时降级到规则+模板。

    返回更新字典（追加语义）：
        - ``facts``: 抽取出的结构化事实列表
        - ``timeline``: 时间线事件列表
        - ``missing_facts``: 按案由推断的缺失关键事实
    """
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)
    existing_facts = _get(state, "facts", []) or []

    # --- 优先 LLM 抽取 ---
    llm_result = _try_llm_extract_facts(user_goal, case_type)
    if llm_result is not None:
        facts, timeline = llm_result
    else:
        # --- 降级：规则+模板抽取 ---
        facts: list[Fact] = []
        facts.extend(_extract_amounts(user_goal))
        facts.extend(_extract_parties(user_goal))
        facts.extend(_extract_actions(user_goal))
        for time_str, _ in _extract_times(user_goal):
            facts.append(
                Fact(
                    fact_id=_short_id(),
                    category="时间",
                    content=time_str,
                    source="extracted",
                    confidence=0.6,
                )
            )
        timeline = _build_timeline(user_goal)

    # --- 缺失事实评估 ---
    all_facts = list(existing_facts) + facts
    missing_facts = _assess_missing_facts(case_type, all_facts)

    return {
        "facts": facts,
        "timeline": timeline,
        "missing_facts": missing_facts,
    }
