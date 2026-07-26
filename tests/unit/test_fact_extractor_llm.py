"""P0-3 / P0-6：fact_extractor LLM 路径真实单测。

验证 P0-3 修复（``Fact.source`` 加入 ``"llm"``）后，LLM 抽取路径不会触发
Pydantic ValidationError；并验证 LLM 不可用时正确降级到规则路径。

覆盖：
  1. mock chat_json 返回 fact → 节点用 LLM 路径产出 ``Fact(source="llm")``；
  2. mock chat_json 返回 timeline → 节点同时产出 ``TimelineEvent``；
  3. LLM 不可用（llm_available()=False）→ 降级到规则路径，source="extracted"；
  4. chat_json 返回 None → 降级；
  5. chat_json 返回非法结构 → 降级；
  6. LLM 抽取的 category 非法 → 归为「其他」；
  7. confidence 越界 → 截断到 [0,1]。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lvyan.nodes import fact_extractor  # noqa: E402
from lvyan.schemas import CaseState, Fact  # noqa: E402


def _make_state(user_goal: str = "公司辞退我，工作 3 年，月薪 8000 元") -> CaseState:
    import datetime

    return CaseState(
        run_id="run-test",
        thread_id="thread-test",
        current_date=datetime.date(2026, 7, 26),
        user_goal=user_goal,
        case_type="劳动争议",
    )


# ---------------------------------------------------------------------------
# 1. LLM 成功路径：Fact(source="llm") 不报 ValidationError（P0-3 核心回归）
# ---------------------------------------------------------------------------
def test_llm_fact_extraction_does_not_raise(monkeypatch: pytest.MonkeyPatch):
    """mock chat_json 返回 fact → 节点应产出 source="llm" 的 Fact，不报错。"""
    monkeypatch.setattr(
        "lvyan.llm.llm_available", lambda: True
    )
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda messages, **kw: {
            "facts": [
                {
                    "category": "金额",
                    "content": "月薪 8000 元",
                    "confidence": 0.95,
                },
                {
                    "category": "行为",
                    "content": "公司辞退劳动者",
                    "confidence": 0.9,
                },
            ],
            "timeline": [
                {"date": "2023年", "description": "入职"},
                {"date": "2026年7月", "description": "被辞退"},
            ],
        },
    )

    state = _make_state()
    result = fact_extractor.fact_extractor(state)

    facts: list[Fact] = result["facts"]
    assert len(facts) >= 2
    # 关键：source="llm" 不应触发 ValidationError
    llm_facts = [f for f in facts if f.source == "llm"]
    assert len(llm_facts) >= 1
    # timeline 也被正确转换
    assert len(result["timeline"]) >= 2


def test_llm_fact_has_correct_source_value(monkeypatch: pytest.MonkeyPatch):
    """LLM 抽取的 fact source 必须是 "llm"，区别于规则抽取的 "extracted"。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda messages, **kw: {
            "facts": [{"category": "金额", "content": "5万元", "confidence": 0.9}],
            "timeline": [],
        },
    )

    result = fact_extractor.fact_extractor(_make_state())

    facts = result["facts"]
    assert any(f.source == "llm" for f in facts)
    assert not any(f.source == "extracted" for f in facts)


# ---------------------------------------------------------------------------
# 2. LLM 不可用 → 降级到规则路径
# ---------------------------------------------------------------------------
def test_fallback_to_rules_when_llm_unavailable(monkeypatch: pytest.MonkeyPatch):
    """llm_available()=False → 走规则路径，source 全为 "extracted"。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: False)

    # chat_json 不应被调用
    def _fail(*a, **kw):
        raise AssertionError("LLM 不可用时不应调用 chat_json")

    monkeypatch.setattr("lvyan.llm.chat_json", _fail)

    result = fact_extractor.fact_extractor(_make_state("公司拖欠我 5 万元工资"))

    facts = result["facts"]
    # 规则路径应抽到金额（content 形如 "5 万元"，含 5 与 万）
    amount_facts = [f for f in facts if f.category == "金额"]
    assert len(amount_facts) >= 1
    assert any("5" in f.content and "万" in f.content for f in amount_facts)
    # source 全为 extracted
    assert all(f.source == "extracted" for f in facts)


def test_fallback_when_chat_json_returns_none(monkeypatch: pytest.MonkeyPatch):
    """chat_json 返回 None → 降级。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr("lvyan.llm.chat_json", lambda messages, **kw: None)

    result = fact_extractor.fact_extractor(_make_state("拖欠 3 万元"))

    facts = result["facts"]
    # 规则路径抽到金额
    assert any(f.category == "金额" for f in facts)
    assert all(f.source == "extracted" for f in facts)


def test_fallback_when_chat_json_invalid_structure(monkeypatch: pytest.MonkeyPatch):
    """chat_json 返回非 dict 结构 → 降级。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json", lambda messages, **kw: "not a dict"
    )

    result = fact_extractor.fact_extractor(_make_state("欠薪 1 万元"))
    facts = result["facts"]
    assert any(f.source == "extracted" for f in facts)


# ---------------------------------------------------------------------------
# 3. 非法 category 归为「其他」，confidence 越界截断
# ---------------------------------------------------------------------------
def test_invalid_category_normalized_to_other(monkeypatch: pytest.MonkeyPatch):
    """LLM 返回非法 category → 归为「其他」。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda messages, **kw: {
            "facts": [
                {"category": "INVALID", "content": "事实1", "confidence": 0.8},
                {"category": "金额", "content": "事实2", "confidence": 0.7},
            ],
            "timeline": [],
        },
    )

    result = fact_extractor.fact_extractor(_make_state())
    facts = result["facts"]

    other_facts = [f for f in facts if f.category == "其他"]
    assert len(other_facts) == 1
    assert other_facts[0].content == "事实1"


def test_confidence_out_of_range_clamped(monkeypatch: pytest.MonkeyPatch):
    """confidence 越界（>1 或 <0）→ 截断到 [0,1]。"""
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda messages, **kw: {
            "facts": [
                {"category": "金额", "content": "A", "confidence": 1.5},
                {"category": "金额", "content": "B", "confidence": -0.3},
            ],
            "timeline": [],
        },
    )

    result = fact_extractor.fact_extractor(_make_state())
    facts = result["facts"]

    by_content = {f.content: f.confidence for f in facts}
    assert by_content["A"] == 1.0
    assert by_content["B"] == 0.0


# ---------------------------------------------------------------------------
# 4. Fact.source schema 验证：直接构造 source="llm" 不报错
# ---------------------------------------------------------------------------
def test_fact_source_llm_is_valid():
    """直接构造 Fact(source="llm") 应成功（P0-3 schema 修复回归）。"""
    f = Fact(
        fact_id="f1",
        category="金额",
        content="5万元",
        source="llm",
        confidence=0.9,
    )
    assert f.source == "llm"


def test_fact_source_document_is_valid():
    """source="document" 应成功（为后续文档解析预留）。"""
    f = Fact(
        fact_id="f2",
        category="行为",
        content="签约",
        source="document",
        confidence=0.85,
    )
    assert f.source == "document"
