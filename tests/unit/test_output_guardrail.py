"""Output Guardrail 节点单元测试（SubTask 14.4）。

覆盖场景：
1. 干净输出（无问题）→ output_retry_needed=False，无 pending_human_approval
2. 隐私脱敏：输出含手机号 → 脱敏
3. 缺风险声明 → 自动追加
4. 数字概率 → 拦截并替换
5. 无效引用 → 移除
6. 缺章节 → output_retry_needed=True
7. 达到迭代上限 → 强制放行（不 retry）
8. Human-in-the-loop：不可逆操作 → pending_human_approval（HITL 关闭时）
9. 高风险 → 追加高风险声明
10. route_after_output_guardrail 路由
11. 返回值结构
12. HITL 开启时 interrupt 被调用（mock）
13. HITL approve → status=approved
14. HITL reject → 保留分析正文，但拒绝不可逆操作
15. HITL edit → final_output 被替换
16. 最终校验未通过 → risk_level 上调为 high
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

from lvyan.config import settings
from lvyan.graph.routing import route_after_output_guardrail
from lvyan.nodes.output_guardrail import MAX_OUTPUT_ITERATIONS, output_guardrail
from lvyan.schemas import Authority


# ---------------------------------------------------------------------------
# 辅助：构造 Authority
# ---------------------------------------------------------------------------
def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
    article_text: str = (
        "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
        "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
    ),
    source_id: str | None = None,
) -> Authority:
    return Authority(
        source_id=source_id or f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=article_text,
        authority_level="法律",
        effective_date=date(2021, 1, 1),
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_state(
    final_output: str,
    complexity: str = "light",
    statutes: list[Authority] | None = None,
    risk_level: str = "low",
    output_iteration: int = 0,
) -> dict:
    """构造测试用 state dict。"""
    if statutes is None:
        statutes = [_make_authority()]
    return {
        "run_id": "run-guardrail-test",
        "thread_id": "thread-guardrail-test",
        "current_date": date(2026, 7, 23),
        "user_goal": "测试 output_guardrail",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": complexity,
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": statutes,
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": None,
        "citation_audit": None,
        "risk_level": risk_level,
        "confidence": "medium",
        "iteration": 0,
        "final_output": final_output,
        "output_iteration": output_iteration,
        "output_retry_needed": False,
        "pending_human_approval": None,
    }


# 干净的 Light 模式输出（含法条引用 + 风险声明，无 PII / 数字概率 / 不可逆操作）
_CLEAN_LIGHT = """# 日常咨询快答

## 用户目标
咨询合同违约赔偿问题。

## 核心法律结论
本案构成违约，可主张赔偿。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 收集合同与违约证据。

## 风险声明
以上内容仅供参考，不构成正式法律意见。
"""


# ---------------------------------------------------------------------------
# 1. 干净输出 → 无 retry / 无 HITL
# ---------------------------------------------------------------------------
def test_clean_output_no_issues():
    """干净输出（结构完整 / 风险声明 / 有效引用 / 无 PII / 无数字概率）→ 直接放行。"""
    state = _make_state(_CLEAN_LIGHT, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert result["output_retry_needed"] is False
    assert "pending_human_approval" not in result or result.get("pending_human_approval") is None
    # final_output 仍包含核心内容
    assert "用户目标" in result["final_output"]
    assert "风险声明" in result["final_output"]


# ---------------------------------------------------------------------------
# 2. 隐私脱敏：输出含手机号 → 脱敏
# ---------------------------------------------------------------------------
def test_privacy_redaction_phone():
    """输出含手机号 → 脱敏，手机号不出现在 final_output 中。"""
    text = _CLEAN_LIGHT.replace(
        "本案构成违约，可主张赔偿。",
        "本案构成违约，联系电话：13812345678，可主张赔偿。",
    )
    state = _make_state(text, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert "13812345678" not in result["final_output"]
    assert "[手机号已脱敏]" in result["final_output"]


# ---------------------------------------------------------------------------
# 3. 缺风险声明 → 自动追加
# ---------------------------------------------------------------------------
def test_missing_risk_disclaimer_auto_appended():
    """输出缺少风险声明 → 自动追加标准风险声明。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
- 《中华人民共和国民法典》第五百七十七条

