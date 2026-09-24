"""GraphState ↔ CaseState 字段一致性守护测试。

GraphState（TypedDict，图执行层）与 CaseState（Pydantic，领域模型）是人工
维护的双模型。历史 bug 表明两者会静默漂移：CaseState 新增字段时 GraphState
漏列只能靠节点测试间接暴露；GraphState 多出的字段被
``CaseState.model_validate`` 静默丢弃——若有人把该视图当完整状态回写即丢数据。

本测试固化两个契约：
1. CaseState 字段必须是 GraphState 字段的子集（新领域字段必须同步进图状态）；
2. GraphState 多出的字段必须恰为**运行域白名单**（新字段入图须显式登记）。
"""

from __future__ import annotations

from lvyan.graph.state import GraphState
from lvyan.schemas import CaseState

# GraphState 中允许超出 CaseState 的字段（运行域：图执行层状态，不属于领域
# 模型；preflight 每 run 复位）。新增运行域字段时须同步：
# 1. 此白名单；2. preflight 复位清单（若属于"上一 run 不得泄漏进本 run"的域）。
RUN_SCOPED_FIELDS: frozenset[str] = frozenset(
    {
        "critic_report",
        "critic_feedback",
        "pending_human_approval",
        "output_iteration",
        "output_retry_needed",
        "legal_answer",
        "document_payload",
        "document_file",
    }
)


def test_case_state_is_subset_of_graph_state():
    """CaseState 每个字段都必须存在于 GraphState——防止领域模型漂移漏列。"""
    case_fields = set(CaseState.model_fields.keys())
    graph_fields = set(GraphState.__annotations__.keys())
    missing = case_fields - graph_fields
    assert not missing, f"GraphState 缺少 CaseState 新增字段: {sorted(missing)}"


def test_graph_state_extras_are_exactly_run_scoped_whitelist():
    """GraphState 超出 CaseState 的字段必须恰为运行域白名单。"""
    case_fields = set(CaseState.model_fields.keys())
    graph_fields = set(GraphState.__annotations__.keys())
    extras = graph_fields - case_fields
    assert extras == set(RUN_SCOPED_FIELDS), (
        "GraphState 多出的字段与运行域白名单不一致："
        f"未登记={sorted(extras - set(RUN_SCOPED_FIELDS))}, "
        f"已废弃={sorted(set(RUN_SCOPED_FIELDS) - extras)}。"
        "新增运行域字段请同步本测试白名单与 preflight 复位清单。"
    )
