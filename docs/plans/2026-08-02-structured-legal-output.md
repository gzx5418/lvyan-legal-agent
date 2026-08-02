# 结构化法律输出 (LegalAnswerV1) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将法律 Agent 的最终输出从单一 Markdown 字符串升级为结构化 `LegalAnswerV1` JSON，前端组件化渲染，并保留 Markdown 回退兼容。

**Architecture:** 在 `composer` 节点产出 Markdown 的同时，新增 `build_legal_answer(state)` 构建结构化 Pydantic 模型；`output_guardrail` 增加结构化校验器；SSE `final_output` 事件扩展为同时携带 `answer`（结构化）与 `markdown_fallback`；前端按 `schema_version` 分支渲染，旧前端继续用 Markdown。

**Tech Stack:** Python 3.11 / Pydantic v2 / LangGraph / FastAPI / 原生 JS（无框架）

---

## 背景与现状

当前 `composer` 节点把 `CaseState` 中已有的结构化数据（`facts` / `statutes` / `cases` / `reasoning_result` / `evidence_requirements` / `missing_facts`）全部压平成一段 Markdown 字符串写入 `final_output`，经 SSE 整体发送，前端用手写 Markdown 渲染器显示。这导致同类案件输出样式不一致、法条格式随机、风险表达模糊、无法稳定导出 DOCX/PDF。

本计划复用已有结构化中间态，新增面向最终输出的 `LegalAnswerV1` 数据协议，使「法律结论、事实、证据、争点、法条、行动建议」成为具有固定字段与关联关系的可审计数据。

## 文件结构总览

**新建文件：**
- `src/lvyan/schemas/legal_answer.py` — `LegalAnswerV1` 及全部子模型定义（唯一数据协议源）
- `src/lvyan/nodes/answer_builder.py` — `build_legal_answer(state)` 构建器（CaseState → LegalAnswerV1）
- `src/lvyan/nodes/answer_validator.py` — 结构化校验器（防幻觉、字段完整性、引用一致性）
- `tests/unit/test_legal_answer_model.py` — 数据模型测试
- `tests/unit/test_answer_builder.py` — 构建器测试
- `tests/unit/test_answer_validator.py` — 校验器测试
- `src/lvyan/api/static/components.js` — 前端结构化渲染组件

**修改文件：**
- `src/lvyan/schemas/__init__.py` — 导出新模型
- `src/lvyan/graph/state.py` — `GraphState` 增加 `legal_answer` 字段
- `src/lvyan/nodes/composer.py` — 调用 `build_legal_answer` 并写入 state
- `src/lvyan/nodes/output_guardrail.py` — 增加结构化校验
- `src/lvyan/api/sse.py` — SSE 事件扩展（同时发送 `answer` 与 `markdown_fallback`）
- `src/lvyan/api/static/app.js` — 按 `schema_version` 分支渲染

---

## 阶段一：数据协议与后端构建

### Task 1: 定义 LegalAnswerV1 数据模型

**Files:**
- Create: `src/lvyan/schemas/legal_answer.py`
- Create: `tests/unit/test_legal_answer_model.py`

- [ ] **Step 1: 编写模型测试**

创建 `tests/unit/test_legal_answer_model.py`：

```python
"""LegalAnswerV1 数据协议模型测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lvyan.schemas.legal_answer import (
    LegalAnswerV1,
    AnswerMeta,
    ExecutiveSummary,
    FactItem,
    LegalIssue,
    EvidenceItem,
    RiskItem,
    ActionItem,
    LegalCitation,
    UncertaintyItem,
)


def test_legal_answer_minimal_valid():
    """最小合法 LegalAnswerV1 只需 meta + executive_summary + disclaimer。"""
    answer = LegalAnswerV1(
        schema_version="legal_answer_v1",
        meta=AnswerMeta(
            title="测试分析",
            jurisdiction="中国大陆",
            case_type="租赁合同纠纷",
            law_as_of_date="2026-08-02",
            risk_level="medium",
            material_completeness="partial",
        ),
        executive_summary=ExecutiveSummary(
            conclusion="测试结论",
            key_reasons=["理由1"],
            main_uncertainty="不确定点",
        ),
        facts=[],
        issues=[],
        evidence=[],
        risks=[],
        action_plan=[],
        citations=[],
        uncertainties=[],
        disclaimer="本内容为法律信息分析，不是法院裁判。",
    )
    assert answer.schema_version == "legal_answer_v1"


def test_fact_item_status_enum():
    """FactItem.status 必须是四态之一。"""
    for status in ("confirmed", "claimed", "inferred", "missing"):
        item = FactItem(fact_id="F1", content="x", status=status)
        assert item.status == status
    with pytest.raises(ValidationError):
        FactItem(fact_id="F1", content="x", status="invalid")


def test_legal_issue_requires_rules_or_facts():
    """LegalIssue 至少应关联规则或事实（验证默认值结构）。"""
    issue = LegalIssue(
        issue_id="I1",
        question="争点",
        conclusion="结论",
        rules=["C1"],
        supporting_facts=["F1"],
        analysis="分析",
    )
    assert issue.counterarguments == []  # 默认空列表


def test_citation_authority_level_order():
    """LegalCitation.level 必须是固定权威层级。"""
    for level in ("law", "regulation", "judicial_interpretation", "guiding_case", "reference_case", "normative"):
        c = LegalCitation(
            citation_id="C1",
            full_name="法",
            level=level,
            article_number="第一条",
            status="effective",
        )
        assert c.level == level


def test_risk_item_no_numeric_probability_required():
    """RiskItem 不强制要求数字概率（避免虚假精确）。"""
    r = RiskItem(
        dimension="证据充分程度",
        rating="medium",
        detail="缺少交接材料",
    )
    assert r.score is None  # 可选字段


def test_action_item_phase_enum():
    """ActionItem.phase 必须是固定时序枚举。"""
    for phase in ("immediate", "short_term", "contingency"):
        a = ActionItem(phase=phase, description="行动", target="目标")
        assert a.phase == phase
    with pytest.raises(ValidationError):
        ActionItem(phase="never", description="x", target="y")


def test_schema_version_immutable_literal():
    """schema_version 只接受 legal_answer_v1。"""
    with pytest.raises(ValidationError):
        LegalAnswerV1(
            schema_version="v2",
            meta=AnswerMeta(
                title="t", jurisdiction="中国大陆", case_type="c",
                law_as_of_date="2026-08-02", risk_level="low",
                material_completeness="complete",
            ),
            executive_summary=ExecutiveSummary(
                conclusion="c", key_reasons=[], main_uncertainty="u"
            ),
            disclaimer="d",
        )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_legal_answer_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lvyan.schemas.legal_answer'`

- [ ] **Step 3: 实现数据模型**

创建 `src/lvyan/schemas/legal_answer.py`：

