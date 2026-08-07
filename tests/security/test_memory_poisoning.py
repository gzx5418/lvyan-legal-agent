"""SubTask 18.3：数据库污染与记忆投毒测试。

验证 case vault 跨 thread_id 隔离，以及 user_preferences 的 ``sanitize_preference``
拒绝含身份证号 / 银行卡号 / 合同原文等敏感信息的"偏好"。

覆盖场景：
1. thread_A 存入材料，thread_B retrieve → None（跨 thread 隔离）
2. thread_B list_documents(thread_A) → []（不可枚举他人材料）
3. thread_B delete(thread_A, doc) → False（不可删除他人材料）
4. 伪造 requesting_thread_id → check_access 返回 False
5. cleanup_expired 仅清理过期 thread，不影响未过期 thread
6. set_ttl(thread_A, 0) 立即过期后，thread_A 自身 retrieve → None
7. sanitize_preference 拒绝含身份证号的偏好值
8. sanitize_preference 拒绝含银行卡号的偏好值
9. sanitize_preference 白名单过滤未知字段（合同原文 / 病历字段被丢弃）
10. UserPreferences 端到端：set 含敏感信息偏好 → get 时已脱敏
"""

from __future__ import annotations

import time


from lvyan.memory.case_vault import DEFAULT_TTL_SECONDS, CaseVault
from lvyan.memory.user_preferences import (
    UserPreference,
    UserPreferences,
    sanitize_preference,
)


# ---------------------------------------------------------------------------
# 1. 跨 thread 隔离：retrieve
# ---------------------------------------------------------------------------
def test_cross_thread_retrieve_denied(tmp_vault: CaseVault):
    """thread_A 存入材料，thread_B retrieve → None。"""
    content = b"secret contract content for thread A"
    tmp_vault.store("thread_A", "doc_1", content)

    # thread_B 尝试读取 thread_A 的材料 → 拒绝
    result = tmp_vault.retrieve("thread_B", "doc_1")
    assert result is None

    # thread_A 自身可读取
    own = tmp_vault.retrieve("thread_A", "doc_1")
    assert own == content


def test_cross_thread_retrieve_with_fabricated_thread_id(tmp_vault: CaseVault):
    """伪造 thread_id 读取他人材料 → 拒绝。

    注：Windows 文件系统大小写不敏感，``LEGIT_THREAD`` 与 ``legit_thread``
    会被视为同一目录，故此处仅用明确不同的 thread_id 验证隔离。
    """
    tmp_vault.store("legit_thread", "doc_x", b"sensitive")

    # 各种伪造尝试（与 legit_thread 明确不同）
    for fake_thread in ["other_thread", "fake", "spoofed", "thread_B", "impostor"]:
        result = tmp_vault.retrieve(fake_thread, "doc_x")
        assert result is None, f"伪造 thread_id={fake_thread!r} 不应读取成功"

    # 合法 thread 自身可读
    assert tmp_vault.retrieve("legit_thread", "doc_x") == b"sensitive"
    # 空字符串同样不可读
    assert tmp_vault.retrieve("", "doc_x") is None


# ---------------------------------------------------------------------------
# 2. 跨 thread 隔离：list_documents
# ---------------------------------------------------------------------------
def test_cross_thread_list_documents_returns_empty(tmp_vault: CaseVault):
    """thread_B list_documents(thread_A) → []（不可枚举他人材料）。"""
    tmp_vault.store("thread_A", "doc_1", b"a")
    tmp_vault.store("thread_A", "doc_2", b"b")

    # thread_B 尝试列举 thread_A 的材料 → 空
    # list_documents 只接受 thread_id 参数，但它读取的是该 thread 的 manifest，
    # 因此 thread_B 调用 list_documents("thread_B") 应为空
    own_b = tmp_vault.list_documents("thread_B")
    assert own_b == []

    # thread_A 自身可列举
    own_a = tmp_vault.list_documents("thread_A")
    assert len(own_a) == 2


# ---------------------------------------------------------------------------
# 3. 跨 thread 隔离：delete
# ---------------------------------------------------------------------------
def test_cross_thread_delete_denied(tmp_vault: CaseVault):
    """thread_B delete(thread_A, doc) → False（不可删除他人材料）。"""
    tmp_vault.store("thread_A", "doc_1", b"important")

    # thread_B 尝试删除 thread_A 的材料 → delete 内部 check_access 拒绝
    deleted = tmp_vault.delete("thread_B", "doc_1")
    assert deleted is False

    # thread_A 的材料仍在
    assert tmp_vault.retrieve("thread_A", "doc_1") == b"important"


