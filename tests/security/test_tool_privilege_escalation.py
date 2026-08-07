"""SubTask 18.4：工具越权测试（安全评测视角）。

验证不可逆操作均触发 Human-in-the-loop，``HITL_ENABLED=false`` 时跳过
``interrupt`` 但仍记录 warning，以及 ``render_docx`` 导出前必须经过 HITL。

覆盖场景：
1. document 模式生成律师函（含「发送律师函」）→ 触发 HITL
2. composer 输出含「提交法院」→ 触发 HITL
3. composer 输出含「签署合同」→ 触发 HITL（Task 18.4 补全）
4. composer 输出含「代为签署」→ 触发 HITL（Task 18.4 补全）
5. composer 输出含「对外共享证据」→ 触发 HITL
6. 干净输出（无不可逆操作）→ 不触发 HITL
7. HITL_ENABLED=false → 跳过 interrupt，但仍记录 pending_human_approval + warning 标注
8. HITL_ENABLED=true → interrupt 被调用（mock）
9. HITL approve → status=approved
10. HITL reject → 保留分析正文，但拒绝不可逆操作
11. 多个不可逆操作同时出现 → 全部入 pending_human_approval
12. render_docx 导出前 HITL 已标记（流程约束：guardrail 先于导出）
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from lvyan.config import settings
from lvyan.nodes.output_guardrail import output_guardrail
from lvyan.schemas import Authority
from lvyan.tools.export import render_docx


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------
def _make_authority(
    title: str = "中华人民共和国民法典",
    article_number: str = "第五百七十七条",
) -> Authority:
    from datetime import datetime, timezone

    return Authority(
        source_id=f"src-{title}-{article_number}",
        title=title,
        article_number=article_number,
        article_text=(
            "当事人一方不履行合同义务或者履行合同义务不符合约定的，"
            "应当承担继续履行、采取补救措施或者赔偿损失等违约责任。"
        ),
        authority_level="法律",
        effective_date=date(2021, 1, 1),
        status="effective",
        retrieved_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )


# 结构完整的基准输出（含全部章节 + 风险声明 + 有效引用，无 PII / 数字概率 / 不可逆操作）
_BASE_OUTPUT = """# 日常咨询快答

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


def _make_state(
    final_output: str,
    risk_level: str = "low",
    output_iteration: int = 0,
) -> dict[str, Any]:
    return {
        "run_id": "run-tool-priv",
        "thread_id": "thread-tool-priv",
        "current_date": date(2026, 7, 23),
        "user_goal": "测试工具越权",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "light",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": [_make_authority()],
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


def _output_with_action(action_text: str) -> str:
    """在基准输出的「行动建议」处插入不可逆操作描述。"""
    return _BASE_OUTPUT.replace(
        "1. 收集合同与违约证据。",
        f"1. {action_text}",
    )


# ---------------------------------------------------------------------------
# 1-5. 不可逆操作触发 HITL（HITL_ENABLED=false，验证 pending_human_approval）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "action_text, expected_op",
    [
        ("发送律师函催告对方履行合同。", "发送律师函"),
        ("将材料提交法院立案。", "向法院提交材料"),
        ("建议您签署合同补充协议。", "签署合同/协议"),
        ("由律师代为签署相关文件。", "代为签署"),
        ("对外共享证据给第三方机构。", "对外共享证据"),
        ("向法院提起诉讼追究违约责任。", "提起诉讼"),
        ("代表用户联系对方协商。", "代表用户联系第三方"),
        ("删除案件材料中的过期证据。", "删除案件材料"),
        ("修改原始合同条款后重新签署。", "修改原始合同"),
    ],
)
def test_irreversible_operations_trigger_hitl(
    monkeypatch: pytest.MonkeyPatch, action_text: str, expected_op: str
):
    """各类不可逆操作 → 触发 HITL，pending_human_approval 含对应操作描述。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    state = _make_state(_output_with_action(action_text))

    result = output_guardrail(state)

    assert result.get("pending_human_approval") is not None
    ops = result["pending_human_approval"]["operations"]
    assert expected_op in ops
    # final_output 应含「需要您确认后才能执行」标注
    assert "需要您确认后才能执行" in result["final_output"]


# ---------------------------------------------------------------------------
# 6. 干净输出 → 不触发 HITL
# ---------------------------------------------------------------------------
def test_clean_output_no_hitl(monkeypatch: pytest.MonkeyPatch):
    """无不可逆操作的输出 → 不触发 HITL。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_BASE_OUTPUT)

    result = output_guardrail(state)

    assert result.get("pending_human_approval") is None
    assert "需要您确认后才能执行" not in result["final_output"]


