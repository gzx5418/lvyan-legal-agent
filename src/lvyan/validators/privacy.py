"""隐私脱敏验证器（SubTask 14.2）。

对最终输出文本进行隐私脱敏，识别并掩码以下敏感信息：

1. **身份证号**：15 位或 18 位（末位为数字 / X / x）。
2. **手机号**：11 位，以 ``1[3-9]`` 开头。
3. **银行卡号**：16-19 位连续数字（在身份证 / 手机号已替换后匹配剩余数字串）。
4. **邮箱**：标准 email 格式。
5. **病历号 / 门诊号 / 住院号**：``病历号: 123456`` / ``门诊号123456`` 等格式。
6. **住址**：保守匹配 ``XX省XX市XX区/县...`` / ``XX市XX路XX号`` 等显式地址模式。

设计要点
--------
- 替换顺序：身份证 → 手机 → 银行卡 → 邮箱 → 病历 → 住址。前序替换使用不含数字的
  占位符，确保后续数字型正则不会重复命中占位符。
- 所有正则均带 ``(?<!\d)`` / ``(?!\d)`` 数字边界（或对应字符边界），避免在更长数字串中
  产生子串误匹配。
- 住址匹配刻意保守：仅命中同时包含「省/市/区/县/路/号/街道/小区/栋/室」等关键词
  的显式地址片段，降低误报。

公开接口
--------
    redact_privacy(text) -> PrivacyRedactionResult
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "PrivacyRedactionResult",
    "redact_privacy",
    "SanitizedItem",
    "SanitizedItemType",
    "sanitize_privacy",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class PrivacyRedactionResult(BaseModel):
    """隐私脱敏结果。"""

    redacted_text: str
    redaction_count: int
    redaction_types: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 正则模式
# ---------------------------------------------------------------------------
# 身份证号：15 位纯数字 或 18 位（前 17 位数字 + 末位数字/X/x）
# 使用 (?<![\dXx]) / (?![\dXx]) 边界，避免在更长数字串中子串匹配
_ID_CARD_RE = re.compile(r"(?<![\dXx])(?:\d{17}[\dXx]|\d{15})(?![\dXx])")

# 手机号：11 位，1[3-9] 开头
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 银行卡号：16-19 位连续数字（在身份证 / 手机号已替换后匹配剩余数字串）
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

# 银行卡号（上下文关键词模式）：需在「卡号/账号/银行卡/账户」等关键词附近，
# 避免将日期、法条编号等纯数字串误判为银行卡号
_BANK_CARD_CONTEXT_RE = re.compile(
    r"(?:卡号|账号|银行卡|账户|开户行)\s*[:：]?\s*(\d{16,19})"
)

# 邮箱
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 病历号 / 门诊号 / 住院号：关键词 + 可选分隔符 + 数字
# 命中「病历号: 123456」「门诊号123456」「住院号：123456」等
_MEDICAL_RECORD_RE = re.compile(
    r"(?:病历号|门诊号|住院号|病案号|就诊号)\s*[:：]?\s*\d{4,20}"
)

# 住址：保守匹配，要求同时出现省/市/区/县等行政区划关键词与路/号/街道等细节
# 模式 1：XX省XX市XX区/县 + 后续地址片段
# 模式 2：XX市XX路XX号
# 模式 3：XX街道XX小区XX栋XX室
_ADDRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|自治区)[\u4e00-\u9fa5]{2,8}(?:市|自治州)"
        r"[\u4e00-\u9fa5]{2,8}(?:区|县|市)[\u4e00-\u9fa5０-９0-9]{0,40}?"
        r"(?=[\s。，,；;。\n]|$)"
    ),
    re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:市|县)[\u4e00-\u9fa5]{2,10}(?:路|街|道|巷|弄)"
        r"[\u4e00-\u9fa5０-９0-9]{0,30}?(?:号|弄|号院|小区|大厦|大楼)"
        r"[\u4e00-\u9fa5０-９0-9]{0,20}?(?=[\s。，,；;。\n]|$)"
    ),
    re.compile(
        r"[\u4e00-\u9fa5]{2,10}(?:街道|镇|乡)[\u4e00-\u9fa5]{2,10}(?:小区|村|社区)"
        r"[\u4e00-\u9fa5０-９0-9]{0,20}?(?:栋|幢|号楼|单元)"
        r"[\u4e00-\u9fa5０-９0-9]{0,20}?(?:室|号)?"
        r"(?=[\s。，,；;。\n]|$)"
    ),
)

# 占位符（不含数字，避免被后续数字型正则误匹配）
_PLACEHOLDERS: dict[str, str] = {
    "id_card": "[身份证号已脱敏]",
    "phone": "[手机号已脱敏]",
    "bank_card": "[银行卡号已脱敏]",
    "email": "[邮箱已脱敏]",
    "medical_record": "[病历号已脱敏]",
    "address": "[住址已脱敏]",
}


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def redact_privacy(text: str) -> PrivacyRedactionResult:
    """对文本进行隐私脱敏。

    按以下顺序依次替换（前序替换的占位符不含数字，不会被后续正则重复命中）：

    1. 身份证号 → ``[身份证号已脱敏]``
    2. 手机号 → ``[手机号已脱敏]``
    3. 银行卡号 → ``[银行卡号已脱敏]``
    4. 邮箱 → ``[邮箱已脱敏]``
    5. 病历号 / 门诊号 / 住院号 → ``[病历号已脱敏]``
    6. 住址 → ``[住址已脱敏]``

    Args:
        text: 待脱敏的文本。

    Returns:
        :class:`PrivacyRedactionResult`：含 ``redacted_text`` /
        ``redaction_count`` / ``redaction_types``。
    """
    if not text:
        return PrivacyRedactionResult(
            redacted_text="",
            redaction_count=0,
            redaction_types={},
        )

    redaction_types: dict[str, int] = {}
    result = text

    # 1. 身份证号（先于银行卡，避免 18 位身份证被当作银行卡）
    result, n = _ID_CARD_RE.subn(_PLACEHOLDERS["id_card"], result)
    if n:
        redaction_types["id_card"] = n

    # 2. 手机号（先于银行卡，避免 11 位手机号在更长数字串中被吞）
    result, n = _PHONE_RE.subn(_PLACEHOLDERS["phone"], result)
    if n:
        redaction_types["phone"] = n

    # 3. 银行卡号（此时身份证 / 手机号已替换为不含数字的占位符）
    result, n = _BANK_CARD_RE.subn(_PLACEHOLDERS["bank_card"], result)
    if n:
        redaction_types["bank_card"] = n

    # 4. 邮箱
    result, n = _EMAIL_RE.subn(_PLACEHOLDERS["email"], result)
    if n:
        redaction_types["email"] = n

    # 5. 病历号 / 门诊号 / 住院号
    result, n = _MEDICAL_RECORD_RE.subn(_PLACEHOLDERS["medical_record"], result)
    if n:
        redaction_types["medical_record"] = n

    # 6. 住址（保守匹配）
    address_count = 0
    for pattern in _ADDRESS_PATTERNS:
        result, n = pattern.subn(_PLACEHOLDERS["address"], result)
        address_count += n
    if address_count:
        redaction_types["address"] = address_count

    total = sum(redaction_types.values())
    return PrivacyRedactionResult(
        redacted_text=result,
        redaction_count=total,
        redaction_types=redaction_types,
    )


# ---------------------------------------------------------------------------
# sanitize_privacy：带位置信息的脱敏（SubTask 14.2 spec 接口）
# ---------------------------------------------------------------------------
SanitizedItemType = Literal[
    "id_card", "bank_card", "phone", "email", "medical_record"
]

# 脱敏类型优先级（同位置重叠时高优先级保留）：身份证 > 手机 > 银行卡 > 邮箱 > 病历
_SANITIZE_PRIORITY: dict[str, int] = {
    "id_card": 1,
    "phone": 2,
    "bank_card": 3,
    "email": 4,
    "medical_record": 5,
}


class SanitizedItem(BaseModel):
    """单条脱敏项：记录原始位置与替换文本。"""

    item_type: SanitizedItemType
    original_position: tuple[int, int]
    replacement: str


def _phone_mask(phone: str) -> str:
    """手机号脱敏：保留前 3 后 4，中间用 ``****`` 替换（``1xx****xxxx``）。"""
    if len(phone) < 7:
        return "[手机号已脱敏]"
    return phone[:3] + "****" + phone[-4:]


def sanitize_privacy(text: str) -> tuple[str, list[SanitizedItem]]:
    """对文本进行隐私脱敏，返回脱敏后文本与脱敏项列表。

    替换策略：
        - 身份证号 → ``[身份证号已脱敏]``
        - 手机号 → ``1xx****xxxx``（保留前 3 后 4）
        - 银行卡号 → ``[银行卡号已脱敏]``（需上下文关键词「卡号/账号/银行卡/账户」）
        - 邮箱 → ``[邮箱已脱敏]``
        - 病历号 / 门诊号 / 住院号 → ``[病历号已脱敏]``

    Args:
        text: 待脱敏的文本。

    Returns:
        ``(sanitized_text, items)``：脱敏后文本与 :class:`SanitizedItem` 列表
        （按原始位置升序排列，``original_position`` 为脱敏前的 ``[start, end)``）。
    """
    if not text:
        return "", []

    # 收集所有匹配：(start, end, item_type, replacement, priority)
    raw_matches: list[tuple[int, int, str, str, int]] = []

    # 1. 身份证号
    for m in _ID_CARD_RE.finditer(text):
        raw_matches.append(
            (m.start(), m.end(), "id_card", "[身份证号已脱敏]", 1)
        )

    # 2. 手机号（保留前 3 后 4）
    for m in _PHONE_RE.finditer(text):
        raw_matches.append(
            (m.start(), m.end(), "phone", _phone_mask(m.group(0)), 2)
        )

    # 3. 银行卡号（上下文关键词模式，避免日期 / 法条编号误判）
    for m in _BANK_CARD_CONTEXT_RE.finditer(text):
        # group(1) 是纯数字部分，定位其精确位置
        start, end = m.start(1), m.end(1)
        raw_matches.append(
            (start, end, "bank_card", "[银行卡号已脱敏]", 3)
        )

    # 4. 邮箱
    for m in _EMAIL_RE.finditer(text):
        raw_matches.append(
            (m.start(), m.end(), "email", "[邮箱已脱敏]", 4)
        )

    # 5. 病历号 / 门诊号 / 住院号
    for m in _MEDICAL_RECORD_RE.finditer(text):
        raw_matches.append(
            (m.start(), m.end(), "medical_record", "[病历号已脱敏]", 5)
        )

    # 按位置升序、同位置按优先级升序排序
    raw_matches.sort(key=lambda x: (x[0], x[4]))

    # 去重重叠区间：保留先出现的（高优先级）
    filtered: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, item_type, replacement, _priority in raw_matches:
        if start >= last_end:
            filtered.append((start, end, item_type, replacement))
            last_end = end

    # 构建脱敏后文本
    parts: list[str] = []
    items: list[SanitizedItem] = []
    pos = 0
    for start, end, item_type, replacement in filtered:
        parts.append(text[pos:start])
        parts.append(replacement)
        items.append(
            SanitizedItem(
                item_type=item_type,  # type: ignore[arg-type]
                original_position=(start, end),
                replacement=replacement,
            )
        )
        pos = end
    parts.append(text[pos:])

    return "".join(parts), items