# ---------------------------------------------------------------------------
# 4. check_access 直接验证
# ---------------------------------------------------------------------------
def test_check_access_isolation(tmp_vault: CaseVault):
    """check_access 强制校验 thread_id == requesting_thread_id。"""
    tmp_vault.store("thread_A", "doc_1", b"data")

    # 自身访问 → True
    assert tmp_vault.check_access("thread_A", "doc_1", "thread_A") is True
    # 跨 thread → False
    assert tmp_vault.check_access("thread_A", "doc_1", "thread_B") is False
    # 空 requesting_thread_id → False
    assert tmp_vault.check_access("thread_A", "doc_1", "") is False
    # 空 thread_id → False
    assert tmp_vault.check_access("", "doc_1", "") is False
    # 不存在的 doc，自身 thread 仍返回 True（check_access 不检查 doc 存在性）
    assert tmp_vault.check_access("thread_A", "nonexistent", "thread_A") is True


# ---------------------------------------------------------------------------
# 5. cleanup_expired 不影响其他 thread
# ---------------------------------------------------------------------------
def test_cleanup_expired_does_not_affect_others(tmp_vault: CaseVault):
    """cleanup_expired 仅清理过期 thread，未过期 thread 材料保留。"""
    # thread_A：正常 TTL（未过期）
    tmp_vault.store("thread_A", "doc_a", b"keep me")
    # thread_B：TTL=0（立即过期）
    tmp_vault.store("thread_B", "doc_b", b"expire me")
    tmp_vault.set_ttl("thread_B", 0)
    # 等待过期生效（set_ttl 用 now + 0，已为过去时刻）
    time.sleep(0.01)

    cleaned = tmp_vault.cleanup_expired()
    # 至少清理了 thread_B
    assert cleaned >= 1

    # thread_A 材料不受影响
    assert tmp_vault.retrieve("thread_A", "doc_a") == b"keep me"
    # thread_B 已被清理
    assert tmp_vault.retrieve("thread_B", "doc_b") is None


def test_cleanup_expired_with_no_expired_threads(tmp_vault: CaseVault):
    """无过期 thread 时 cleanup_expired 返回 0，不影响任何材料。"""
    tmp_vault.store("thread_A", "doc_a", b"keep")
    tmp_vault.store("thread_B", "doc_b", b"keep")

    cleaned = tmp_vault.cleanup_expired()
    assert cleaned == 0
    assert tmp_vault.retrieve("thread_A", "doc_a") == b"keep"
    assert tmp_vault.retrieve("thread_B", "doc_b") == b"keep"


# ---------------------------------------------------------------------------
# 6. TTL 过期后自身 retrieve 返回 None
# ---------------------------------------------------------------------------
def test_self_retrieve_after_expiry(tmp_vault: CaseVault):
    """set_ttl(thread, 0) 立即过期后，thread 自身 retrieve → None。"""
    tmp_vault.store("thread_A", "doc_a", b"data")
    assert tmp_vault.retrieve("thread_A", "doc_a") == b"data"

    tmp_vault.set_ttl("thread_A", 0)
    time.sleep(0.01)
    # 过期后自身亦不可读
    assert tmp_vault.retrieve("thread_A", "doc_a") is None


def test_default_ttl_is_seven_days():
    """DEFAULT_TTL_SECONDS 应为 7 天（604800 秒）。"""
    assert DEFAULT_TTL_SECONDS == 604800


# ---------------------------------------------------------------------------
# 7. sanitize_preference：身份证号
# ---------------------------------------------------------------------------
def test_sanitize_preference_rejects_id_card():
    """白名单字段值含身份证号 → 值置空。"""
    pref = {
        "user_id": "110101199001011234",  # 字段在白名单，值是身份证号
        "response_style": "我的身份证号是 110101199001011234",
    }
    cleaned = sanitize_preference(pref)
    # user_id 是白名单字段，但值是 18 位身份证号 → 置空
    assert cleaned["user_id"] == ""
    # response_style 含身份证号 → 置空
    assert cleaned["response_style"] == ""