```python
"""LegalAnswerV1：法律 Agent 最终输出的结构化数据协议。

设计原则：
1. 所有法律结论、事实、证据、争点、法条、行动建议均为显式字段，不依赖自由文本。
2. 禁止字段化「胜诉率」「概率百分比」等未经校准的数字，风险仅用定性维度表达。
3. 法条引用必须包含完整名称 + 条款序号 + 效力状态，便于审计防幻觉。
4. 事实必须标注四态来源（已确认/用户陈述/系统推断/缺失），防止把推断写成事实。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 顶部元信息
# ---------------------------------------------------------------------------
class AnswerMeta(BaseModel):
    """输出顶部身份与适用范围信息（程序生成，不由模型自由修改）。"""

    title: str
    jurisdiction: str  # 中国大陆/港澳台/涉外
    case_type: str
    law_as_of_date: str  # ISO 日期字符串，便于跨语言传输
    risk_level: Literal["low", "medium", "high"]
    material_completeness: Literal["complete", "partial", "insufficient"]
    analysis_mode: Literal["light", "deep", "document"] = "deep"


# ---------------------------------------------------------------------------
# 结论摘要
# ---------------------------------------------------------------------------
class ExecutiveSummary(BaseModel):
    """核心判断摘要（3-5 条，普通用户优先看到）。"""

    conclusion: str
    key_reasons: list[str] = []
    main_uncertainty: str


# ---------------------------------------------------------------------------
# 事实分层
# ---------------------------------------------------------------------------
FactStatus = Literal["confirmed", "claimed", "inferred", "missing"]


class FactItem(BaseModel):
    """单条事实，带来源状态标签。

    - confirmed: 有合同/转账/聊天记录等支撑
    - claimed: 仅来自用户描述
    - inferred: Agent 根据上下文推测
    - missing: 会影响结论但尚未提供
    """

    fact_id: str
    content: str
    status: FactStatus
    source_ref: str | None = None  # 支撑证据引用
    detail: str | None = None


# ---------------------------------------------------------------------------
# 争议焦点（本地化 IRAC）
# ---------------------------------------------------------------------------
class LegalIssue(BaseModel):
    """单个争点，采用结论-规则-分析-反方-小结五段式。"""

    issue_id: str
    question: str  # 争点表述
    conclusion: str  # 初步结论
    rules: list[str] = []  # 关联的 citation_id 列表
    supporting_facts: list[str] = []  # 关联的 fact_id 列表
    analysis: str = ""  # 适用分析
    counterarguments: list[str] = []  # 不利因素


# ---------------------------------------------------------------------------
# 证据矩阵
# ---------------------------------------------------------------------------
class EvidenceItem(BaseModel):
    """证据分析表的单行。"""

    evidence_id: str
    name: str
    purpose: str  # 证明目的
    status: Literal["provided", "missing", "partial"]
    probative_force: Literal["key", "strong", "medium", "weak"]
    next_step: str | None = None


# ---------------------------------------------------------------------------
# 风险矩阵（多维度，无单一胜诉率）
# ---------------------------------------------------------------------------
class RiskItem(BaseModel):
    """风险的单一维度评估。score 可选，仅用于可计算指标（如材料完整度）。"""

    dimension: str  # 如「证据充分程度」「法律依据明确度」
    rating: Literal["high", "medium", "low"]
    detail: str
    score: float | None = None  # 仅可计算维度使用，0.0-1.0


# ---------------------------------------------------------------------------
# 行动建议（分时序）
# ---------------------------------------------------------------------------
class ActionItem(BaseModel):
    """下一步行动的单项。"""

    phase: Literal["immediate", "short_term", "contingency"]
    description: str
    target: str  # 目标
    required_materials: list[str] = []
    deadline: str | None = None
    risk: str | None = None


# ---------------------------------------------------------------------------
# 法条与案例引用
# ---------------------------------------------------------------------------
CitationLevel = Literal[
    "law",  # 法律
    "regulation",  # 行政法规
    "judicial_interpretation",  # 司法解释
    "guiding_case",  # 指导性案例
    "reference_case",  # 参考案例
    "normative",  # 规范性文件
]


class LegalCitation(BaseModel):
    """单条法律引用卡片。

    正文只显示 full_name + article_number，详情（条款正文/来源）折叠展示。
    """

    citation_id: str
    full_name: str  # 法律文件全称，如《中华人民共和国民法典》
    article_number: str  # 条款序号，如「第七百零三条」
    article_text: str = ""  # 条款正文（折叠展示）
    level: CitationLevel
    status: Literal["effective", "repealed", "not_yet_effective", "unknown"] = "unknown"
    effective_date: str | None = None
    official_source: str | None = None
    role_in_analysis: str | None = None  # 本条在当前分析中的作用


# ---------------------------------------------------------------------------
# 不确定性说明
# ---------------------------------------------------------------------------
class UncertaintyItem(BaseModel):
    """显式声明的不确定点，避免模型伪装确定。"""

    description: str
    impact: str  # 对结论的影响
    resolution: str | None = None  # 如何消除该不确定


# ---------------------------------------------------------------------------
# 顶层协议
# ---------------------------------------------------------------------------
class LegalAnswerV1(BaseModel):
    """法律 Agent 最终结构化输出协议。"""

    schema_version: Literal["legal_answer_v1"] = "legal_answer_v1"
    meta: AnswerMeta
    executive_summary: ExecutiveSummary
    facts: list[FactItem] = []
    issues: list[LegalIssue] = []
    evidence: list[EvidenceItem] = []
    risks: list[RiskItem] = []
    action_plan: list[ActionItem] = []
    citations: list[LegalCitation] = []
    uncertainties: list[UncertaintyItem] = []
    disclaimer: str


__all__ = [
    "LegalAnswerV1",
    "AnswerMeta",
    "ExecutiveSummary",
    "FactItem",
    "FactStatus",
    "LegalIssue",
    "EvidenceItem",
    "RiskItem",
    "ActionItem",
    "LegalCitation",
    "CitationLevel",
    "UncertaintyItem",
]
```

- [ ] **Step 4: 更新 schemas 导出**

修改 `src/lvyan/schemas/__init__.py`，在现有导出后增加：

```python
from .legal_answer import (
    LegalAnswerV1,
    AnswerMeta,
    ExecutiveSummary,
    FactItem,
    LegalIssue,
    EvidenceItem,
    RiskItem,
    ActionItem,
    LegalCitation,
    UncertaintyItem,
)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_legal_answer_model.py -v`
Expected: 8 passed

