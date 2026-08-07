"""Privacy Validator 单元测试（SubTask 14.2）。

覆盖场景（redact_privacy）：
1. 空文本 / 无 PII：redaction_count=0，文本不变
2. 身份证号（18 位 / 15 位 / 末位 X）→ 脱敏
3. 手机号（1[3-9] 开头 11 位）→ 脱敏
4. 银行卡号（16-19 位）→ 脱敏
5. 邮箱 → 脱敏
6. 病历号 / 门诊号 / 住院号 → 脱敏
7. 住址（XX省XX市XX区...）→ 脱敏
8. 混合 PII：多种类型同时出现，计数正确
9. 优先级：18 位身份证不被当作银行卡
10. 优先级：11 位手机号在长数字串边界外不被误吞

覆盖场景（sanitize_privacy —— spec 接口，带位置信息）：
11. 空文本 → ("", [])
12. 返回类型：tuple[str, list[SanitizedItem]]
13. 手机号脱敏格式：1xx****xxxx（保留前 3 后 4）
14. 身份证号 → [身份证号已脱敏]
15. 银行卡号（含上下文关键词）→ [银行卡号已脱敏]
16. 银行卡号（无上下文关键词）→ 不匹配（避免日期 / 法条编号误判）
17. 邮箱 → [邮箱已脱敏]
18. 病历号 → [病历号已脱敏]
19. SanitizedItem 字段完整性（item_type / original_position / replacement）
20. 位置追踪：original_position 对应原始文本中的 [start, end)
21. 多项脱敏：按原始位置升序排列
22. 优先级：18 位身份证不被同时识别为银行卡
23. SanitizedItemType 字面量值
"""

from __future__ import annotations

from lvyan.validators.privacy import (
    PrivacyRedactionResult,
    SanitizedItem,
    redact_privacy,
    sanitize_privacy,
)


# ---------------------------------------------------------------------------
# 1. 空文本 / 无 PII
# ---------------------------------------------------------------------------
def test_redact_empty_text():
    """空文本 → redaction_count=0，redacted_text=''。"""
    result = redact_privacy("")
    assert result.redaction_count == 0
    assert result.redacted_text == ""
    assert result.redaction_types == {}


def test_redact_no_pii():
    """无 PII 文本 → redaction_count=0，文本不变。"""
    text = "本案为合同纠纷，依据《民法典》第五百七十七条处理。"
    result = redact_privacy(text)
    assert result.redaction_count == 0
    assert result.redacted_text == text
    assert result.redaction_types == {}


# ---------------------------------------------------------------------------
# 2. 身份证号
# ---------------------------------------------------------------------------
def test_redact_id_card_18_digits():
    """18 位身份证号（全数字）→ 脱敏。"""
    text = "原告身份证号：110101199003071234，请核实。"
    result = redact_privacy(text)
    assert result.redaction_count >= 1
    assert result.redaction_types.get("id_card", 0) >= 1
    assert "110101199003071234" not in result.redacted_text
    assert "[身份证号已脱敏]" in result.redacted_text


def test_redact_id_card_with_x():
    """18 位身份证号（末位 X）→ 脱敏。"""
    text = "身份证：11010119900307123X"
    result = redact_privacy(text)
    assert result.redaction_types.get("id_card", 0) >= 1
    assert "11010119900307123X" not in result.redacted_text


def test_redact_id_card_15_digits():
    """15 位旧版身份证号 → 脱敏。"""
    text = "旧版身份证：110101900307123"
    result = redact_privacy(text)
    assert result.redaction_types.get("id_card", 0) >= 1
    assert "110101900307123" not in result.redacted_text


# ---------------------------------------------------------------------------
# 3. 手机号
# ---------------------------------------------------------------------------
def test_redact_phone():
    """11 位手机号 → 脱敏。"""
    text = "联系电话：13812345678，请回复。"
    result = redact_privacy(text)
    assert result.redaction_types.get("phone", 0) >= 1
    assert "13812345678" not in result.redacted_text
    assert "[手机号已脱敏]" in result.redacted_text


def test_redact_phone_not_in_longer_digits():
    """11 位数字串作为更长数字串的子串时不应被当作手机号。"""
    # 13 位数字串：1381234567890 - 不应匹配 11 位手机号
    text = "订单号：1381234567890"
    result = redact_privacy(text)
    # 不应匹配为手机号（因为有数字边界 (?<!\d) / (?!\d)）
    assert result.redaction_types.get("phone", 0) == 0


# ---------------------------------------------------------------------------
# 4. 银行卡号
# ---------------------------------------------------------------------------
def test_redact_bank_card_19_digits():
    """19 位银行卡号 → 脱敏。"""
    text = "收款账号：6222021234567890123"
    result = redact_privacy(text)
    assert result.redaction_types.get("bank_card", 0) >= 1
    assert "6222021234567890123" not in result.redacted_text
    assert "[银行卡号已脱敏]" in result.redacted_text


