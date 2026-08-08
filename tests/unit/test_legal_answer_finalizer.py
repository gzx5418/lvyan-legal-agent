"""legal_answer_finalizer 节点测试。"""

from __future__ import annotations

from datetime import date

from lvyan.schemas.case import CaseState
from lvyan.nodes.legal_answer_finalizer import legal_answer_finalizer


def _make_state(**overrides) -> CaseState:
    defaults = dict(
        run_id="run-1",
        thread_id="thread-1",
        current_date=date(2026, 8, 2),
        user_goal="追回押金",
        jurisdiction="中国大陆",
        case_type="房屋租赁合同纠纷",
        complexity="deep",
        risk_level="medium",
        confidence="medium",
        law_as_of_date=date(2026, 8, 2),
    )
    defaults.update(overrides)
    return CaseState(**defaults)


def test_finalizer_builds_legal_answer_for_deep_mode():
    """deep 模式正常构建结构化输出。"""
    state = _make_state(complexity="deep")
    result = legal_answer_finalizer(state)
    assert result["legal_answer"] is not None
    assert result["legal_answer"]["schema_version"] == "legal_answer_v1"


def test_finalizer_skips_document_mode():
    """P0-2：document 模式返回 legal_answer=None，不覆盖文书输出。"""
    state = _make_state(complexity="document")
    result = legal_answer_finalizer(state)
    assert result["legal_answer"] is None


def test_finalizer_clears_on_hitl_edit():
    """P0-1：HITL 编辑后清空 legal_answer，前端回退 Markdown。"""
    state = _make_state()
    state_dict = state.model_dump()
    state_dict["pending_human_approval"] = {"status": "edited", "action": "edit"}
    result = legal_answer_finalizer(state_dict)
    assert result["legal_answer"] is None


def test_finalizer_keeps_answer_when_hitl_approved():
    """HITL 批准后仍保留结构化输出。"""
    state = _make_state()
    state_dict = state.model_dump()
    state_dict["pending_human_approval"] = {"status": "approved", "action": "approve"}
    result = legal_answer_finalizer(state_dict)
    assert result["legal_answer"] is not None


def test_finalizer_syncs_risk_level():
    """P0-1：finalizer 用 guardrail 后的 risk_level 同步 meta。"""
    state = _make_state(risk_level="low")
    state_dict = state.model_dump()
    state_dict["risk_level"] = "high"  # guardrail 上调
    result = legal_answer_finalizer(state_dict)
    assert result["legal_answer"]["meta"]["risk_level"] == "high"


def test_finalizer_redacts_pii_in_string_fields():
    """P0-1：结构化字段中的手机号被脱敏。"""
    state = _make_state()
    state.facts = []  # 清空，用 user_goal 注入手机号
    state.user_goal = "联系手机 13800138000 追回押金"
    result = legal_answer_finalizer(state)
    answer = result["legal_answer"]
    # user_goal 进入 meta.title，应被脱敏
    assert "13800138000" not in answer["meta"]["title"]