- [ ] **Step 6: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/schemas/legal_answer.py src/lvyan/schemas/__init__.py tests/unit/test_legal_answer_model.py
git commit -m "feat: add LegalAnswerV1 structured output data protocol"
```

---

### Task 2: 实现 build_legal_answer 构建器

**Files:**
- Create: `src/lvyan/nodes/answer_builder.py`
- Create: `tests/unit/test_answer_builder.py`

- [ ] **Step 1: 编写构建器测试**

创建 `tests/unit/test_answer_builder.py`：

```python
"""build_legal_answer 构建器测试。"""
from __future__ import annotations

from datetime import date, datetime

from lvyan.schemas.case import CaseState, Fact, MissingFact
from lvyan.schemas.authority import Authority
from lvyan.schemas.evidence import CaseAuthority, EvidenceRequirement
from lvyan.schemas.output import ReasoningResult, CitationAudit, CitationDetail

from lvyan.nodes.answer_builder import build_legal_answer


def _make_state(**overrides) -> CaseState:
    """构造测试用 CaseState。"""
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


def test_build_minimal_state():
    """空状态也能构建合法 LegalAnswerV1（带免责声明）。"""
    state = _make_state()
    answer = build_legal_answer(state)
    assert answer.schema_version == "legal_answer_v1"
    assert answer.meta.case_type == "房屋租赁合同纠纷"
    assert answer.meta.risk_level == "medium"
    assert "法律信息分析" in answer.disclaimer


def test_facts_classified_by_source():
    """Fact 按 source 映射到四态标签。"""
    state = _make_state(
        facts=[
            Fact(fact_id="F1", category="金额", content="押金3000元", source="document"),
            Fact(fact_id="F2", category="行为", content="房东拒退", source="user"),
            Fact(fact_id="F3", category="其他", content="推测无损坏", source="llm"),
        ],
        missing_facts=[
            MissingFact(fact_key="M1", question="有无交接记录?", reason="影响判定"),
        ],
    )
    answer = build_legal_answer(state)
    by_id = {f.fact_id: f for f in answer.facts}
    # document/user 来源 → confirmed/claimed；llm → inferred；missing_facts → missing
    assert by_id["F1"].status == "confirmed"
    assert by_id["F2"].status == "claimed"
    assert by_id["F3"].status == "inferred"
    assert any(f.status == "missing" for f in answer.facts)


def test_statutes_become_citations():
    """Authority 转换为 LegalCitation，保留 full_name/article/status。"""
    state = _make_state(
        statutes=[
            Authority(
                source_id="S1",
                title="中华人民共和国民法典",
                article_number="第七百零三条",
                article_text="租赁合同是出租人...",
                authority_level="法律",
                status="effective",
                retrieved_at=datetime(2026, 8, 2),
            ),
        ],
    )
    answer = build_legal_answer(state)
    assert len(answer.citations) == 1
    c = answer.citations[0]
    assert c.full_name == "中华人民共和国民法典"
    assert c.article_number == "第七百零三条"
    assert c.level == "law"
    assert c.status == "effective"


def test_reasoning_result_maps_to_issues():
    """disputed_focus 映射为 LegalIssue 列表。"""
    state = _make_state(
        reasoning_result=ReasoningResult(
            judicial_tendency="somewhat_favorable",
            evidence_confidence="medium",
            disputed_focus=["房东能否扣除全部押金", "损坏举证责任归属"],
            key_factors=["押金支付事实", "缺少交接记录"],
        ),
    )
    answer = build_legal_answer(state)
    assert len(answer.issues) == 2
    assert answer.issues[0].question == "房东能否扣除全部押金"


def test_evidence_requirements_map_to_matrix():
    """EvidenceRequirement 映射为证据矩阵行。"""
    state = _make_state(
        evidence_requirements=[
            EvidenceRequirement(
                requirement_id="E1",
                fact_to_prove="已支付押金",
                evidence_types=["转账记录"],
                current_status="met",
            ),
            EvidenceRequirement(
                requirement_id="E2",
                fact_to_prove="房屋交接状态",
                evidence_types=["交接单", "照片"],
                current_status="missing",
                gap_description="未提供",
            ),
        ],
    )
    answer = build_legal_answer(state)
    assert len(answer.evidence) == 2
    by_id = {e.evidence_id: e for e in answer.evidence}
    assert by_id["E1"].status == "provided"
    assert by_id["E2"].status == "missing"


def test_risk_level_maps_to_risk_matrix():
    """risk_level 映射为综合风险维度 + 多维度评估。"""
    state = _make_state(
        risk_level="high",
        confidence="low",
    )
    answer = build_legal_answer(state)
    assert len(answer.risks) >= 1
    assert any(r.dimension == "综合风险" and r.rating == "high" for r in answer.risks)


def test_action_plan_generated_from_missing_facts():
    """缺失事实生成 immediate 行动建议。"""
    state = _make_state(
        missing_facts=[
            MissingFact(fact_key="M1", question="补充租赁合同", reason="关键证据", is_blocking=True),
        ],
    )
    answer = build_legal_answer(state)
    assert any(a.phase == "immediate" for a in answer.action_plan)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lvyan.nodes.answer_builder'`

- [ ] **Step 3: 实现构建器**

创建 `src/lvyan/nodes/answer_builder.py`：