# ---------------------------------------------------------------------------
# 7. HITL_ENABLED=false → 跳过 interrupt，但仍记录 warning
# ---------------------------------------------------------------------------
def test_hitl_disabled_skips_interrupt_but_records_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    """HITL_ENABLED=false → 不调用 interrupt，但仍写 pending_human_approval + 标注。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    state = _make_state(_output_with_action("发送律师函催告对方。"))

    with patch("lvyan.nodes.output_guardrail.interrupt") as mock_interrupt:
        result = output_guardrail(state)

    # interrupt 不应被调用
    assert mock_interrupt.call_count == 0
    # 但 pending_human_approval 仍被记录（status=pending）
    assert result["pending_human_approval"] is not None
    assert result["pending_human_approval"]["status"] == "pending"
    # final_output 含 warning 标注
    assert "需要您确认后才能执行" in result["final_output"]


# ---------------------------------------------------------------------------
# 8. HITL_ENABLED=true → interrupt 被调用
# ---------------------------------------------------------------------------
def test_hitl_enabled_calls_interrupt(monkeypatch: pytest.MonkeyPatch):
    """HITL_ENABLED=true + 不可逆操作 → interrupt 被调用（mock 返回 approve）。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_output_with_action("发送律师函催告对方。"))

    with patch(
        "lvyan.nodes.output_guardrail.interrupt", return_value="approve"
    ) as mock_interrupt:
        result = output_guardrail(state)

    assert mock_interrupt.call_count == 1
    # interrupt 调用参数应含操作列表
    call_args = mock_interrupt.call_args[0][0]
    assert "operations" in call_args
    assert "发送律师函" in call_args["operations"]
    # 批准 → status=approved
    assert result["pending_human_approval"]["status"] == "approved"


# ---------------------------------------------------------------------------
# 9. HITL approve → status=approved
# ---------------------------------------------------------------------------
def test_hitl_approve_response(monkeypatch: pytest.MonkeyPatch):
    """用户批准 → status=approved，输出保留。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_output_with_action("向法院提起诉讼。"))

    with patch(
        "lvyan.nodes.output_guardrail.interrupt", return_value="approve"
    ):
        result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "approved"
    assert "用户目标" in result["final_output"]


# ---------------------------------------------------------------------------
# 10. HITL reject → 保留分析正文，但拒绝不可逆操作
# ---------------------------------------------------------------------------
def test_hitl_reject_response(monkeypatch: pytest.MonkeyPatch):
    """用户拒绝 → 保留分析正文并明确拒绝执行，status=rejected。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_output_with_action("向法院提起诉讼。"))
    original = state["final_output"]

    with patch(
        "lvyan.nodes.output_guardrail.interrupt", return_value="reject"
    ):
        result = output_guardrail(state)

    assert original in result["final_output"]
    assert "拒绝执行" in result["final_output"]
    assert result["pending_human_approval"]["status"] == "rejected"
    assert result["output_retry_needed"] is False


def test_hitl_none_response_treated_as_reject(monkeypatch: pytest.MonkeyPatch):
    """用户无响应（None）→ fail-closed，保留正文但拒绝执行。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_output_with_action("向法院提起诉讼。"))
    original = state["final_output"]

    with patch(
        "lvyan.nodes.output_guardrail.interrupt", return_value=None
    ):
        result = output_guardrail(state)

    assert original in result["final_output"]
    assert "拒绝执行" in result["final_output"]
    assert result["pending_human_approval"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# 11. 多个不可逆操作同时出现
# ---------------------------------------------------------------------------
def test_multiple_irreversible_operations_all_recorded(
    monkeypatch: pytest.MonkeyPatch,
):
    """输出含多个不可逆操作 → 全部入 pending_human_approval.operations。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    text = _BASE_OUTPUT.replace(
        "1. 收集合同与违约证据。",
        "1. 发送律师函催告。\n2. 向法院提起诉讼。\n3. 对外共享证据。",
    )
    state = _make_state(text)

    result = output_guardrail(state)

    ops = result["pending_human_approval"]["operations"]
    assert "发送律师函" in ops
    assert "提起诉讼" in ops
    assert "对外共享证据" in ops
    assert len(ops) >= 3