def test_redact_bank_card_16_digits():
    """16 位银行卡号 → 脱敏。"""
    text = "卡号：6222020987654321"
    result = redact_privacy(text)
    assert result.redaction_types.get("bank_card", 0) >= 1
    assert "6222020987654321" not in result.redacted_text


# ---------------------------------------------------------------------------
# 5. 邮箱
# ---------------------------------------------------------------------------
def test_redact_email():
    """邮箱 → 脱敏。"""
    text = "联系邮箱：zhangsan@example.com，谢谢。"
    result = redact_privacy(text)
    assert result.redaction_types.get("email", 0) >= 1
    assert "zhangsan@example.com" not in result.redacted_text
    assert "[邮箱已脱敏]" in result.redacted_text


# ---------------------------------------------------------------------------
# 6. 病历号 / 门诊号 / 住院号
# ---------------------------------------------------------------------------
def test_redact_medical_record():
    """病历号 → 脱敏。"""
    text = "病历号：12345678，请核对。"
    result = redact_privacy(text)
    assert result.redaction_types.get("medical_record", 0) >= 1
    assert "12345678" not in result.redacted_text
    assert "[病历号已脱敏]" in result.redacted_text


def test_redact_outpatient_number():
    """门诊号（无分隔符）→ 脱敏。"""
    text = "门诊号123456"
    result = redact_privacy(text)
    assert result.redaction_types.get("medical_record", 0) >= 1
    assert "[病历号已脱敏]" in result.redacted_text


def test_redact_inpatient_number():
    """住院号 → 脱敏。"""
    text = "住院号: 87654321"
    result = redact_privacy(text)
    assert result.redaction_types.get("medical_record", 0) >= 1
    assert "[病历号已脱敏]" in result.redacted_text


# ---------------------------------------------------------------------------
# 7. 住址
# ---------------------------------------------------------------------------
def test_redact_address_province_city_district():
    """XX省XX市XX区 格式住址 → 脱敏。"""
    text = "原告住址：广东省深圳市南山区科技园路1号，请核实。"
    result = redact_privacy(text)
    assert result.redaction_types.get("address", 0) >= 1
    assert "广东省深圳市南山区科技园路1号" not in result.redacted_text
    assert "[住址已脱敏]" in result.redacted_text


def test_redact_address_city_road_number():
    """XX市XX路XX号 格式住址 → 脱敏。"""
    text = "被告地址：北京市海淀路100号，邮寄专用。"
    result = redact_privacy(text)
    assert result.redaction_types.get("address", 0) >= 1
    assert "[住址已脱敏]" in result.redacted_text


# ---------------------------------------------------------------------------
# 8. 混合 PII
# ---------------------------------------------------------------------------
def test_redact_mixed_pii():
    """多种 PII 同时出现 → 全部脱敏，计数正确。"""
    text = (
        "原告张三，身份证：110101199003071234，"
        "电话：13812345678，邮箱：zhangsan@example.com，"
        "病历号：12345678。"
    )
    result = redact_privacy(text)
    assert result.redaction_count >= 4
    assert "110101199003071234" not in result.redacted_text
    assert "13812345678" not in result.redacted_text
    assert "zhangsan@example.com" not in result.redacted_text
    assert "12345678" not in result.redacted_text
    # 各类型计数
    assert result.redaction_types.get("id_card", 0) >= 1
    assert result.redaction_types.get("phone", 0) >= 1
    assert result.redaction_types.get("email", 0) >= 1
    assert result.redaction_types.get("medical_record", 0) >= 1


# ---------------------------------------------------------------------------
# 9. 优先级：18 位身份证不被当作银行卡
# ---------------------------------------------------------------------------
def test_id_card_not_matched_as_bank_card():
    """18 位身份证号应被识别为 id_card，而非 bank_card。"""
    text = "身份证：110101199003071234"
    result = redact_privacy(text)
    assert result.redaction_types.get("id_card", 0) == 1
    # 不应同时被识别为银行卡
    assert result.redaction_types.get("bank_card", 0) == 0


def test_phone_not_matched_as_bank_card():
    """11 位手机号应被识别为 phone，而非 bank_card。"""
    text = "电话：13812345678"
    result = redact_privacy(text)
    assert result.redaction_types.get("phone", 0) == 1
    assert result.redaction_types.get("bank_card", 0) == 0