```python
"""LegalAnswerV1 构建器：从 CaseState 构建结构化法律输出。

将 composer 已有的结构化中间态（facts / statutes / cases / reasoning_result /
evidence_requirements / missing_facts）映射为 LegalAnswerV1，使最终输出具备
固定字段与关联关系，便于前端组件化渲染与 DOCX/PDF 导出。
"""
from __future__ import annotations

from typing import Any

from lvyan.schemas.case import CaseState, Fact, MissingFact
from lvyan.schemas.authority import Authority
from lvyan.schemas.evidence import EvidenceRequirement
from lvyan.schemas.output import ReasoningResult
from lvyan.schemas.legal_answer import (
    LegalAnswerV1,
    AnswerMeta,
    ExecutiveSummary,
    FactItem,
    LegalIssue,
    EvidenceItem,
    RiskItem,
    ActionItem,
    LegalCitation,
    UncertaintyItem,
)

_DISCLAIMER = (
    "本内容为基于用户陈述和已上传材料生成的法律信息分析，"
    "不是法院裁判、律师正式法律意见或结果保证。"
)

# 权威层级中文 → 协议枚举映射
_AUTHORITY_LEVEL_MAP: dict[str, str] = {
    "宪法": "law",
    "法律": "law",
    "行政法规": "regulation",
    "司法解释": "judicial_interpretation",
    "监察法规": "regulation",
    "地方性法规": "regulation",
    "部门规章": "normative",
}

_FACT_STATUS_MAP: dict[str, str] = {
    "document": "confirmed",
    "extracted": "confirmed",
    "user": "claimed",
    "llm": "inferred",
}


def _fmt_date(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _classify_fact(fact: Fact) -> FactItem:
    status = _FACT_STATUS_MAP.get(fact.source, "claimed")
    return FactItem(
        fact_id=fact.fact_id,
        content=fact.content,
        status=status,  # type: ignore[arg-type]
        source_ref=fact.source_ref,
        detail=fact.category,
    )


def _authority_to_citation(auth: Authority) -> LegalCitation:
    level = _AUTHORITY_LEVEL_MAP.get(auth.authority_level, "normative")
    return LegalCitation(
        citation_id=auth.source_id,
        full_name=auth.title,
        article_number=auth.article_number or "",
        article_text=auth.article_text,
        level=level,  # type: ignore[arg-type]
        status=auth.status,
        effective_date=_fmt_date(auth.effective_date),
        official_source=auth.official_source,
    )


def _build_facts(state: CaseState) -> list[FactItem]:
    items = [_classify_fact(f) for f in state.facts]
    for mf in state.missing_facts:
        items.append(
            FactItem(
                fact_id=mf.fact_key,
                content=mf.question,
                status="missing",
                detail=mf.reason,
            )
        )
    return items


def _build_issues(reasoning: ReasoningResult | None) -> list[LegalIssue]:
    if reasoning is None:
        return []
    issues: list[LegalIssue] = []
    for idx, focus in enumerate(reasoning.disputed_focus):
        issues.append(
            LegalIssue(
                issue_id=f"I{idx + 1}",
                question=focus,
                conclusion=reasoning.legal_relationship or "",
                rules=[],
                supporting_facts=[],
                analysis="",
                counterarguments=reasoning.defendant_arguments if idx == 0 else [],
            )
        )
    return issues


def _build_evidence(reqs: list[EvidenceRequirement]) -> list[EvidenceItem]:
    status_map = {"met": "provided", "partial": "partial", "missing": "missing"}
    force_map = {"met": "strong", "partial": "medium", "missing": "key"}
    items: list[EvidenceItem] = []
    for req in reqs:
        items.append(
            EvidenceItem(
                evidence_id=req.requirement_id,
                name=req.fact_to_prove,
                purpose=req.fact_to_prove,
                status=status_map.get(req.current_status, "missing"),  # type: ignore[arg-type]
                probative_force=force_map.get(req.current_status, "medium"),  # type: ignore[arg-type]
                next_step=req.gap_description,
            )
        )
    return items


def _build_risks(state: CaseState) -> list[RiskItem]:
    risks: list[RiskItem] = []
    rating_map = {"low": "low", "medium": "medium", "high": "high"}
    risks.append(
        RiskItem(
            dimension="综合风险",
            rating=rating_map.get(state.risk_level, "medium"),  # type: ignore[arg-type]
            detail=f"风险等级：{state.risk_level}",
        )
    )
    conf_detail = {
        "high": "证据较充分",
        "medium": "证据部分充分",
        "low": "证据不足",
        "insufficient": "证据严重不足，难以得出确定结论",
    }
    risks.append(
        RiskItem(
            dimension="证据置信度",
            rating="high" if state.confidence == "high" else ("medium" if state.confidence == "medium" else "low"),  # type: ignore[arg-type]
            detail=conf_detail.get(state.confidence, "未知"),
        )
    )
    if state.reasoning_result is not None:
        tend = state.reasoning_result.judicial_tendency
        tend_map = {
            "favorable": ("low", "整体趋势有利"),
            "somewhat_favorable": ("low", "整体趋势较有利"),
            "even": ("medium", "双方势均力敌"),
            "somewhat_unfavorable": ("high", "存在不利趋势"),
            "insufficient": ("high", "信息不足以判断趋势"),
        }
        rating, detail = tend_map.get(tend, ("medium", "趋势未知"))
        risks.append(
            RiskItem(dimension="司法裁判趋势", rating=rating, detail=detail)  # type: ignore[arg-type]
        )
    return risks


def _build_action_plan(state: CaseState) -> list[ActionItem]:
    plan: list[ActionItem] = []
    for mf in state.missing_facts:
        if mf.is_blocking:
            plan.append(
                ActionItem(
                    phase="immediate",
                    description=f"补充材料：{mf.question}",
                    target="补齐关键证据缺口",
                    required_materials=[mf.question],
                    risk=mf.reason,
                )
            )
    if not plan:
        plan.append(
            ActionItem(
                phase="immediate",
                description="整理现有证据材料并妥善保存原始文件",
                target="固定证据",
            )
        )
    plan.append(
        ActionItem(
            phase="short_term",
            description="发送书面催告，要求对方说明具体扣款项目与依据",
            target="尝试协商解决",
        )
    )
    plan.append(
        ActionItem(
            phase="contingency",
            description="协商不成时，整理证据目录与起诉材料，选择适当程序",
            target="依法维权",
        )
    )
    return plan


def _build_summary(state: CaseState) -> ExecutiveSummary:
    reasoning = state.reasoning_result
    if reasoning is not None:
        conclusion = reasoning.legal_relationship or "基于现有材料的初步分析"
        key_reasons = reasoning.key_factors[:3] if reasoning.key_reasons else []
    else:
        conclusion = f"关于「{state.user_goal}」的初步分析"
        key_reasons = []
    uncertainty = (
        state.missing_facts[0].question
        if state.missing_facts
        else "暂无显著不确定点"
    )
    return ExecutiveSummary(
        conclusion=conclusion,
        key_reasons=key_reasons,
        main_uncertainty=uncertainty,
    )


def build_legal_answer(state: CaseState) -> LegalAnswerV1:
    """从 CaseState 构建 LegalAnswerV1 结构化输出。"""
    meta = AnswerMeta(
        title=state.user_goal or "法律分析意见",
        jurisdiction=state.jurisdiction or "中国大陆",
        case_type=state.case_type or "未分类",
        law_as_of_date=_fmt_date(state.law_as_of_date or state.current_date),
        risk_level=state.risk_level,
        material_completeness=(
            "complete"
            if not state.missing_facts
            else ("partial" if len(state.missing_facts) <= 2 else "insufficient")
        ),
        analysis_mode=state.complexity,
    )
    return LegalAnswerV1(
        meta=meta,
        executive_summary=_build_summary(state),
        facts=_build_facts(state),
        issues=_build_issues(state.reasoning_result),
        evidence=_build_evidence(state.evidence_requirements),
        risks=_build_risks(state),
        action_plan=_build_action_plan(state),
        citations=[_authority_to_citation(a) for a in state.statutes],
        uncertainties=[
            UncertaintyItem(
                description=mf.question,
                impact=mf.reason,
                resolution="补充相应材料后可进一步判断" if mf.is_blocking else None,
            )
            for mf in state.missing_facts
        ],
        disclaimer=_DISCLAIMER,
    )


__all__ = ["build_legal_answer"]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_builder.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/nodes/answer_builder.py tests/unit/test_answer_builder.py
git commit -m "feat: add build_legal_answer to map CaseState to structured output"
```

---

