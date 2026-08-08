"""P0-6：output_guardrail HITL 行为单测。

验证 P0-2 修复后的三种 HITL action（approve / reject / edit）正确处理：

  1. approve → ``pending_human_approval.status == "approved"``，正文不变；
  2. reject  → ``status == "rejected"``，原分析正文保留（不被擦成「操作已取消」）；
  3. edit    → ``status == "edited"``，``final_output`` 被替换为 ``edited_output``；
  4. 结构化 dict 响应 ``{"action": "reject"}`` 不会被错误当成 approve；
  5. 旧式字符串响应 ``"approve"`` / ``"reject"`` 仍兼容；
  6. 未知 action 抛 ``ValueError``；
  7. edit 缺少 ``edited_output`` 抛 ``ValueError``。

通过 monkeypatch ``lvyan.config.settings.hitl_enabled=True`` 与
``langgraph.types.interrupt`` 模拟中断返回值。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# 路径引导
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lvyan.nodes import output_guardrail as og_module  # noqa: E402
from lvyan.schemas import CaseState  # noqa: E402

# 节点函数本体（模块本身不可调用）
output_guardrail = og_module.output_guardrail


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------
_VALID_LIGHT_OUTPUT = (
    "## 用户目标\n咨询发送律师函事宜。\n\n"
    "## 核心法律结论\n建议先收集证据。\n\n"
    "## 关键法条引用\n无具体条文引用。\n\n"
    "## 行动建议\n建议操作：发送律师函、提起诉讼。\n\n"
    "_以上仅供参考，不构成正式法律意见。_"
)


def _make_state(final_output: str = "") -> CaseState:
    return CaseState(
        run_id="run-test",
        thread_id="thread-test",
        current_date=__import__("datetime").date(2026, 7, 26),
        user_goal="请帮我起草发送律师函",
        complexity="light",  # 用 light 避开 deep 严格 section 校验
        final_output=final_output or _VALID_LIGHT_OUTPUT,
        risk_level="low",
    )


@pytest.fixture
def hitl_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制开启 HITL。"""
    monkeypatch.setattr("lvyan.nodes.output_guardrail.settings.hitl_enabled", True)


@pytest.fixture
def patch_interrupt(monkeypatch: pytest.MonkeyPatch):
    """返回一个 setter，用于注入 interrupt 的返回值。"""

    def _set(value: Any) -> None:
        monkeypatch.setattr(
            "lvyan.nodes.output_guardrail.interrupt",
            lambda payload: value,
        )

    return _set


# ---------------------------------------------------------------------------
# 1. approve：正文不变，status=approved
# ---------------------------------------------------------------------------
def test_hitl_approve_dict_response(hitl_enabled, patch_interrupt):
    """approve (dict) → 正文不变，pending_human_approval.status=approved。"""
    state = _make_state()
    original = state.final_output
    patch_interrupt({"action": "approve"})

    result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "approved"
    assert "用户已批准" in result["final_output"]
    # 原分析正文保留
    assert original in result["final_output"]


def test_hitl_approve_string_response(hitl_enabled, patch_interrupt):
    """approve (旧式字符串) → 同样视为批准。"""
    state = _make_state()
    patch_interrupt("approve")

    result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "approved"


# ---------------------------------------------------------------------------
# 2. reject：保留原分析正文，不被擦成「操作已取消」
# ---------------------------------------------------------------------------
def test_hitl_reject_dict_preserves_analysis(hitl_enabled, patch_interrupt):
    """reject (dict) → 保留原分析正文，仅追加拒绝提示，status=rejected。

    P0-2 关键修复点：旧实现把整篇分析替换成「操作已取消」，丢失法律分析正文。
    """
    state = _make_state(
        final_output=(
            "## 用户目标\n咨询发送律师函事宜。\n\n"
            "## 核心法律结论\n您的权益受到侵害，建议发函。\n\n"
            "## 关键法条引用\n依据民法典相关规定。\n\n"
            "_以上仅供参考，不构成正式法律意见。_"
        )
    )
    patch_interrupt({"action": "reject"})

    result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "rejected"
    # 原分析正文必须保留
    assert "权益受到侵害" in result["final_output"]
    assert "依据民法典" in result["final_output"]
    # 拒绝提示存在
    assert "拒绝" in result["final_output"]