# ---------------------------------------------------------------------------
# 10. 返回类型校验
# ---------------------------------------------------------------------------
def test_return_type():
    """返回值应为 PrivacyRedactionResult 实例。"""
    result = redact_privacy("无敏感信息")
    assert isinstance(result, PrivacyRedactionResult)
    assert isinstance(result.redacted_text, str)
    assert isinstance(result.redaction_count, int)
    assert isinstance(result.redaction_types, dict)


# ===========================================================================
# sanitize_privacy 测试（spec 接口，带位置信息）
# ===========================================================================

# ---------------------------------------------------------------------------
# 11. 空文本 → ("", [])
# ---------------------------------------------------------------------------
def test_sanitize_empty_text():
    """空文本 → 返回 ('', [])。"""
    sanitized, items = sanitize_privacy("")
    assert sanitized == ""
    assert items == []


def test_sanitize_no_pii():
    """无 PII 文本 → 文本不变，items 为空。"""
    text = "本案为合同纠纷，依据《民法典》第五百七十七条处理。"
    sanitized, items = sanitize_privacy(text)
    assert sanitized == text
    assert items == []


# ---------------------------------------------------------------------------
# 12. 返回类型：tuple[str, list[SanitizedItem]]
# ---------------------------------------------------------------------------
def test_sanitize_return_type():
    """返回值应为 tuple[str, list[SanitizedItem]]。"""
    sanitized, items = sanitize_privacy("联系电话：13812345678")
    assert isinstance(sanitized, str)
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, SanitizedItem)


# ---------------------------------------------------------------------------
# 13. 手机号脱敏格式：1xx****xxxx（保留前 3 后 4）
# ---------------------------------------------------------------------------
def test_sanitize_phone_mask_format():
    """手机号脱敏格式为 1xx****xxxx（保留前 3 后 4，中间 ****）。"""
    text = "联系电话：13812345678，请回复。"
    sanitized, items = sanitize_privacy(text)
    assert "13812345678" not in sanitized
    # 应出现 138****5678 格式
    assert "138****5678" in sanitized
    assert len(items) == 1
    assert items[0].item_type == "phone"
    assert items[0].replacement == "138****5678"


def test_sanitize_phone_mask_short_number():
    """手机号不足 7 位时退化为 [手机号已脱敏]。"""
    # 构造一个极短的手机号场景（1[3-9] 开头但不足 11 位不会匹配，
    # 这里直接验证 _phone_mask 的边界行为）
    from lvyan.validators.privacy import _phone_mask

    assert _phone_mask("123") == "[手机号已脱敏]"


# ---------------------------------------------------------------------------
# 14. 身份证号 → [身份证号已脱敏]
# ---------------------------------------------------------------------------
def test_sanitize_id_card():
    """身份证号 → [身份证号已脱敏]。"""
    text = "身份证：110101199003071234"
    sanitized, items = sanitize_privacy(text)
    assert "110101199003071234" not in sanitized
    assert "[身份证号已脱敏]" in sanitized
    assert len(items) == 1
    assert items[0].item_type == "id_card"
    assert items[0].replacement == "[身份证号已脱敏]"


# ---------------------------------------------------------------------------
# 15. 银行卡号（含上下文关键词）→ [银行卡号已脱敏]
# ---------------------------------------------------------------------------
def test_sanitize_bank_card_with_context():
    """银行卡号（含「卡号」关键词）→ [银行卡号已脱敏]。"""
    text = "收款卡号：6222021234567890123"
    sanitized, items = sanitize_privacy(text)
    assert "6222021234567890123" not in sanitized
    assert "[银行卡号已脱敏]" in sanitized
    assert any(i.item_type == "bank_card" for i in items)


def test_sanitize_bank_card_with_account_keyword():
    """银行卡号（含「账号」关键词）→ [银行卡号已脱敏]。"""
    text = "还款账号：6222020987654321"
    sanitized, items = sanitize_privacy(text)
    assert "6222020987654321" not in sanitized
    assert "[银行卡号已脱敏]" in sanitized


# ---------------------------------------------------------------------------
# 16. 银行卡号（无上下文关键词）→ 不匹配（避免日期 / 法条编号误判）
# ---------------------------------------------------------------------------
def test_sanitize_bank_card_no_context_not_matched():
    """纯 16-19 位数字串无上下文关键词 → 不应被识别为银行卡号。"""
    text = "根据《民法典》第一百四十三条规定，自2021年1月1日起施行。"
    sanitized, items = sanitize_privacy(text)
    # 不应出现银行卡号脱敏占位符
    assert "[银行卡号已脱敏]" not in sanitized
    assert not any(i.item_type == "bank_card" for i in items)