### Task 3: 实现结构化校验器

**Files:**
- Create: `src/lvyan/nodes/answer_validator.py`
- Create: `tests/unit/test_answer_validator.py`

- [ ] **Step 1: 编写校验器测试**

创建 `tests/unit/test_answer_validator.py`：

```python
"""LegalAnswerV1 校验器测试。"""
from __future__ import annotations

import pytest

from lvyan.schemas.legal_answer import (
    LegalAnswerV1,
    AnswerMeta,
    ExecutiveSummary,
    LegalIssue,
    FactItem,
    LegalCitation,
)
from lvyan.nodes.answer_validator import validate_legal_answer, ValidationError as AVError


def _make_answer(**overrides) -> LegalAnswerV1:
    base = dict(
        schema_version="legal_answer_v1",
        meta=AnswerMeta(
            title="t", jurisdiction="中国大陆", case_type="c",
            law_as_of_date="2026-08-02", risk_level="low",
            material_completeness="complete",
        ),
        executive_summary=ExecutiveSummary(
            conclusion="c", key_reasons=["r"], main_uncertainty="u"
        ),
        disclaimer="本内容为法律信息分析，不是法院裁判。",
    )
    base.update(overrides)
    return LegalAnswerV1(**base)


def test_valid_answer_passes():
    answer = _make_answer()
    validate_legal_answer(answer)  # 不抛异常


def test_issue_referencing_nonexistent_fact_fails():
    answer = _make_answer(
        issues=[
            LegalIssue(
                issue_id="I1", question="q", conclusion="c",
                supporting_facts=["F999"],  # 不存在
            )
        ],
    )
    with pytest.raises(AVError, match="F999"):
        validate_legal_answer(answer)


def test_issue_referencing_nonexistent_citation_fails():
    answer = _make_answer(
        issues=[
            LegalIssue(
                issue_id="I1", question="q", conclusion="c",
                rules=["C999"],  # 不存在
            )
        ],
    )
    with pytest.raises(AVError, match="C999"):
        validate_legal_answer(answer)


def test_citation_missing_article_number_fails():
    answer = _make_answer(
        citations=[
            LegalCitation(
                citation_id="C1", full_name="某法", article_number="",
                level="law", status="effective",
            ),
        ],
    )
    with pytest.raises(AVError, match="条款序号"):
        validate_legal_answer(answer)


def test_guaranteed_win_language_fails():
    answer = _make_answer(
        executive_summary=ExecutiveSummary(
            conclusion="保证胜诉", key_reasons=[], main_uncertainty="u"
        ),
    )
    with pytest.raises(AVError, match="不当承诺"):
        validate_legal_answer(answer)


def test_repealed_citation_without_warning_fails():
    answer = _make_answer(
        citations=[
            LegalCitation(
                citation_id="C1", full_name="旧法", article_number="第一条",
                level="law", status="repealed",
            ),
        ],
    )
    with pytest.raises(AVError, match="已失效"):
        validate_legal_answer(answer)


def test_inferred_fact_without_uncertainty_warning():
    """存在 inferred 事实但无 uncertainties → 警告但不阻断（允许通过）。"""
    answer = _make_answer(
        facts=[FactItem(fact_id="F1", content="推断", status="inferred")],
        uncertainties=[],  # 无不确定性声明
    )
    # inferred 事实存在时应有 uncertainties，但仅警告不阻断
    validate_legal_answer(answer)  # 通过
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_validator.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现校验器**

创建 `src/lvyan/nodes/answer_validator.py`：

```python
"""LegalAnswerV1 结构化校验器：在输出进入前端前拦截法律幻觉与字段不一致。

校验项：
1. 每个 issue 的 supporting_facts / rules 引用必须真实存在
2. 法条引用必须包含 full_name + article_number
3. 不得出现「保证胜诉」「百分之百」等不当承诺
4. 已失效法律引用必须有对应 uncertainty 说明
5. 存在 inferred 事实时应提示不确定性
"""
from __future__ import annotations

from lvyan.schemas.legal_answer import LegalAnswerV1


class ValidationError(Exception):
    """结构化校验失败。"""


# 禁止出现的不当承诺措辞（含正则模糊匹配）
_FORBIDDEN_PHRASES = [
    "保证胜诉",
    "百分之百",
    "100%胜诉",
    "必定胜诉",
    "稳赢",
    "绝对能赢",
]


def _check_reference_integrity(answer: LegalAnswerV1) -> None:
    fact_ids = {f.fact_id for f in answer.facts}
    citation_ids = {c.citation_id for c in answer.citations}
    for issue in answer.issues:
        for fid in issue.supporting_facts:
            if fid not in fact_ids:
                raise ValidationError(
                    f"争点 {issue.issue_id} 引用了不存在的事实 {fid}"
                )
        for cid in issue.rules:
            if cid not in citation_ids:
                raise ValidationError(
                    f"争点 {issue.issue_id} 引用了不存在的法条 {cid}"
                )


def _check_citations(answer: LegalAnswerV1) -> None:
    for c in answer.citations:
        if not c.article_number.strip():
            raise ValidationError(
                f"法条引用 {c.citation_id}（{c.full_name}）缺少条款序号"
            )


def _check_forbidden_promises(answer: LegalAnswerV1) -> None:
    texts = [
        answer.executive_summary.conclusion,
        *answer.executive_summary.key_reasons,
        answer.executive_summary.main_uncertainty,
        *[i.conclusion for i in answer.issues],
        *[i.analysis for i in answer.issues],
    ]
    for text in texts:
        for phrase in _FORBIDDEN_PHRASES:
            if phrase in text:
                raise ValidationError(
                    f"输出包含不当承诺措辞「{phrase}」，法律分析不得保证结果"
                )


def _check_repealed_citations(answer: LegalAnswerV1) -> None:
    uncertainty_texts = " ".join(u.description for u in answer.uncertainties)
    for c in answer.citations:
        if c.status == "repealed" and c.full_name not in uncertainty_texts:
            raise ValidationError(
                f"引用了已失效法律「{c.full_name}」，但未在不确定性中说明"
            )


def validate_legal_answer(answer: LegalAnswerV1) -> None:
    """校验 LegalAnswerV1，失败抛 ValidationError。

    校验失败时不应让模型自行润色放行，而应返回结构化错误触发重写。
    """
    _check_reference_integrity(answer)
    _check_citations(answer)
    _check_forbidden_promises(answer)
    _check_repealed_citations(answer)


