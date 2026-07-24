"""Composer 节点单元测试（SubTask 14.1）。

覆盖场景：
1. Light 模式：输出包含用户目标 / 核心法律结论 / 关键法条引用 / 行动建议 / 风险声明
2. Deep 模式：输出包含案件事实摘要 / 法律关系识别 / 构成要件分析 / 争议焦点 等
3. Document 模式：输出包含文书标题 / 当事人 / 事实与理由 / 法律依据 / 落款
4. citation_audit 未通过：输出开头加显著警告
5. risk_level=high：输出末尾追加高风险声明
6. 默认 complexity（未设置）→ light 模式
7. 空 statutes：输出含占位提示
8. 返回值结构：含 final_output 键
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from lvyan.nodes.composer import composer
from lvyan.schemas import Authority, ReasoningResult


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


def _make_reasoning_result(
    judicial_tendency: str = "somewhat_favorable",
    evidence_confidence: str = "medium",
) -> ReasoningResult:
    return ReasoningResult(
        legal_relationship="合同纠纷",
        elements=["合同关系成立（已满足）", "违约行为（已满足）"],
        disputed_focus=["是否构成违约"],
        plaintiff_arguments=["原告主张对方违约应赔偿"],
        defendant_arguments=["被告主张不可抗力免责"],
        evidence_mapping=["争议焦点1 → 合同文本"],
        judicial_tendency=judicial_tendency,  # type: ignore[arg-type]
        evidence_confidence=evidence_confidence,  # type: ignore[arg-type]
        key_factors=["违约行为待证实"],
    )


def _make_base_state(
    complexity: str = "light",
    user_goal: str = "公司辞退我要求经济补偿",
    statutes: list[Authority] | None = None,
    reasoning_result: ReasoningResult | None = None,
    risk_level: str = "low",
    citation_audit: dict | None = None,
    facts: list | None = None,
    run_id: str = "run-composer-test",
) -> dict:
    """构造测试用 state dict。"""
    if statutes is None:
        statutes = [_make_authority()]
    if reasoning_result is None:
        reasoning_result = _make_reasoning_result()
    if facts is None:
        facts = []
    return {
        "run_id": run_id,
        "thread_id": "thread-composer-test",
        "current_date": date(2026, 7, 23),
        "user_goal": user_goal,
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": complexity,
        "facts": facts,
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
        "reasoning_result": reasoning_result,
        "citation_audit": citation_audit,
        "risk_level": risk_level,
        "confidence": "medium",
        "iteration": 0,
        "final_output": None,
    }


# ---------------------------------------------------------------------------
# 1. Light 模式
# ---------------------------------------------------------------------------
def test_composer_light_mode_sections():
    """Light 模式输出包含必要章节。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    assert "final_output" in result
    output = result["final_output"]
    assert "用户目标" in output
    assert "核心法律结论" in output
    assert "关键法条引用" in output
    assert "行动建议" in output
    assert "风险声明" in output


def test_composer_light_mode_contains_user_goal():
    """Light 模式输出包含 user_goal 文本。"""
    state = _make_base_state(complexity="light", user_goal="我想了解租房合同违约问题")
    result = composer(state)
    assert "我想了解租房合同违约问题" in result["final_output"]


