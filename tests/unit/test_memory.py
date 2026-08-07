"""Task 19：记忆系统与 case vault 单元测试。

覆盖：
1. 短期记忆保存 / 加载 / 删除
2. 长期记忆 sanitize_preference 过滤敏感信息
3. case vault 存取与 list_documents
4. case vault 跨 thread 隔离（check_access / retrieve）
5. case vault TTL 过期清理
6. case vault delete / delete_thread
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from lvyan.memory import (
    DEFAULT_TTL_SECONDS,
    CaseVault,
    ShortTermMemory,
    UserPreference,
    UserPreferences,
    sanitize_preference,
)
from lvyan.schemas import (
    Authority,
    CaseState,
    CitationAudit,
    Fact,
    PlanStep,
)
from lvyan.schemas.output import CitationDetail


# ---------------------------------------------------------------------------
# 辅助：构造包含「短期记忆要素」的 CaseState
# ---------------------------------------------------------------------------
def _make_state(thread_id: str = "thread-1") -> CaseState:
    """构造含事实 / 法条 / 管辖地 / 计划 / citation audit 的 CaseState。"""
    return CaseState(
        run_id="run-1",
        thread_id=thread_id,
        current_date=date(2026, 7, 23),
        user_goal="测试记忆系统",
        jurisdiction="中国大陆",
        case_type="合同纠纷",
        complexity="deep",
        facts=[
            Fact(
                fact_id="f1",
                category="当事人",
                content="原告张三",
                source="user",
                confidence=0.9,
            ),
            Fact(
                fact_id="f2",
                category="金额",
                content="借款 5 万元",
                source="user",
                confidence=1.0,
            ),
        ],
        statutes=[
            Authority(
                source_id="cc-667",
                title="中华人民共和国民法典",
                article_number="第六百七十九条",
                article_text="借款合同是借款人向贷款人借款，到期返还借款并支付利息的合同。",
                authority_level="法律",
                retrieved_at=datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc),
            ),
        ],
        plan=[
            PlanStep(step_id="s1", action="检索借贷利率上限", tool="retrieve_statutes"),
        ],
        citation_audit=CitationAudit(
            passed=True,
            total_citations=1,
            verified=1,
            fabricated=0,
            repealed_cited=0,
            unsupported=0,
            details=[
                CitationDetail(
                    citation_text="《民法典》第六百七十九条",
                    status="verified",
                    matched_source_id="cc-667",
                ),
            ],
        ),
        iteration=1,
    )


# ===========================================================================
# 测试 1：短期记忆保存 / 加载 / 删除
# ===========================================================================
def test_short_term_memory_save_load_delete(tmp_path):
    mem = ShortTermMemory(base_dir=tmp_path / "checkpoints")
    state = _make_state("thread-save")

    # 保存
    mem.save("thread-save", state)

    # 加载并验证关键字段完整（事实 / 法条 source_id / 管辖地 / 计划 / citation audit）
    loaded = mem.load("thread-save")
    assert loaded is not None
    assert loaded.thread_id == "thread-save"
    assert loaded.jurisdiction == "中国大陆"
    assert len(loaded.facts) == 2
    assert loaded.facts[0].content == "原告张三"
    # 已检索法条 source_id 列表
    assert [s.source_id for s in loaded.statutes] == ["cc-667"]
    # 当前计划
    assert loaded.plan[0].step_id == "s1"
    # citation audit 结果
    assert loaded.citation_audit is not None
    assert loaded.citation_audit.passed is True
    assert loaded.citation_audit.details[0].matched_source_id == "cc-667"

    # list_threads 包含该 thread
    assert "thread-save" in mem.list_threads()

    # delete 后 load 返回 None
    mem.delete("thread-save")
    assert mem.load("thread-save") is None
    assert "thread-save" not in mem.list_threads()


def test_short_term_memory_load_missing_returns_none(tmp_path):
    mem = ShortTermMemory(base_dir=tmp_path / "checkpoints")
    assert mem.load("not-exist") is None


# ===========================================================================
# 测试 2：长期记忆 sanitize_preference 过滤敏感信息
# ===========================================================================
def test_sanitize_preference_filters_sensitive_info():
    """身份证号 / 银行卡号 / 合同原文等敏感字段必须被过滤。"""
    pref = {
        "user_id": "u1",
        "response_style": "brief",
        "prefer_depth": "deep",
        "preferred_doc_format": "md",
        "language": "zh",
        # 敏感 key（黑名单 → 整字段丢弃）
        "id_card": "110101199001011234",
        "bank_card": "6222021234567890123",
        "contract_text": "甲方张三向乙方李四借款人民币5万元",
        "medical_record": "高血压病史3年",
        "chat_history": "用户：我借了5万",
        "evidence": "借条照片",
        "password": "p@ssw0rd",
        # 非白名单 key → 丢弃
        "note_nonallowed": "110101199001011234",
    }

    cleaned = sanitize_preference(pref)

    # 1) 白名单字段保留
    assert cleaned["user_id"] == "u1"
    assert cleaned["response_style"] == "brief"

    # 2) 敏感 key 不出现在结果中
    for forbidden_key in (
        "id_card",
        "bank_card",
        "contract_text",
        "medical_record",
        "chat_history",
        "evidence",
        "password",
        "note_nonallowed",
    ):
        assert forbidden_key not in cleaned, f"敏感字段 {forbidden_key} 未被过滤"

    # 3) 整个结果中不包含身份证号 / 银行卡号原文
    import json as _json

    blob = _json.dumps(cleaned, ensure_ascii=False)
    assert "110101199001011234" not in blob
    assert "6222021234567890123" not in blob


def test_sanitize_preference_blanks_sensitive_value_in_allowed_field():
    """白名单字段的值若是身份证号，应被置空而非原样保留。"""
    pref = {
        "user_id": "u1",
        "language": "110101199001011234",  # 白名单 key，但值是身份证号
    }
    cleaned = sanitize_preference(pref)
    # user_id 保留
    assert cleaned.get("user_id") == "u1"
    # language 值被置空（敏感值过滤）
    assert cleaned.get("language") == ""


def test_user_preferences_set_get_update(tmp_path):
    """UserPreferences 基本读写与部分更新。"""
    store = UserPreferences(base_dir=tmp_path / "user_prefs")

    # get 不存在 → 返回默认并落盘
    pref = store.get("u-new")
    assert pref.user_id == "u-new"
    assert pref.response_style == "brief"

    # set 整体覆盖
    custom = UserPreference(
        user_id="u-new",
        response_style="detailed",
        prefer_depth="deep",
        preferred_doc_format="docx",
        language="en",
    )
    store.set("u-new", custom)

    reloaded = store.get("u-new")
    assert reloaded.response_style == "detailed"
    assert reloaded.preferred_doc_format == "docx"
    assert reloaded.language == "en"

    # update 部分更新
    store.update("u-new", response_style="brief")
    reloaded2 = store.get("u-new")
    assert reloaded2.response_style == "brief"
    # 其余字段不受影响
    assert reloaded2.preferred_doc_format == "docx"


# ===========================================================================
# 测试 3：case vault 存取与 list_documents
# ===========================================================================
def test_case_vault_store_retrieve_list(tmp_path):
    vault = CaseVault(base_dir=tmp_path / "vault")
    content = "借款合同原文".encode("utf-8")
    meta = {"filename": "contract.pdf", "doc_type": "合同", "content_hash": "abc123"}

    path = vault.store("t1", "doc1", content, meta)

    # 返回 vault 内部路径
    assert path.endswith("doc1.enc")
    assert "t1" in path

    # retrieve 内容一致（加密占位为 base64，应可逆）
    retrieved = vault.retrieve("t1", "doc1")
    assert retrieved is not None
    assert retrieved == content

    # list_documents 列出元数据
    docs = vault.list_documents("t1")
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "doc1"
    assert docs[0]["metadata"]["filename"] == "contract.pdf"
    assert docs[0]["content_size"] == len(content)


def test_case_vault_retrieve_missing_returns_none(tmp_path):
    vault = CaseVault(base_dir=tmp_path / "vault")
    assert vault.retrieve("t1", "nope") is None


# ===========================================================================
# 测试 4：case vault 跨 thread 隔离
# ===========================================================================
def test_case_vault_cross_thread_isolation(tmp_path):
    vault = CaseVault(base_dir=tmp_path / "vault")
    vault.store("t1", "secret-doc", "敏感材料".encode("utf-8"), {"kind": "evidence"})

    # check_access：t2 请求访问 t1 的材料 → False
    assert vault.check_access("t1", "secret-doc", "t2") is False

    # t1 自己访问自己 → True
    assert vault.check_access("t1", "secret-doc", "t1") is True

    # retrieve 跨 thread → None（隔离：retrieve 内部用 requesting=thread_id 自身）
    # 这里用 t2 视角：retrieve("t2", "secret-doc")，check_access("t2","secret-doc","t2")=True，
    # 但 t2 目录下根本没有该文件 → 返回 None，达到隔离效果。
    assert vault.retrieve("t2", "secret-doc") is None

    # t2 list_documents 不应看到 t1 的材料
    assert vault.list_documents("t2") == []

    # t1 自身仍可正常读取
    assert vault.retrieve("t1", "secret-doc") == "敏感材料".encode("utf-8")


def test_case_vault_check_access_rules(tmp_path):
    """check_access 的核心隔离规则：只能访问自己 thread。"""
    vault = CaseVault(base_dir=tmp_path / "vault")
    assert vault.check_access("t1", "doc1", "t1") is True
    assert vault.check_access("t1", "doc1", "t2") is False
    assert vault.check_access("", "doc1", "") is False
    assert vault.check_access("t1", "doc1", "T1") is False  # 大小写敏感


# ===========================================================================
# 测试 5：case vault TTL 过期清理
# ===========================================================================
def test_case_vault_ttl_cleanup(tmp_path):
    vault = CaseVault(base_dir=tmp_path / "vault")
    vault.store("t1", "ephemeral", "临时材料".encode("utf-8"), {"note": "会过期"})

    # 设置 TTL=0 → 立即过期
    vault.set_ttl("t1", ttl_seconds=0)

    # 清理已过期 → 返回 >= 1
    time.sleep(0.01)  # 确保时间推进，使 now > expires_at
    cleaned = vault.cleanup_expired()
    assert cleaned >= 1

    # 再次 retrieve → None（材料已清理）
    assert vault.retrieve("t1", "ephemeral") is None
    # list_documents 也为空
    assert vault.list_documents("t1") == []


def test_case_vault_default_ttl_not_expired(tmp_path):
    """未设置 TTL 的 thread 默认 7 天，不应被立即清理。"""
    vault = CaseVault(base_dir=tmp_path / "vault")
    vault.store("t1", "doc", b"x")
    # 不调用 set_ttl，默认 7 天
    assert vault.cleanup_expired() == 0
    assert vault.retrieve("t1", "doc") == b"x"


# ===========================================================================
# 测试 6：case vault delete / delete_thread
# ===========================================================================
def test_case_vault_delete_and_delete_thread(tmp_path):
    vault = CaseVault(base_dir=tmp_path / "vault")
    vault.store("t1", "doc1", "内容1".encode("utf-8"), {"filename": "a.pdf"})

    # delete 单个文件 → retrieve 返回 None
    assert vault.delete("t1", "doc1") is True
    assert vault.retrieve("t1", "doc1") is None

    # delete 不存在的文件 → False
    assert vault.delete("t1", "doc1") is False

    # 再存两份，验证 delete_thread 清空整个 thread
    vault.store("t1", "doc2", "内容2".encode("utf-8"), {"filename": "b.pdf"})
    vault.store("t1", "doc3", "内容3".encode("utf-8"), {"filename": "c.pdf"})
    assert len(vault.list_documents("t1")) == 2

    # delete_thread → list_documents 返回空
    assert vault.delete_thread("t1") is True
    assert vault.list_documents("t1") == []

    # 删除不存在的 thread → False
    assert vault.delete_thread("never-existed") is False


# ===========================================================================
# 补充：导入可发现性（对应验证标准 1）
# ===========================================================================
def test_memory_public_api_importable():
    from lvyan.memory import (  # noqa: F401
        CaseVault,
        ShortTermMemory,
        UserPreference,
        UserPreferences,
        sanitize_preference,
    )

    assert DEFAULT_TTL_SECONDS == 604800