## 行动建议
1. 起诉。
"""
    state = _make_state(text, statutes=[_make_authority()])
    result = output_guardrail(state)
    # 应自动追加风险声明
    assert "仅供参考" in result["final_output"] or "不构成" in result["final_output"]
    assert result["output_retry_needed"] is False


# ---------------------------------------------------------------------------
# 4. 数字概率 → 拦截并替换
# ---------------------------------------------------------------------------
def test_numeric_probability_intercepted():
    """输出含 85% → 拦截，替换为定性标签。"""
    text = _CLEAN_LIGHT.replace(
        "本案构成违约，可主张赔偿。",
        "本案构成违约，胜诉率85%，可主张赔偿。",
    )
    state = _make_state(text, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert "85%" not in result["final_output"]
    assert "信息不足" in result["final_output"]


# ---------------------------------------------------------------------------
# 5. 无效引用 → 移除
# ---------------------------------------------------------------------------
def test_invalid_citation_removed():
    """输出引用《民法典》第9999条但 statutes 中不存在 → 移除引用。"""
    text = _CLEAN_LIGHT.replace(
        "《中华人民共和国民法典》第五百七十七条",
        "《中华人民共和国民法典》第九千九百九十九条虚构条款",
    )
    state = _make_state(text, statutes=[_make_authority(article_number="第五百七十七条")])
    result = output_guardrail(state)
    # 9999 不应出现在最终输出中（或至少被标注警告）
    assert (
        "第九千九百九十九条" not in result["final_output"] or "校验备注" in result["final_output"]
    )


# ---------------------------------------------------------------------------
# 6. 缺章节 → output_retry_needed=True
# ---------------------------------------------------------------------------
def test_missing_section_triggers_retry():
    """输出缺「行动建议」章节 → output_retry_needed=True。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
（暂无法条）

## 风险声明
以上内容仅供参考。
"""
    state = _make_state(text, statutes=[], output_iteration=0)
    result = output_guardrail(state)
    assert result["output_retry_needed"] is True
    assert result["output_iteration"] == 1


# ---------------------------------------------------------------------------
# 7. 达到迭代上限 → 强制放行（不 retry）
# ---------------------------------------------------------------------------
def test_max_iterations_force_pass():
    """output_iteration >= MAX_OUTPUT_ITERATIONS + 缺章节 → 强制放行，不 retry。"""
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
（暂无法条）

## 风险声明
以上内容仅供参考。
"""
    state = _make_state(text, statutes=[], output_iteration=MAX_OUTPUT_ITERATIONS)
    result = output_guardrail(state)
    assert result["output_retry_needed"] is False
    # 应有强制放行备注
    assert "强制放行" in result["final_output"] or "已达" in result["final_output"]


