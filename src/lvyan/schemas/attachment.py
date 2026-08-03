"""附件分块相关数据模型。"""
from __future__ import annotations

from pydantic import BaseModel


class AttachmentChunk(BaseModel):
    """单个附件分块：按 markdown 标题/段落切分后的最小检索单元。"""

    chunk_id: str          # f"{document_id}#{序号}"
    document_id: str       # 所属附件 file_id
    document_name: str
    section: str           # 所属标题；无标题时为 "正文"
    content: str
    char_offset: int       # 在原始 markdown 中的起始字符偏移