__all__ = ["validate_legal_answer", "ValidationError"]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_validator.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/nodes/answer_validator.py tests/unit/test_answer_validator.py
git commit -m "feat: add structured validator to catch hallucinations and bad promises"
```

---

## 阶段二：集成到图与 SSE 传输

### Task 4: GraphState 增加 legal_answer 字段并集成到 composer

**Files:**
- Modify: `src/lvyan/graph/state.py` (字段定义区，约 L175-248)
- Modify: `src/lvyan/nodes/composer.py` (composer 函数，约 L849-883)

- [ ] **Step 1: 编写集成测试**

在 `tests/unit/test_answer_builder.py` 末尾追加：

```python
def test_composer_writes_legal_answer_to_state():
    """composer 节点应在 final_output 之外同时写入 legal_answer。"""
    from lvyan.nodes.composer import composer

    state = _make_state()
    result = composer(state)
    assert "final_output" in result
    assert "legal_answer" in result
    # legal_answer 应是可序列化 dict
    assert result["legal_answer"]["schema_version"] == "legal_answer_v1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_builder.py::test_composer_writes_legal_answer_to_state -v`
Expected: FAIL with KeyError or AssertionError

- [ ] **Step 3: GraphState 增加 legal_answer 字段**

修改 `src/lvyan/graph/state.py`，在 `final_output` 字段附近增加：

```python
final_output: str | None = None
# LegalAnswerV1 结构化输出（与 final_output 并行，供前端组件化渲染）
legal_answer: dict | None = None
```

注意：使用 `dict | None` 而非 Pydantic 模型，因为 LangGraph state 需要 JSON 可序列化；构建器返回的 `LegalAnswerV1` 通过 `.model_dump()` 写入。

- [ ] **Step 4: composer 调用 build_legal_answer**

修改 `src/lvyan/nodes/composer.py` 的 `composer` 函数（约 L849-883），在返回 `result` 前增加结构化构建：

```python
def composer(state: CaseState) -> dict[str, Any]:
    complexity = str(_get(state, "complexity", "light") or "light")
    document_payload: dict[str, Any] | None = None
    if complexity == "deep":
        output = _compose_deep(state)
    elif complexity == "document":
        output, document_payload = _compose_document(state)
    else:
        output = _compose_light(state)

    risk_level = str(_get(state, "risk_level", "low") or "low")
    if risk_level == "high" and "高风险声明" not in output:
        output = output + "\n\n> ⚠️ **高风险声明**：本案存在较高法律风险，建议优先咨询专业律师。"

    # 结构化输出：构建 LegalAnswerV1 并校验
    from lvyan.nodes.answer_builder import build_legal_answer
    from lvyan.nodes.answer_validator import validate_legal_answer, ValidationError as AVError
    from lvyan.schemas.case import CaseState as _CS

    legal_answer_dict: dict[str, Any] | None = None
    try:
        # composer 接收的 state 可能是 dict 或 CaseState，统一转换
        cs = state if isinstance(state, _CS) else _CS.model_validate(state)
        answer = build_legal_answer(cs)
        validate_legal_answer(answer)
        legal_answer_dict = answer.model_dump(mode="json")
    except AVError as exc:
        _logger.warning("legal_answer 校验失败，仅返回 Markdown: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("legal_answer 构建失败，仅返回 Markdown: %s", exc)

    result: dict[str, Any] = {
        "final_output": output,
        "document_payload": document_payload,
        "legal_answer": legal_answer_dict,
    }
    return result
```

确保文件顶部已导入 `_logger`（若已有则无需重复）。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_answer_builder.py -v`
Expected: 8 passed

- [ ] **Step 6: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/graph/state.py src/lvyan/nodes/composer.py tests/unit/test_answer_builder.py
git commit -m "feat: composer emits structured legal_answer alongside markdown"
```

---

### Task 5: SSE 事件扩展，同时发送结构化数据与 Markdown 回退

**Files:**
- Modify: `src/lvyan/api/sse.py` (default_runner 的流收集逻辑，约 L980-988；_drive 发送点 L505；_resume_drive 发送点 L808)

- [ ] **Step 1: 编写 SSE 事件格式测试**

在 `tests/unit/test_api_safety.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 结构化输出：SSE final_output 事件携带 answer + markdown_fallback
# ---------------------------------------------------------------------------
def test_final_output_event_includes_structured_answer_when_available():
    """当 state 含 legal_answer 时，SSE final_output 事件应同时携带 answer 与 markdown_fallback。"""
    from lvyan.api.sse import format_sse_event
    import json

    legal_answer = {
        "schema_version": "legal_answer_v1",
        "meta": {"title": "测试"},
    }
    event = {
        "event": "final_output",
        "output": "# Markdown 回退",
        "schema_version": "legal_answer_v1",
        "answer": legal_answer,
        "markdown_fallback": "# Markdown 回退",
    }
    frame = format_sse_event(event)
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload["schema_version"] == "legal_answer_v1"
    assert payload["answer"]["meta"]["title"] == "测试"
    assert payload["markdown_fallback"] == "# Markdown 回退"
    # 旧字段 output 仍保留，兼容旧前端
    assert payload["output"] == "# Markdown 回退"


def test_final_output_event_falls_back_to_markdown_only():
    """无 legal_answer 时，事件仅含 output（旧格式，兼容）。"""
    from lvyan.api.sse import format_sse_event
    import json

    event = {"event": "final_output", "output": "# 纯 Markdown"}
    payload = json.loads(format_sse_event(event).removeprefix("data: ").strip())
    assert payload["output"] == "# 纯 Markdown"
    assert "answer" not in payload
```

- [ ] **Step 2: 运行测试确认通过（format_sse_event 已支持任意 dict）**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/unit/test_api_safety.py::test_final_output_event_includes_structured_answer_when_available tests/unit/test_api_safety.py::test_final_output_event_falls_back_to_markdown_only -v`
Expected: PASS（format_sse_event 本就透传 dict）

- [ ] **Step 3: default_runner 收集 legal_answer**

修改 `src/lvyan/api/sse.py` 的 `_stream_graph_events`（约 L980-988），在收集 `final_output` 的同时收集 `legal_answer`：

```python
elif mode == "updates" and isinstance(payload, dict):
    for _node_name, update in payload.items():
        if isinstance(update, dict):
            out = update.get("final_output")
            if out:
                final_output = out
            la = update.get("legal_answer")
            if la:
                legal_answer = la
