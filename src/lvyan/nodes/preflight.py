"""预检节点：运行域状态复位与运行前校验。"""

from __future__ import annotations

from typing import Any

from lvyan.schemas import CaseState


def preflight(state: CaseState) -> dict[str, Any]:
    """预检节点：每 run 首节点，负责复位运行域（run-scoped）字段。

    P1 修复（跨 run 状态污染）：同 thread 的新 run 经 LangGraph checkpoint
    合并语义会沿用上一 run 的 LastValue 字段——上一 run 的 HITL 审批状态、
    输出重试计数、critic 报告等会泄漏进本 run（表现为：上一 run 以 edited
    结束后本 run 不产出 legal_answer；重试额度被提前耗尽）。preflight 是
    天然的每 run 首节点，在此统一复位运行域字段；facts / conversation_summary
    等会话累积字段**不**复位。

    未来职责
    --------
    - 校验 ``CaseState`` 必填字段（run_id / thread_id / current_date / user_goal）齐全。
    - 探测外部依赖可用性：官方法律全文库、OpenSearch、对象存储、模型网关。
    """
    return {
        "pending_human_approval": None,
        "output_iteration": 0,
        "output_retry_needed": False,
        "document_payload": None,
        "document_file": None,
        "legal_answer": None,
        "critic_report": None,
        "critic_feedback": [],
        "citation_audit": None,
        "reasoning_result": None,
        "final_output": "",
    }
