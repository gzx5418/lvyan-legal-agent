"""法律权威条目模型。

表示从官方法律数据库或精编知识库中检索到的一条具体法条 / 司法解释条款。
``CaseState.statutes`` 字段持有该模型列表，``CitationAudit`` 据此核对引用真实性。

为避免循环导入，本模块不依赖 schemas 内其它模型。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class Authority(BaseModel):
    """法规权威条目：一条可被引用的法律 / 行政法规 / 司法解释条文。"""

    source_id: str
    title: str
    article_number: str | None = None
    article_text: str
    authority_level: str  # 宪法/法律/行政法规/司法解释/监察法规/地方性法规
    publication_date: date | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    status: Literal["effective", "repealed", "not_yet_effective", "unknown"] = "unknown"
    jurisdiction: str = "中国大陆"
    official_source: str | None = None  # 官方数据库 URL
    content_hash: str | None = None
    retrieved_at: datetime
    lexical_score: float = 0.0
    dense_score: float = 0.0
    rerank_score: float = 0.0


__all__ = ["Authority"]