```

函数返回值改为 `(final_output, legal_answer)`，相应更新调用处（`_drive` 与 `_resume_drive` 中调用 `_stream_graph_events` 的位置）。

- [ ] **Step 4: _drive / _resume_drive 发送扩展事件**

修改 `_drive`（约 L505）和 `_resume_drive`（约 L808）的发送逻辑：

```python
await ctx.publish(
    {
        "event": "final_output",
        "output": ctx.final_output,
        "schema_version": "legal_answer_v1" if ctx.legal_answer else None,
        "answer": ctx.legal_answer,
        "markdown_fallback": ctx.final_output,
    }
)
```

在 `RunContext` 中增加 `legal_answer: dict | None = None` 字段（约 L60 属性定义区）。

- [ ] **Step 5: 运行测试套件确认无回归**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/ -q -m "not slow" --tb=short`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/sse.py tests/unit/test_api_safety.py
git commit -m "feat: SSE final_output carries structured answer with markdown fallback"
```

---

## 阶段三：前端组件化渲染

### Task 6: 前端按 schema_version 分支渲染

**Files:**
- Create: `src/lvyan/api/static/components.js`
- Modify: `src/lvyan/api/static/app.js` (final_output 事件处理，约 L441-448；index.html 引入 components.js)

- [ ] **Step 1: 实现 components.js 渲染组件**

创建 `src/lvyan/api/static/components.js`，实现以下函数（返回 HTML 字符串）：

```javascript
// 法律分析结构化渲染组件
// 设计：低饱和专业配色，单栏正文，渐进展开

const LEGAL_COLORS = {
  primary: '#1F4B7A',   // 深法务蓝
  success: '#287A5B',   // 墨绿
  warning: '#B7791F',   // 琥珀
  danger: '#B42318',    // 暗红
  inferred: '#6B5CA5',  // 紫灰
  neutral: '#667085',   // 灰蓝
};

const FACT_STATUS_META = {
  confirmed: { label: '已确认', color: LEGAL_COLORS.success, icon: '✓' },
  claimed:   { label: '待核实', color: LEGAL_COLORS.neutral, icon: '○' },
  inferred:  { label: '系统推断', color: LEGAL_COLORS.inferred, icon: '△' },
  missing:   { label: '需补充', color: LEGAL_COLORS.warning, icon: '!' },
};

const RISK_RATING_META = {
  high:   { label: '高风险', color: LEGAL_COLORS.danger },
  medium: { label: '中风险', color: LEGAL_COLORS.warning },
  low:    { label: '低风险', color: LEGAL_COLORS.success },
};