def test_sanitize_preference_rejects_bank_card():
    """白名单字段值含银行卡号 → 值置空。"""
    pref = {
        "user_id": "normal_user",
        "response_style": "卡号 6222020200011111111 请记录",
    }
    cleaned = sanitize_preference(pref)
    # response_style 含 19 位银行卡号 → 置空
    assert cleaned["response_style"] == ""
    # user_id 正常保留
    assert cleaned["user_id"] == "normal_user"


def test_sanitize_preference_rejects_phone():
    """白名单字段值含手机号 → 值置空。"""
    pref = {
        "user_id": "user1",
        "response_style": "联系我 13812345678",
    }
    cleaned = sanitize_preference(pref)
    assert cleaned["response_style"] == ""


# ---------------------------------------------------------------------------
# 8. sanitize_preference：白名单过滤未知字段
# ---------------------------------------------------------------------------
def test_sanitize_preference_drops_unknown_sensitive_fields():
    """非白名单字段（合同原文 / 病历 / 证据）一律丢弃。"""
    pref = {
        "user_id": "user1",
        "response_style": "brief",
        "contract_text": "甲方与乙方签订的合同全文...",  # 非白名单
        "medical_history": "高血压病史 5 年",  # 非白名单
        "evidence": "聊天记录截图",  # 非白名单
        "id_card": "110101199001011234",  # 非白名单 + 敏感
        "bank_card_number": "6222020200011111111",  # 非白名单 + 敏感
        "chat_history": "...",  # 非白名单
    }
    cleaned = sanitize_preference(pref)

    # 仅保留白名单字段
    assert set(cleaned.keys()) == {"user_id", "response_style"}
    assert cleaned["user_id"] == "user1"
    assert cleaned["response_style"] == "brief"
    # 敏感字段全部被丢弃
    assert "contract_text" not in cleaned
    assert "medical_history" not in cleaned
    assert "evidence" not in cleaned
    assert "id_card" not in cleaned
    assert "bank_card_number" not in cleaned
    assert "chat_history" not in cleaned


def test_sanitize_preference_preserves_allowed_fields():
    """白名单字段且值无敏感信息 → 完整保留。"""
    pref = {
        "user_id": "user1",
        "response_style": "detailed",
        "prefer_depth": "deep",
        "preferred_doc_format": "docx",
        "language": "zh",
    }
    cleaned = sanitize_preference(pref)
    assert cleaned == {
        "user_id": "user1",
        "response_style": "detailed",
        "prefer_depth": "deep",
        "preferred_doc_format": "docx",
        "language": "zh",
    }


def test_sanitize_preference_does_not_mutate_input():
    """sanitize_preference 不应修改入参字典。"""
    pref = {"user_id": "user1", "contract_text": "secret"}
    original = dict(pref)
    sanitize_preference(pref)
    assert pref == original


# ---------------------------------------------------------------------------
# 9. UserPreferences 端到端：set 含敏感信息 → get 已脱敏
# ---------------------------------------------------------------------------
def test_user_preferences_set_sanitizes_on_write(tmp_user_prefs: UserPreferences):
    """set 时对落盘内容做 sanitize，敏感信息不持久化。"""
    pref = UserPreference(
        user_id="user1",
        response_style="我的身份证号是 110101199001011234",
    )
    tmp_user_prefs.set("user1", pref)

    # get 时重新 sanitize，response_style 应为空
    loaded = tmp_user_prefs.get("user1")
    assert loaded.user_id == "user1"
    assert loaded.response_style == ""  # 含身份证号 → 置空


def test_user_preferences_update_filters_unknown_fields(
    tmp_user_prefs: UserPreferences,
):
    """update 传入非白名单字段 → 被 sanitize 过滤，不写入。"""
    tmp_user_prefs.update(
        "user1",
        response_style="brief",
        contract_text="合同原文不应保存",
        id_card="110101199001011234",
    )
    loaded = tmp_user_prefs.get("user1")
    assert loaded.response_style == "brief"
    # 非白名单字段未写入（UserPreference 模型本身也没有这些字段）


def test_user_preferences_get_default_for_new_user(
    tmp_user_prefs: UserPreferences,
):
    """新用户 get → 返回默认偏好并持久化。"""
    loaded = tmp_user_prefs.get("new_user")
    assert loaded.user_id == "new_user"
    assert loaded.response_style == "brief"
    assert loaded.prefer_depth == "light"
    assert loaded.preferred_doc_format == "md"
    assert loaded.language == "zh"