def test_composer_light_mode_contains_statute():
    """Light 模式输出包含法条引用。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    output = result["final_output"]
    assert "中华人民共和国民法典" in output
    assert "第五百七十七条" in output


# ---------------------------------------------------------------------------
# 2. Deep 模式
# ---------------------------------------------------------------------------
def test_composer_deep_mode_sections():
    """Deep 模式输出包含全部深度分析章节。"""
    state = _make_base_state(complexity="deep")
    result = composer(state)
    output = result["final_output"]
    assert "用户问题与事实摘要" in output
    assert "案件管辖与案由" in output
    assert "法律关系分析" in output
    assert "构成要件分析" in output
    assert "争议焦点" in output
    assert "双方主张对比" in output
    assert "证据分析与缺口" in output
    assert "裁判倾向分析" in output
    assert "法条详引" in output
    assert "类案参考" in output
    assert "法规冲突提示" in output
    assert "行动建议" in output
    assert "风险声明" in output
    assert "知识来源" in output


def test_composer_deep_mode_contains_reasoning():
    """Deep 模式输出包含推理结果的关键信息。"""
    rr = _make_reasoning_result(judicial_tendency="somewhat_favorable")
    state = _make_base_state(complexity="deep", reasoning_result=rr)
    result = composer(state)
    output = result["final_output"]
    assert "合同纠纷" in output  # legal_relationship
    assert "较有利" in output  # tendency label
    assert "不可抗力" in output  # defendant argument


# ---------------------------------------------------------------------------
# 3. Document 模式
# ---------------------------------------------------------------------------
def test_composer_document_mode_lawyer_letter():
    """Document 模式（律师函）输出包含文书结构与风险声明。"""
    state = _make_base_state(
        complexity="document",
        user_goal="请帮我发律师函催告对方履行合同",
        run_id="run-composer-doc-test-1",
    )
    result = composer(state)
    output = result["final_output"]
    assert "律师函" in output
    assert "事实陈述" in output or "事实与理由" in output
    assert "法律依据" in output
    # 文书必须有风险声明（通过 output_validator 校验）
    assert "仅供参考" in output or "不构成" in output


def test_composer_document_mode_lawsuit():
    """Document 模式（起诉状）输出包含起诉状结构。"""
    state = _make_base_state(
        complexity="document",
        user_goal="我要起诉对方违约，请帮我写起诉状",
        run_id="run-composer-doc-test-2",
    )
    result = composer(state)
    output = result["final_output"]
    assert "起诉状" in output
    assert "诉讼请求" in output
    assert "事实与理由" in output
    assert "法律依据" in output


# ---------------------------------------------------------------------------
# 4. citation_audit 未通过 → 输出开头加警告
# ---------------------------------------------------------------------------
def test_composer_citation_audit_warning():
    """citation_audit.passed=False → 输出开头加引用校验未通过警告。"""
    state = _make_base_state(
        complexity="light",
        citation_audit={"passed": False, "total_citations": 1, "issues": []},
    )
    result = composer(state)
    output = result["final_output"]
    assert "引用校验未通过" in output
    # 警告应在开头
    assert output.startswith("⚠") or "引用校验未通过" in output[:50]


def test_composer_citation_audit_passed_no_warning():
    """citation_audit.passed=True → 不加引用校验警告。"""
    state = _make_base_state(
        complexity="light",
        citation_audit={"passed": True, "total_citations": 1, "issues": []},
    )
    result = composer(state)
    output = result["final_output"]
    assert "引用校验未通过" not in output


# ---------------------------------------------------------------------------
# 5. risk_level=high → 输出末尾追加高风险声明
# ---------------------------------------------------------------------------
def test_composer_high_risk_disclaimer():
    """risk_level=high → 输出包含高风险声明。"""
    state = _make_base_state(complexity="light", risk_level="high")
    result = composer(state)
    output = result["final_output"]
    assert "高风险声明" in output


def test_composer_low_risk_no_high_risk_disclaimer():
    """risk_level=low → 不追加高风险声明。"""
    state = _make_base_state(complexity="light", risk_level="low")
    result = composer(state)
    output = result["final_output"]
    assert "高风险声明" not in output


# ---------------------------------------------------------------------------
# 6. 默认 complexity（未设置）→ light 模式
# ---------------------------------------------------------------------------
def test_composer_default_complexity():
    """complexity 未设置 → 默认 light 模式。"""
    state = _make_base_state(complexity="light")
    # 移除 complexity 字段模拟未设置
    state.pop("complexity")
    result = composer(state)
    output = result["final_output"]
    # light 模式特征：包含「日常咨询快答」标题
    assert "日常咨询快答" in output or "用户目标" in output


# ---------------------------------------------------------------------------
# 7. 空 statutes → 输出含占位提示
# ---------------------------------------------------------------------------
def test_composer_empty_statutes():
    """statutes 为空 → 输出含占位提示。"""
    state = _make_base_state(complexity="light", statutes=[])
    result = composer(state)
    output = result["final_output"]
    assert "暂未检索到适用法条" in output or "暂无" in output


# ---------------------------------------------------------------------------
# 8. 返回值结构
# ---------------------------------------------------------------------------
def test_composer_return_structure():
    """返回值应包含 final_output 键。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    assert isinstance(result, dict)
    assert "final_output" in result
    assert isinstance(result["final_output"], str)
    assert len(result["final_output"]) > 0