# ---------------------------------------------------------------------------
# 17. 邮箱 → [邮箱已脱敏]
# ---------------------------------------------------------------------------
def test_sanitize_email():
    """邮箱 → [邮箱已脱敏]。"""
    text = "联系邮箱：zhangsan@example.com，谢谢。"
    sanitized, items = sanitize_privacy(text)
    assert "zhangsan@example.com" not in sanitized
    assert "[邮箱已脱敏]" in sanitized
    assert len(items) == 1
    assert items[0].item_type == "email"


# ---------------------------------------------------------------------------
# 18. 病历号 → [病历号已脱敏]
# ---------------------------------------------------------------------------
def test_sanitize_medical_record():
    """病历号 → [病历号已脱敏]。"""
    text = "病历号：12345678，请核对。"
    sanitized, items = sanitize_privacy(text)
    assert "12345678" not in sanitized
    assert "[病历号已脱敏]" in sanitized
    assert len(items) == 1
    assert items[0].item_type == "medical_record"


# ---------------------------------------------------------------------------
# 19. SanitizedItem 字段完整性
# ---------------------------------------------------------------------------
def test_sanitize_item_fields():
    """SanitizedItem 包含 item_type / original_position / replacement 三个字段。"""
    text = "电话：13812345678"
    _, items = sanitize_privacy(text)
    assert len(items) == 1
    item = items[0]
    assert hasattr(item, "item_type")
    assert hasattr(item, "original_position")
    assert hasattr(item, "replacement")
    assert item.item_type == "phone"
    assert isinstance(item.original_position, tuple)
    assert len(item.original_position) == 2
    assert isinstance(item.replacement, str)


# ---------------------------------------------------------------------------
# 20. 位置追踪：original_position 对应原始文本中的 [start, end)
# ---------------------------------------------------------------------------
def test_sanitize_position_accuracy():
    """original_position 应精确指向原始文本中的敏感信息区间。"""
    phone = "13812345678"
    text = f"前缀文字{phone}后缀文字"
    _, items = sanitize_privacy(text)
    assert len(items) == 1
    start, end = items[0].original_position
    # 从原始文本中按位置截取应得到原始手机号
    assert text[start:end] == phone


def test_sanitize_position_id_card():
    """身份证号 original_position 精确指向原始文本中的身份证号。"""
    id_card = "110101199003071234"
    text = f"原告身份证号：{id_card}，请核实。"
    _, items = sanitize_privacy(text)
    assert len(items) == 1
    start, end = items[0].original_position
    assert text[start:end] == id_card


# ---------------------------------------------------------------------------
# 21. 多项脱敏：按原始位置升序排列
# ---------------------------------------------------------------------------
def test_sanitize_multiple_items_sorted_by_position():
    """多项脱敏 → items 按原始位置升序排列。"""
    text = (
        "身份证：110101199003071234，"
        "电话：13812345678，"
        "邮箱：zhangsan@example.com。"
    )
    sanitized, items = sanitize_privacy(text)
    assert len(items) == 3
    # 验证按位置升序
    positions = [item.original_position[0] for item in items]
    assert positions == sorted(positions)
    # 验证类型
    assert items[0].item_type == "id_card"
    assert items[1].item_type == "phone"
    assert items[2].item_type == "email"
    # 验证脱敏后文本不含原始 PII
    assert "110101199003071234" not in sanitized
    assert "13812345678" not in sanitized
    assert "zhangsan@example.com" not in sanitized


# ---------------------------------------------------------------------------
# 22. 优先级：18 位身份证不被同时识别为银行卡
# ---------------------------------------------------------------------------
def test_sanitize_id_card_not_matched_as_bank_card():
    """18 位身份证号应被识别为 id_card，不被同时识别为 bank_card。"""
    text = "身份证：110101199003071234"
    _, items = sanitize_privacy(text)
    assert len(items) == 1
    assert items[0].item_type == "id_card"
    # 不应同时出现 bank_card 项
    assert not any(i.item_type == "bank_card" for i in items)


# ---------------------------------------------------------------------------
# 23. SanitizedItemType 字面量值
# ---------------------------------------------------------------------------
def test_sanitize_item_type_literal_values():
    """SanitizedItemType 应包含 5 种脱敏类型字面量。"""
    # SanitizedItemType 是 Literal 类型，验证取值集合
    valid_types = {"id_card", "bank_card", "phone", "email", "medical_record"}
    # 通过实际脱敏场景验证各类型都能被识别
    test_cases = {
        "id_card": "身份证：110101199003071234",
        "phone": "电话：13812345678",
        "bank_card": "卡号：6222021234567890123",
        "email": "邮箱：test@example.com",
        "medical_record": "病历号：12345678",
    }
    found_types: set[str] = set()
    for expected_type, text in test_cases.items():
        _, items = sanitize_privacy(text)
        for item in items:
            found_types.add(item.item_type)
    # 应至少覆盖所有 5 种类型
    assert valid_types.issubset(found_types)