# ---------------------------------------------------------------------------
# 12. render_docx 导出前 HITL 已标记（流程约束）
# ---------------------------------------------------------------------------
def test_render_docx_must_follow_hitl(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """render_docx 调用前必须经过 HITL：含不可逆操作时 guardrail 先标记 pending。

    流程约束：``render_docx`` 是导出工具，本身不检查 HITL；HITL 在
    ``output_guardrail`` 阶段完成。本测试验证：含不可逆操作的输出经
    ``output_guardrail`` 后会设置 ``pending_human_approval``，调用方在
    未获批准前不应调用 ``render_docx`` 导出。
    """
    monkeypatch.setattr(settings, "hitl_enabled", False)
    state = _make_state(_output_with_action("发送律师函催告对方。"))

    guardrail_result = output_guardrail(state)

    # 1. guardrail 已标记需要人工审批
    assert guardrail_result["pending_human_approval"] is not None
    assert guardrail_result["pending_human_approval"]["status"] == "pending"
    # 2. final_output 含警告标注（证明 guardrail 已介入）
    assert "需要您确认后才能执行" in guardrail_result["final_output"]

    # 3. 流程约束：pending 状态下调用方不应导出；若强行调用 render_docx，
    #    导出应成功但导出的是含 HITL 警告标注的 final_output（而非原始不可逆指令）
    out = tmp_path / "letter.docx"
    export = render_docx(guardrail_result["final_output"], str(out))
    assert export.success is True
    # 导出的 output_path 可能是 .docx 或降级为 .md；这里只验证导出成功，
    # 且导出内容来源是含 HITL 标注的 final_output（guardrail 已先于导出执行）
    assert export.output_path


def test_render_docx_clean_output_no_hitl_marker(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """干净输出（无不可逆操作）经 guardrail 后无 HITL 标注，可直接导出。"""
    monkeypatch.setattr(settings, "hitl_enabled", True)
    state = _make_state(_BASE_OUTPUT)

    guardrail_result = output_guardrail(state)
    assert guardrail_result.get("pending_human_approval") is None
    # 干净输出不应含 HITL 标注
    assert "需要您确认后才能执行" not in guardrail_result["final_output"]

    out = tmp_path / "clean.docx"
    export = render_docx(guardrail_result["final_output"], str(out))
    assert export.success is True
    assert export.output_path


# ---------------------------------------------------------------------------
# 13. document 模式生成律师函 → 触发 HITL（spec 场景）
# ---------------------------------------------------------------------------
def test_document_mode_lawyer_letter_triggers_hitl(
    monkeypatch: pytest.MonkeyPatch,
):
    """document 模式下生成律师函并建议「发送律师函」→ 触发 HITL。

    使用与 light 模式一致的结构（用户目标/核心法律结论/关键法条引用/行动建议/
    风险声明），避免结构校验触发 retry 而提前返回，从而验证 HITL 检测路径。
    """
    monkeypatch.setattr(settings, "hitl_enabled", False)
    letter = _BASE_OUTPUT.replace(
        "1. 收集合同与违约证据。",
        "1. 发送律师函至乙方注册地址催告履行。",
    )
    state = _make_state(letter)

    result = output_guardrail(state)
    assert result["pending_human_approval"] is not None
    assert "发送律师函" in result["pending_human_approval"]["operations"]


# ---------------------------------------------------------------------------
# 14. 「签署」相关动作均触发 HITL（Task 18.4 补全验证）
# ---------------------------------------------------------------------------
def test_signing_actions_trigger_hitl(monkeypatch: pytest.MonkeyPatch):
    """「签署合同」「签署协议」「代为签署」均触发 HITL。"""
    monkeypatch.setattr(settings, "hitl_enabled", False)
    for action in ["建议您签署合同补充条款", "建议签署协议以确认", "由律师代为签署文件"]:
        state = _make_state(_output_with_action(action))
        result = output_guardrail(state)
        assert result["pending_human_approval"] is not None, f"未触发 HITL：{action}"
        ops = result["pending_human_approval"]["operations"]
        assert any("签署" in op for op in ops), f"签署动作未入 ops：{action}"
