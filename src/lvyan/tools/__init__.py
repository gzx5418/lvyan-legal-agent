"""标准工具层：法规检索、类案、文档处理、计算器、文书导出等可被节点调用的工具。

所有工具统一返回 Pydantic v2 模型（继承 ``ToolResult``），可 ``model_dump_json()``
序列化为 JSON，便于 LangGraph 节点直接消费与日志记录。

子模块：
  - ``base``       : ToolResult 基类
  - ``statutes``   : 法规检索 / 条文查询 / 有效性核验
  - ``cases``      : 案例检索 / 详情查询（桩实现，基于精编知识库）
  - ``documents``  : 文档文本提取 / 合同条款风险分析
  - ``calculators``: 诉讼时效 / 赔偿金额 / 证据清单 / 时间线
  - ``export``     : Markdown -> DOCX 文书导出
"""

from __future__ import annotations

from lvyan.tools.base import ToolResult
from lvyan.tools.calculators import (
    ClaimAmountResult,
    DeadlineResult,
    EvidenceChecklistResult,
    EvidenceItem,
    TimelineItem,
    TimelineResult,
    build_case_timeline,
    calculate_claim_amount,
    calculate_legal_deadline,
    generate_evidence_checklist,
)
from lvyan.tools.cases import (
    CaseDetailResult,
    CaseHit,
    CaseSearchResult,
    get_case_detail,
    search_cases,
)
from lvyan.tools.documents import (
    ContractAnalysisResult,
    DocumentExtractResult,
    analyze_contract_clause,
    extract_document,
)
from lvyan.tools.export import (
    ExportResult,
    render_docx,
)
from lvyan.tools.statutes import (
    StatuteArticleResult,
    StatuteHit,
    StatuteSearchResult,
    StatuteStatusResult,
    get_statute_article,
    search_statutes,
    verify_statute_status,
)

__all__ = [
    # 基类
    "ToolResult",
    # 法规工具
    "StatuteHit",
    "StatuteSearchResult",
    "StatuteArticleResult",
    "StatuteStatusResult",
    "search_statutes",
    "get_statute_article",
    "verify_statute_status",
    # 案例工具
    "CaseHit",
    "CaseSearchResult",
    "CaseDetailResult",
    "search_cases",
    "get_case_detail",
    # 文档工具
    "DocumentExtractResult",
    "ContractAnalysisResult",
    "extract_document",
    "analyze_contract_clause",
    # 计算工具
    "DeadlineResult",
    "ClaimAmountResult",
    "EvidenceItem",
    "EvidenceChecklistResult",
    "TimelineItem",
    "TimelineResult",
    "calculate_legal_deadline",
    "calculate_claim_amount",
    "generate_evidence_checklist",
    "build_case_timeline",
    # 导出工具
    "ExportResult",
    "render_docx",
]
