"""类案文档模型(U-01):真实类案库的存储/检索统一 schema。

与 :class:`lvyan.schemas.evidence.CaseAuthority`(检索结果领域模型)和
:class:`lvyan.retrieval.case_source.CaseResult`(数据源传输对象)的关系:

- ``CaseDocument`` 是**入库事实源**:OpenSearch / curated / 外部 API 灌库与
  存储的统一形态,携带 v2 要素化字段(构成要件/争议焦点/诉请/抗辩/理由段落),
  支撑要件级检索(计划方向 G)与 Span 级引用(计划 U-51/U-56)。
- ``CaseAuthority`` 是**图执行内的检索结果视图**(轻量,进 CaseState)。
- ``CaseResult`` 是数据源层的传输 dataclass。

转换方向:``CaseDocument.to_authority()``(入库 → 图)。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from lvyan.schemas.evidence import CaseAuthority

__all__ = [
    "SpanRef",
    "CitedStatuteRef",
    "CaseDocument",
]


class SpanRef(BaseModel):
    """支持片段引用:定位"哪部/哪份材料的哪一段真正支撑结论"。

    - ``statute``:locator 为条/款/项(如 ``"第十三条第二款"``);
    - ``case``:locator 为判决书段落定位(如 ``"本院认为#3"`` 或段落序号区间);
    - ``document``(预留,U-56 用户证据坐标化):locator 为页码/段落,配合
      ``page`` / ``bbox`` 跳转高亮。
    """

    source_type: Literal["statute", "case", "document"]
    source_id: str
    locator: str
    text: str = Field(default="", max_length=2000, description="片段原文摘录")
    # U-56 EvidenceSpan 坐标化预留(用户上传文档场景)
    page: int | None = None
    bbox: list[int] | None = None


class CitedStatuteRef(BaseModel):
    """类案裁判所引用的法条(要素化字段,供要件级检索与关联分析)。"""

    title: str
    article_number: str = ""
    source_id: str | None = None


class CaseDocument(BaseModel):
    """入库类案文档:基础字段 + v2 要素化字段。

    要素化字段对齐 JUREX-4E 的"要件标注"思想但为自建 schema,覆盖民事:
    逐要件可检索、可举证、可关联类案理由段落。所有要素化字段可空——
    curated 桩与外部数据源可渐进补齐,但类型稳定。
    """

    case_id: str
    # 案件标题(如"张某诉某公司劳动争议案");OpenSearchCaseSource 的多字段
    # 检索以 title 为最高权重字段, LeCaRDv2/CAIL 数据均有
    title: str = ""
    case_number: str | None = None
    # None 容忍:与 curated 桩(CaseHit.court: str | None)对齐,避免灌库转换失败
    court: str | None = None
    case_type: str = ""
    # 效力级别:指导性案例 > 参考案例 > 普通;呈现与检索过滤共用
    effective_level: Literal["guiding", "reference", "normal", "unknown"] = "unknown"
    brief_facts: str = ""
    ruling_summary: str = ""
    judgment_date: date | None = None
    source_url: str | None = None
    source_name: str = ""
    content_hash: str = ""

    # --- v2 要素化字段 ---
    legal_elements: list[str] = Field(default_factory=list, description="构成要件(逐条)")
    legal_issues: list[str] = Field(default_factory=list, description="争议焦点")
    claims: list[str] = Field(default_factory=list, description="诉请/请求权基础")
    defenses: list[str] = Field(default_factory=list, description="抗辩事由")
    evidence_summary: list[str] = Field(default_factory=list, description="关键证据概要")
    reasoning_spans: list[SpanRef] = Field(default_factory=list, description="裁判理由支撑段落定位")
    cited_statutes: list[CitedStatuteRef] = Field(default_factory=list, description="裁判引用法条")

    def to_authority(self, similarity_score: float) -> CaseAuthority:
        """入库文档 → 图执行内的检索结果视图。

        ``ruling_summary`` 截断以控制 CaseState 体积(完整理由经
        ``reasoning_spans`` 按需取用)。
        """
        return CaseAuthority(
            case_id=self.case_id,
            case_number=self.case_number,
            court=self.court or "未知法院",
            case_type=self.case_type,
            brief_facts=self.brief_facts,
            ruling_summary=self.ruling_summary[:500],
            ruling_date=self.judgment_date,
            similarity_score=similarity_score,
            source_url=self.source_url,
            effective_level=self.effective_level,
        )
