"""Triage（管辖分流）与 Fact Extractor（事实抽取）节点单元测试。

覆盖 Task 10 验证标准：
1. 港澳台管辖判断正确。
2. 大陆管辖与案由识别。
3. 复杂度分级（light / deep / document）。
4. 事实抽取——金额。
5. 事实抽取——时间线。
6. 缺失事实评估（劳动合同 blocking）。
7. 规划器生成 PlanStep 与 RetrievalQuery。
8. 紧急期限检测（risk_level 非 low）。
"""

from __future__ import annotations

from lvyan.nodes.fact_extractor import fact_extractor
from lvyan.nodes.planner import missing_fact_assessor, planner
from lvyan.nodes.triage import jurisdiction_triage


# ---------------------------------------------------------------------------
# 测试 1：管辖判断——港澳台
# ---------------------------------------------------------------------------
def test_jurisdiction_foreign():
    """检测港澳台关键词 → jurisdiction=港澳台/涉外, risk_level=high。"""
    state = {"user_goal": "我在香港签的合同出了问题"}
    result = jurisdiction_triage(state)
    assert result["jurisdiction"] == "港澳台/涉外"
    assert result["risk_level"] == "high"
    # 涉外案件应追加 blocking MissingFact 提示用户
    missing = result.get("missing_facts", [])
    assert any(getattr(m, "is_blocking", False) for m in missing)


# ---------------------------------------------------------------------------
# 测试 2：管辖判断——大陆
# ---------------------------------------------------------------------------
def test_jurisdiction_mainland():
    """无港澳台关键词 → jurisdiction=中国大陆, 案由=劳动争议。"""
    state = {"user_goal": "公司辞退我怎么赔偿"}
    result = jurisdiction_triage(state)
    assert result["jurisdiction"] == "中国大陆"
    assert result["case_type"] == "劳动争议"


# ---------------------------------------------------------------------------
# 测试 3：复杂度分级
# ---------------------------------------------------------------------------
def test_complexity_light():
    """无深度/文书关键词 → complexity=light。"""
    state = {"user_goal": "公司辞退我"}
    result = jurisdiction_triage(state)
    assert result["complexity"] == "light"


def test_complexity_deep():
    """含起诉/胜诉 → complexity=deep。"""
    state = {"user_goal": "我想起诉公司，胜诉率多少"}
    result = jurisdiction_triage(state)
    assert result["complexity"] == "deep"


def test_complexity_document():
    """含起草/起诉状 → complexity=document（document 优先于 deep）。"""
    state = {"user_goal": "帮我起草起诉状"}
    result = jurisdiction_triage(state)
    assert result["complexity"] == "document"


# ---------------------------------------------------------------------------
# 测试 4：事实抽取——金额
# ---------------------------------------------------------------------------
def test_extract_amount():
    """抽取金额事实，content 含"5万"。"""
    state = {"user_goal": "公司欠我5万工资"}
    result = fact_extractor(state)
    facts = result.get("facts", [])
    amount_facts = [f for f in facts if getattr(f, "category", None) == "金额"]
    assert len(amount_facts) >= 1
    content = getattr(amount_facts[0], "content", "")
    assert "5万" in content
    # source 应为 extracted
    assert getattr(amount_facts[0], "source", "") == "extracted"


# ---------------------------------------------------------------------------
# 测试 5：事实抽取——时间线
# ---------------------------------------------------------------------------
def test_extract_timeline():
    """抽取两个时间点 → timeline 至少 2 个事件。"""
    state = {"user_goal": "去年3月入职，今年5月被辞退"}
    result = fact_extractor(state)
    timeline = result.get("timeline", [])
    assert len(timeline) >= 2


# ---------------------------------------------------------------------------
# 测试 6：缺失事实评估
# ---------------------------------------------------------------------------
def test_missing_facts_labor_contract_blocking():
    """劳动争议案由 → missing_facts 含"劳动合同"项（is_blocking=False，不阻断主流程）。"""
    state = {"user_goal": "公司辞退我", "case_type": "劳动争议", "facts": []}
    result = fact_extractor(state)
    missing = result.get("missing_facts", [])
    # 找出与"劳动合同"相关的缺失项
    labor_contract_items = []
    for m in missing:
        text = (
            getattr(m, "fact_key", "")
            + getattr(m, "question", "")
            + getattr(m, "reason", "")
        )
        if "劳动合同" in text or "labor_contract" in text:
            labor_contract_items.append(m)
    assert len(labor_contract_items) >= 1, "应包含关于劳动合同的缺失事实"
    # 新设计：is_blocking 全部为 False，让 composer 输出基于现有信息的分析
    # 同时在末尾提示用户补充关键事实
    item = labor_contract_items[0]
    assert getattr(item, "is_blocking", False) is False


def test_missing_fact_assessor_also_detects_blocking():
    """missing_fact_assessor 在 missing_facts 已存在时不重复追加（避免重复项）。"""
    # 先用 fact_extractor 生成初始 missing_facts
    state = {
        "user_goal": "公司辞退我",
        "case_type": "劳动争议",
        "facts": [],
        "missing_facts": [],
    }
    fe_result = fact_extractor(state)
    state["missing_facts"] = fe_result["missing_facts"]

    # 再调用 missing_fact_assessor，应返回 {}（不重复追加）
    result = missing_fact_assessor(state)
    assert result == {} or "missing_facts" not in result, \
        "missing_fact_assessor 不应重复追加 missing_facts"


# ---------------------------------------------------------------------------
# 测试 7：规划器
# ---------------------------------------------------------------------------
def test_planner_generates_plan_and_queries():
    """planner 生成 >=2 个 PlanStep 与 >=1 个 RetrievalQuery。"""
    state = {"user_goal": "公司辞退我", "case_type": "劳动争议", "complexity": "deep"}
    result = planner(state)
    plan = result.get("plan", [])
    queries = result.get("retrieval_queries", [])
    assert len(plan) >= 2, "plan 应至少有 2 个 PlanStep"
    assert len(queries) >= 1, "retrieval_queries 应至少有 1 个"
    assert result.get("iteration") == 0
    # 所有 PlanStep 初始状态应为 pending
    for step in plan:
        assert getattr(step, "status", "") == "pending"


# ---------------------------------------------------------------------------
# 测试 8：紧急期限检测
# ---------------------------------------------------------------------------
def test_urgency_detection_raises_risk_level():
    """含诉讼时效关键词 → risk_level 应为 medium 或 high（非 low）。"""
    state = {"user_goal": "诉讼时效快过了怎么办"}
    result = jurisdiction_triage(state)
    assert result["risk_level"] != "low"
    assert result["risk_level"] in ("medium", "high")