def test_hitl_reject_string_response(hitl_enabled, patch_interrupt):
    """reject (旧式字符串) → 同样保留分析正文。"""
    state = _make_state(
        final_output=(
            "## 用户目标\n咨询发送律师函。\n\n"
            "## 核心法律结论\n原分析内容成立。\n\n"
            "## 关键法条引用\n无。\n\n"
            "## 行动建议\n建议发送律师函。\n\n"
            "_以上仅供参考，不构成正式法律意见。_"
        )
    )
    patch_interrupt("reject")

    result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "rejected"
    assert "原分析内容" in result["final_output"]


# ---------------------------------------------------------------------------
# 3. edit：替换 final_output 为 edited_output
# ---------------------------------------------------------------------------
def test_hitl_edit_dict_replaces_output(hitl_enabled, patch_interrupt):
    """edit (dict) → final_output 被替换为 edited_output。"""
    state = _make_state(
        final_output=(
            "## 用户目标\n咨询发送律师函。\n\n"
            "## 核心法律结论\n原草稿内容。\n\n"
            "## 关键法条引用\n无。\n\n"
            "## 行动建议\n建议发送律师函。\n\n"
            "_以上仅供参考，不构成正式法律意见。_"
        )
    )
    patch_interrupt({"action": "edit", "edited_output": "用户编辑后的版本"})

    result = output_guardrail(state)

    assert result["pending_human_approval"]["status"] == "edited"
    assert "用户编辑后的版本" in result["final_output"]


def test_hitl_edit_missing_edited_output_raises(hitl_enabled, patch_interrupt):
    """edit 缺少 edited_output → 抛 ValueError。"""
    state = _make_state()
    patch_interrupt({"action": "edit"})

    with pytest.raises(ValueError, match="edit 操作缺少 edited_output"):
        output_guardrail(state)


# ---------------------------------------------------------------------------
# 4. 未知 action 抛 ValueError
# ---------------------------------------------------------------------------
def test_hitl_unknown_action_raises(hitl_enabled, patch_interrupt):
    """未知 action → 抛 ValueError。"""
    state = _make_state()
    patch_interrupt({"action": "maybe"})

    with pytest.raises(ValueError, match="未知 HITL action"):
        output_guardrail(state)


# ---------------------------------------------------------------------------
# 5. P0-2 关键回归：reject 不会被错误当成 approve
# ---------------------------------------------------------------------------
def test_hitl_reject_not_treated_as_approve(hitl_enabled, patch_interrupt):
    """P0-2 核心回归：reject 必须不能进入 approve 分支。"""
    state = _make_state()
    patch_interrupt({"action": "reject"})

    result = output_guardrail(state)

    status = result["pending_human_approval"]["status"]
    assert status == "rejected", f"reject 被错误处理为 {status}"
    assert status != "approved"


def test_hitl_edit_not_treated_as_approve(hitl_enabled, patch_interrupt):
    """P0-2 核心回归：edit 必须不能进入 approve 分支。"""
    state = _make_state()
    patch_interrupt({"action": "edit", "edited_output": "新内容"})

    result = output_guardrail(state)

    status = result["pending_human_approval"]["status"]
    assert status == "edited"
    assert status != "approved"


# ---------------------------------------------------------------------------
# 6. 不可逆操作不命中时，不触发 interrupt
# ---------------------------------------------------------------------------
def test_no_irreversible_op_no_interrupt(hitl_enabled, monkeypatch: pytest.MonkeyPatch):
    """输出中不含不可逆操作 → 不应调用 interrupt。"""
    called = {"count": 0}

    def _fail_interrupt(payload):
        called["count"] += 1
        raise AssertionError("不应触发 interrupt")

    monkeypatch.setattr("lvyan.nodes.output_guardrail.interrupt", _fail_interrupt)

    state = _make_state(
        final_output=(
            "## 用户目标\n咨询法律问题。\n\n"
            "## 核心法律结论\n本案不涉及外部动作。\n\n"
            "## 关键法条引用\n无具体条文引用。\n\n"
            "_以上仅供参考，不构成正式法律意见。_"
        )
    )
    result = output_guardrail(state)

    assert called["count"] == 0
    assert "pending_human_approval" not in result