# ---------------------------------------------------------------------------
# 8. Human-in-the-loop：不可逆操作 → pending_human_approval（HITL 关闭时）
# ---------------------------------------------------------------------------
def test_irreversible_operation_triggers_hitl(monkeypatch):
    """输出含「发送律师函」→ pending_human_approval 被设置（HITL 关闭时）。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    text = _CLEAN_LIGHT.replace(
        "1. 收集合同与违约证据。",
        "1. 发送律师函催告对方履行合同。",
    )
    state = _make_state(text, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert "pending_human_approval" in result
    assert result["pending_human_approval"] is not None
    assert "发送律师函" in str(result["pending_human_approval"])


def test_lawsuit_triggers_hitl(monkeypatch):
    """输出含「提起诉讼」→ pending_human_approval 被设置（HITL 关闭时）。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    text = _CLEAN_LIGHT.replace(
        "1. 收集合同与违约证据。",
        "1. 向法院提起诉讼追究违约责任。",
    )
    state = _make_state(text, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert result.get("pending_human_approval") is not None


# ---------------------------------------------------------------------------
# 9. 高风险 → 追加高风险声明
# ---------------------------------------------------------------------------
def test_high_risk_disclaimer_appended():
    """risk_level=high 且输出未含高风险声明 → 追加高风险声明。"""
    state = _make_state(_CLEAN_LIGHT, statutes=[_make_authority()], risk_level="high")
    result = output_guardrail(state)
    assert "高风险声明" in result["final_output"]


def test_low_risk_no_high_risk_disclaimer():
    """risk_level=low → 不追加高风险声明。"""
    state = _make_state(_CLEAN_LIGHT, statutes=[_make_authority()], risk_level="low")
    result = output_guardrail(state)
    assert "高风险声明" not in result["final_output"]


# ---------------------------------------------------------------------------
# 10. route_after_output_guardrail 路由
# ---------------------------------------------------------------------------
def test_route_retry_to_composer():
    """output_retry_needed=True → route 返回 'composer'。"""
    state = {"output_retry_needed": True}
    assert route_after_output_guardrail(state) == "composer"


def test_route_no_retry_to_finalizer():
    """output_retry_needed=False → route 返回 'legal_answer_finalizer'。"""
    state = {"output_retry_needed": False}
    assert route_after_output_guardrail(state) == "legal_answer_finalizer"


def test_route_default_to_finalizer():
    """output_retry_needed 未设置 → 默认返回 'legal_answer_finalizer'。"""
    state = {}
    assert route_after_output_guardrail(state) == "legal_answer_finalizer"


# ---------------------------------------------------------------------------
# 11. 返回值结构
# ---------------------------------------------------------------------------
def test_return_structure():
    """返回值应包含 final_output 与 output_retry_needed。"""
    state = _make_state(_CLEAN_LIGHT, statutes=[_make_authority()])
    result = output_guardrail(state)
    assert isinstance(result, dict)
    assert "final_output" in result
    assert "output_retry_needed" in result
    assert isinstance(result["final_output"], str)
    assert isinstance(result["output_retry_needed"], bool)


# ===========================================================================
# 12-15. HITL 开启时 interrupt 行为（mock interrupt）
# ===========================================================================


def _make_irreversible_state() -> dict:
    """构造含不可逆操作（发送律师函）的 state。"""
    text = _CLEAN_LIGHT.replace(
        "1. 收集合同与违约证据。",
        "1. 发送律师函催告对方履行合同。",
    )
    return _make_state(text, statutes=[_make_authority()])


# ---------------------------------------------------------------------------
# 12. HITL 开启时 interrupt 被调用（mock）
# ---------------------------------------------------------------------------
def test_hitl_interrupt_called_when_enabled(monkeypatch):
    """HITL 开启 + 不可逆操作 → interrupt 被调用（mock 返回 approve）。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_irreversible_state()
    with patch(
        "lvyan.nodes.output_guardrail.interrupt",
        return_value="approve",
    ) as mock_interrupt:
        result = output_guardrail(state)
    # interrupt 应被调用一次
    assert mock_interrupt.call_count == 1
    # 调用参数应包含 operations 与 draft_output
    call_args = mock_interrupt.call_args[0][0]
    assert "operations" in call_args
    assert "draft_output" in call_args
    # pending_human_approval 应为 approved 状态
    assert result["pending_human_approval"]["status"] == "approved"


# ---------------------------------------------------------------------------
# 13. HITL approve → status=approved
# ---------------------------------------------------------------------------
def test_hitl_approve_response(monkeypatch):
    """用户批准 → pending_human_approval.status=approved，输出保留。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_irreversible_state()
    with patch(
        "lvyan.nodes.output_guardrail.interrupt",
        return_value="approve",
    ):
        result = output_guardrail(state)
    assert result["pending_human_approval"]["status"] == "approved"
    # 输出不应为「操作已取消。」
    assert result["final_output"] != "操作已取消。"
    # 应包含不可逆操作提示
    assert "发送律师函" in result["final_output"]


# ---------------------------------------------------------------------------
# 14. HITL reject → 保留分析正文 + 追加拒绝提示（P0-2 修复后行为）
# ---------------------------------------------------------------------------
def test_hitl_reject_response(monkeypatch):
    """用户拒绝 → 原分析正文保留，仅追加拒绝提示，status=rejected。

    P0-2 修复回归：旧实现把整篇输出擦成「操作已取消。」，丢失法律分析正文。
    新实现保留正文，仅追加拒绝提示。
    """
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_irreversible_state()
    original = state["final_output"]
    with patch(
        "lvyan.nodes.output_guardrail.interrupt",
        return_value="reject",
    ):
        result = output_guardrail(state)
    # 原分析正文必须保留（不再被擦成「操作已取消。」）
    assert result["final_output"] != "操作已取消。"
    assert original in result["final_output"]
    # 含拒绝提示
    assert "拒绝" in result["final_output"]
    assert result["output_retry_needed"] is False
    assert result["pending_human_approval"]["status"] == "rejected"


def test_hitl_none_response_treated_as_reject(monkeypatch):
    """用户响应为 None（异常情况）→ fail-closed，同时保留分析正文。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_irreversible_state()
    original = state["final_output"]
    with patch(
        "lvyan.nodes.output_guardrail.interrupt",
        return_value=None,
    ):
        result = output_guardrail(state)
    # 原文保留
    assert original in result["final_output"]
    assert result["final_output"] != "操作已取消。"
    # None 不构成明确批准，必须拒绝不可逆操作
    assert "拒绝执行" in result["final_output"]
    assert result["pending_human_approval"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# 15. HITL edit → final_output 被替换
# ---------------------------------------------------------------------------
def test_hitl_edit_response(monkeypatch):
    """用户编辑输出 → final_output 被替换为编辑后文本, status=edited。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_irreversible_state()
    edited_text = "## 用户编辑后的输出\n用户修改了内容。"
    with patch(
        "lvyan.nodes.output_guardrail.interrupt",
        return_value=edited_text,
    ):
        result = output_guardrail(state)
    assert result["pending_human_approval"]["status"] == "edited"
    # final_output 应包含用户编辑的文本
    assert "用户编辑后的输出" in result["final_output"]


# ===========================================================================
# 16. 最终校验未通过 → risk_level 上调为 high
# ===========================================================================
def test_final_validation_risk_escalation(monkeypatch):
    """脱敏 + 修复后最终校验仍不通过 → risk_level 上调为 high。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    # 构造一个缺章节且达到迭代上限的输出（强制放行但最终校验仍不通过）
    text = """# 日常咨询快答

## 用户目标
咨询违约。

## 核心法律结论
构成违约。

## 关键法条引用
（暂无法条）

## 风险声明
以上内容仅供参考。
"""
    state = _make_state(
        text,
        statutes=[],
        output_iteration=MAX_OUTPUT_ITERATIONS,
        risk_level="low",
    )
    result = output_guardrail(state)
    # 最终校验未通过 → risk_level 应上调为 high
    assert result.get("risk_level") == "high"
    # 应有最终校验未通过的备注
    assert "最终校验" in result["final_output"] or "上调" in result["final_output"]