# ---------------------------------------------------------------------------
# 9. 知识来源章节（含法规版本与生效日期）
# ---------------------------------------------------------------------------
def test_composer_light_knowledge_source():
    """Light 模式输出包含知识来源章节。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    output = result["final_output"]
    assert "知识来源" in output
    # 知识来源应包含法规版本与生效日期信息
    assert "中华人民共和国民法典" in output
    assert "有效" in output
    assert "2021-01-01" in output


def test_composer_deep_knowledge_source():
    """Deep 模式知识来源包含法规版本与生效日期。"""
    state = _make_base_state(complexity="deep")
    result = composer(state)
    output = result["final_output"]
    assert "知识来源" in output
    assert "法规版本信息" in output
    assert "2021-01-01" in output
    assert "生效" in output


# ---------------------------------------------------------------------------
# 10. document_payload（document 模式）
# ---------------------------------------------------------------------------
def test_composer_document_payload():
    """Document 模式返回 document_payload（含 template_name + filled_fields）。"""
    state = _make_base_state(
        complexity="document",
        user_goal="我要起诉对方违约，请帮我写起诉状",
        run_id="run-composer-payload-test",
    )
    result = composer(state)
    assert "document_payload" in result
    payload = result["document_payload"]
    assert payload is not None
    assert "template_name" in payload
    assert "filled_fields" in payload
    fields = payload["filled_fields"]
    assert "doc_type" in fields
    assert "title" in fields
    assert "plaintiffs" in fields
    assert "defendants" in fields
    assert "statutes" in fields


def test_composer_non_document_no_payload():
    """Light / deep 模式 document_payload 为 None。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    assert result.get("document_payload") is None


# ---------------------------------------------------------------------------
# 11. 禁止数字概率（所有模式）
# ---------------------------------------------------------------------------
def test_composer_light_no_numeric_probability():
    """Light 模式输出无数字概率表达。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    output = result["final_output"]
    # 扫描百分比 / 胜诉率等数字概率
    assert not re.search(r"\d+(?:\.\d+)?\s*[%％]", output)
    assert not re.search(r"胜诉率|胜率", output)


def test_composer_deep_no_numeric_probability():
    """Deep 模式输出无数字概率表达。"""
    state = _make_base_state(complexity="deep")
    result = composer(state)
    output = result["final_output"]
    assert not re.search(r"\d+(?:\.\d+)?\s*[%％]", output)
    assert not re.search(r"胜诉率|胜率", output)


def test_composer_deep_uses_qualitative_label():
    """Deep 模式裁判倾向使用定性标签（非数字）。"""
    state = _make_base_state(complexity="deep")
    result = composer(state)
    output = result["final_output"]
    assert "较有利" in output  # judicial_tendency=somewhat_favorable → 较有利


# ---------------------------------------------------------------------------
# 12. Light 模式行动建议最多 3 条
# ---------------------------------------------------------------------------
def test_composer_light_advice_max_three():
    """Light 模式行动建议不超过 3 条。"""
    state = _make_base_state(complexity="light")
    result = composer(state)
    output = result["final_output"]
    # 提取「## 行动建议」与「## 风险声明」之间的编号项
    advice_section = output.split("## 行动建议")[1].split("## 风险声明")[0]
    numbered = re.findall(r"^\d+\.", advice_section, re.MULTILINE)
    assert len(numbered) <= 3
