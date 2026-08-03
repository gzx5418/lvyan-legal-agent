"""附件 Markdown 分块器：按标题与段落切分，控制单块长度。"""
from __future__ import annotations

import re

from lvyan.schemas.attachment import AttachmentChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _split_long_section(body: str, max_chars: int) -> list[str]:
    """将过长的 section 正文按段落/句子边界继续切分。"""
    if len(body) <= max_chars:
        return [body] if body.strip() else []
    paragraphs = re.split(r"\n\s*\n", body)
    pieces: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}".strip() if buf else p
        else:
            if buf:
                pieces.append(buf)
            while len(p) > max_chars:
                pieces.append(p[:max_chars])
                p = p[max_chars:]
            buf = p
    if buf.strip():
        pieces.append(buf)
    return pieces


def chunk_attachment_markdown(
    md: str,
    document_id: str,
    document_name: str,
    max_chars: int = 800,
) -> list[AttachmentChunk]:
    """把附件 markdown 切成 ``AttachmentChunk`` 列表。

    策略：
      1. 以 markdown 标题（``# .. ######``）为一级边界；
      2. 标题下的正文若超过 ``max_chars``，按段落继续切分；
      3. 无任何标题时整篇视为 "正文" section。

    每个 chunk 记录在原始 md 中的 ``char_offset``。
    """
    if not md or not md.strip():
        return []

    headings = list(_HEADING_RE.finditer(md))
    chunks: list[AttachmentChunk] = []
    idx = 0

    if not headings:
        for piece in _split_long_section(md.strip(), max_chars):
            offset = md.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section="正文",
                    content=piece,
                    char_offset=offset if offset >= 0 else 0,
                )
            )
            idx += 1
        return chunks

    # 标题前的导言（若有）
    first_start = headings[0].start()
    if first_start > 0:
        preamble = md[:first_start].strip()
        for piece in _split_long_section(preamble, max_chars):
            offset = md.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section="导言",
                    content=piece,
                    char_offset=offset if offset >= 0 else 0,
                )
            )
            idx += 1

    for i, h in enumerate(headings):
        section_name = h.group(2).strip() or "正文"
        body_start = h.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(md)
        body = md[body_start:body_end].strip()
        if not body:
            continue
        for piece in _split_long_section(body, max_chars):
            # char_offset：piece 在原始 md 中的绝对偏移（md[offset:].startswith(piece)）
            offset = md.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section=section_name,
                    content=piece,
                    char_offset=offset if offset >= 0 else 0,
                )
            )
            idx += 1
    return chunks