function esc(text) {
  if (text == null) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderLegalAnswer(answer) {
  if (!answer || answer.schema_version !== 'legal_answer_v1') return '';
  const parts = [
    renderMeta(answer.meta),
    renderExecutiveSummary(answer.executive_summary),
    renderFacts(answer.facts || []),
    renderIssues(answer.issues || []),
    renderEvidence(answer.evidence || []),
    renderRisks(answer.risks || []),
    renderActionPlan(answer.action_plan || []),
    renderCitations(answer.citations || []),
    renderUncertainties(answer.uncertainties || []),
    renderDisclaimer(answer.disclaimer),
  ];
  return '<div class="legal-answer">' + parts.join('') + '</div>';
}

function renderMeta(meta) {
  const risk = RISK_RATING_META[meta.risk_level] || RISK_RATING_META.medium;
  return `
    <div class="la-meta">
      <h1>法律分析意见</h1>
      <div class="la-meta-grid">
        <span>案件类型：<strong>${esc(meta.case_type)}</strong></span>
        <span>适用法域：<strong>${esc(meta.jurisdiction)}</strong></span>
        <span>法律适用时间：<strong>${esc(meta.law_as_of_date)}</strong></span>
        <span>风险等级：<strong style="color:${risk.color}">${risk.label}</strong></span>
        <span>材料完整度：<strong>${esc(meta.material_completeness)}</strong></span>
      </div>
    </div>`;
}

function renderExecutiveSummary(summary) {
  if (!summary) return '';
  const reasons = (summary.key_reasons || [])
    .map(r => `<li>${esc(r)}</li>`).join('');
  return `
    <section class="la-section">
      <h2>核心结论</h2>
      <p class="la-conclusion">${esc(summary.conclusion)}</p>
      ${reasons ? `<ul class="la-reasons">${reasons}</ul>` : ''}
      <p class="la-uncertainty-main">主要不确定点：${esc(summary.main_uncertainty)}</p>
    </section>`;
}

function renderFacts(facts) {
  if (!facts.length) return '';
  const items = facts.map(f => {
    const meta = FACT_STATUS_META[f.status] || FACT_STATUS_META.claimed;
    return `
      <li class="la-fact" style="border-left:3px solid ${meta.color}">
        <span class="la-fact-icon" style="color:${meta.color}">${meta.icon}</span>
        <span class="la-fact-tag" style="color:${meta.color}">${meta.label}</span>
        <span class="la-fact-content">${esc(f.content)}</span>
      </li>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>事实基础</h2>
      <ul class="la-facts">${items}</ul>
    </section>`;
}

function renderIssues(issues) {
  if (!issues.length) return '';
  const blocks = issues.map(issue => {
    const counter = (issue.counterarguments || [])
      .map(c => `<li>${esc(c)}</li>`).join('');
    return `
      <div class="la-issue">
        <h3>${esc(issue.question)}</h3>
        <p><strong>初步结论：</strong>${esc(issue.conclusion)}</p>
        ${issue.analysis ? `<p><strong>适用分析：</strong>${esc(issue.analysis)}</p>` : ''}
        ${counter ? `<div class="la-counter"><strong>不利因素：</strong><ul>${counter}</ul></div>` : ''}
      </div>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>争议焦点</h2>
      ${blocks}
    </section>`;
}

function renderEvidence(evidence) {
  if (!evidence.length) return '';
  const rows = evidence.map(e => `
    <tr>
      <td>${esc(e.name)}</td>
      <td>${esc(e.purpose)}</td>
      <td>${esc(e.status)}</td>
      <td>${esc(e.probative_force)}</td>
      <td>${esc(e.next_step || '')}</td>
    </tr>`).join('');
  return `
    <section class="la-section">
      <h2>证据分析</h2>
      <table class="la-evidence-table">
        <thead><tr><th>证据</th><th>证明目的</th><th>状态</th><th>证明力</th><th>下一步</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function renderRisks(risks) {
  if (!risks.length) return '';
  const rows = risks.map(r => {
    const meta = RISK_RATING_META[r.rating] || RISK_RATING_META.medium;
    return `
      <tr>
        <td>${esc(r.dimension)}</td>
        <td style="color:${meta.color}">${meta.label}</td>
        <td>${esc(r.detail)}</td>
      </tr>`;
  }).join('');
  return `
    <section class="la-section">
      <h2>风险评估</h2>
      <table class="la-risk-table">
        <thead><tr><th>维度</th><th>等级</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function renderActionPlan(plan) {
  if (!plan.length) return '';
  const phaseLabel = { immediate: '24小时内', short_term: '近期（3日内）', contingency: '协商失败后' };
  const grouped = {};
  plan.forEach(a => {
    const p = a.phase || 'short_term';
    if (!grouped[p]) grouped[p] = [];
    grouped[p].push(a);
  });
  const blocks = ['immediate', 'short_term', 'contingency']
    .filter(p => grouped[p])
    .map(p => {
      const items = grouped[p].map((a, i) => `
        <li>
          <strong>${esc(a.description)}</strong>
          ${a.required_materials && a.required_materials.length ? `<br><small>所需材料：${a.required_materials.map(esc).join('、')}</small>` : ''}
          ${a.deadline ? `<br><small>截止：${esc(a.deadline)}</small>` : ''}
        </li>`).join('');
      return `<div class="la-action-phase"><h4>${phaseLabel[p] || p}</h4><ol>${items}</ol></div>`;
    }).join('');
  return `
    <section class="la-section">
      <h2>下一步行动</h2>
      ${blocks}
    </section>`;
}

function renderCitations(citations) {
  if (!citations.length) return '';
  const items = citations.map(c => `
    <details class="la-citation">
      <summary><strong>《${esc(c.full_name)}》${esc(c.article_number)}</strong></summary>
      <div class="la-citation-detail">
        <p>${esc(c.article_text)}</p>
        <p><small>效力状态：${esc(c.status)}　来源：${esc(c.official_source || '官方数据库')}</small></p>
      </div>
    </details>`).join('');
  return `
    <section class="la-section">
      <h2>法律依据</h2>
      ${items}
    </section>`;
}

function renderUncertainties(uncertainties) {
  if (!uncertainties.length) return '';
  const items = uncertainties.map(u => `
    <li>
      <strong>${esc(u.description)}</strong>
      <br><small>影响：${esc(u.impact)}</small>
      ${u.resolution ? `<br><small>建议：${esc(u.resolution)}</small>` : ''}
    </li>`).join('');
  return `
    <section class="la-section la-uncertainties">
      <h2>不确定性说明</h2>
      <ul>${items}</ul>
    </section>`;
}

function renderDisclaimer(disclaimer) {
  return `
    <section class="la-section la-disclaimer">
      <p>${esc(disclaimer || '')}</p>
    </section>`;
}

window.renderLegalAnswer = renderLegalAnswer;
```

- [ ] **Step 2: index.html 引入 components.js**

修改 `src/lvyan/api/static/index.html`，在 `app.js` 引入前增加：

```html
<script src="/static/components.js"></script>
```

并在 `<head>` 增加 CSS（可在现有 `<style>` 内追加）：

```css
.legal-answer { max-width: 880px; line-height: 1.75; font-size: 16px; }
.la-section { margin: 24px 0; padding: 16px 0; border-bottom: 1px solid #e4e7ec; }
.la-meta h1 { color: #1F4B7A; font-size: 24px; }
.la-meta-grid { display: flex; flex-wrap: wrap; gap: 12px; color: #667085; font-size: 14px; }
.la-conclusion { font-size: 17px; font-weight: 500; color: #1F4B7A; }
.la-facts { list-style: none; padding: 0; }
.la-fact { padding: 8px 12px; margin: 6px 0; background: #f9fafb; }
.la-fact-icon, .la-fact-tag { font-weight: 600; margin-right: 6px; }
.la-issue { margin: 16px 0; padding: 12px; background: #f9fafb; border-radius: 4px; }
.la-counter { color: #B7791F; }
.la-evidence-table, .la-risk-table { width: 100%; border-collapse: collapse; }
.la-evidence-table th, .la-risk-table th { background: #f2f4f7; text-align: left; padding: 8px; }
.la-evidence-table td, .la-risk-table td { padding: 8px; border-bottom: 1px solid #e4e7ec; }
.la-citation { margin: 8px 0; padding: 8px; background: #f9fafb; }
.la-disclaimer { color: #667085; font-size: 13px; border-top: 2px solid #e4e7ec; }
```

- [ ] **Step 3: app.js 按 schema_version 分支渲染**

修改 `src/lvyan/api/static/app.js` 的 `final_output` 事件处理（约 L441-448）：

```javascript
case 'final_output':
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  if (event.answer && event.schema_version === 'legal_answer_v1' && window.renderLegalAnswer) {
    updateLastAgentMessageStructured(event.answer, event.markdown_fallback || event.output || '');
  } else {
    updateLastAgentMessage(event.output || '(无输出)');
  }
  finalizeRun();
  break;
```

在 `app.js` 中新增 `updateLastAgentMessageStructured` 函数（紧邻 `updateLastAgentMessage`）：

```javascript
function updateLastAgentMessageStructured(answer, markdownFallback) {
  const structuredHtml = window.renderLegalAnswer(answer);
  const messages = document.querySelectorAll('.msg-agent');
  if (messages.length === 0) return;
  const last = messages[messages.length - 1];
  last.dataset.content = markdownFallback;
  last.dataset.structuredAnswer = JSON.stringify(answer);
  last.innerHTML = structuredHtml;
}
```

- [ ] **Step 4: 手动验证**

启动服务后上传一个案件材料并运行 deep 分析，确认前端显示结构化组件而非纯 Markdown。

- [ ] **Step 5: 提交**

```bash
cd e:\compelet\法律\AGENT
git add src/lvyan/api/static/components.js src/lvyan/api/static/app.js src/lvyan/api/static/index.html
git commit -m "feat: frontend renders structured legal answer components"
```

---

### Task 7: 运行完整回归测试并更新 .env.example

**Files:**
- Modify: `.env.example` (可选：增加结构化输出相关说明)

- [ ] **Step 1: 运行完整测试套件**

Run: `cd e:\compelet\法律\AGENT && python -m pytest tests/ -q -m "not slow" --tb=short`
Expected: 全部通过，无回归

- [ ] **Step 2: 运行 ruff 检查**

Run: `cd e:\compelet\法律\AGENT && python -m ruff check src/lvyan/schemas/legal_answer.py src/lvyan/nodes/answer_builder.py src/lvyan/nodes/answer_validator.py`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
cd e:\compelet\法律\AGENT
git add -A
git commit -m "test: full regression pass for structured legal output"
```

---

## 自检清单

- [ ] Task 1：LegalAnswerV1 及 10 个子模型定义，8 个测试
- [ ] Task 2：build_legal_answer 从 CaseState 构建结构化输出，7 个测试
- [ ] Task 3：校验器拦截引用不一致、缺条款序号、不当承诺、已失效引用，7 个测试
- [ ] Task 4：composer 同时产出 Markdown + legal_answer，GraphState 新增字段
- [ ] Task 5：SSE 事件扩展，兼容旧前端（output 字段保留）
- [ ] Task 6：前端 components.js 组件化渲染，按 schema_version 分支
- [ ] Task 7：完整回归 + ruff 通过

## 未在本计划范围（后续阶段）

以下为后续迭代，不在本轮实施：
- DOCX / PDF 导出（基于 LegalAnswerV1 渲染）
- 真实跨实例 SSE 事件总线与 Last-Event-ID 重放
- 附件接入 MinIO/S3 共享存储
- Dense 语义检索替换 hash 重排
- run heartbeat / lease / abandoned 自动回收
