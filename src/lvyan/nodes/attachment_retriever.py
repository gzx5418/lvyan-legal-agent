"""附件按需检索节点：把附件切块、按 user_goal 排序，写入紧凑上下文。

取代「把附件全文塞进 user_goal」的旧做法 —— 下游 LLM 节点只看到与问题相关的
若干分块，且总字符数受 ``max_context_chars`` 控制。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lvyan.retrieval.attachment_ranker import rank_chunks
from lvyan.schemas.attachment import AttachmentChunk
from lvyan.tools.attachment_chunker import chunk_attachment_markdown

__all__ = ["attachment_retriever"]

_logger = logging.getLogger("lvyan.nodes.attachment_retriever")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _load_markdown(stored_path: str) -> str:
    """从 stored_path 读取 markdown 正文；失败返回空串。"""
    try:
        p = Path(stored_path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _logger.warning("附件正文读取失败 (%s): %s", stored_path, exc)
        return ""


def attachment_retriever(
    state: Any,
    top_k_per_doc: int = 4,
    max_context_chars: int = 6000,
) -> dict[str, Any]:
    """读取 uploaded_documents → 切块 → 排序 → 写 relevant_attachment_context。

    返回 ``{"relevant_attachment_context": str}``（覆盖语义）。
    无附件或读取失败时写入空串（等价于无相关材料）。
    """
    user_goal = str(_get(state, "user_goal", "") or "")
    docs = _get(state, "uploaded_documents", []) or []

    if not docs or not user_goal.strip():
        return {"relevant_attachment_context": ""}

    all_chunks: list[AttachmentChunk] = []
    for doc in docs:
        doc_id = str(_get(doc, "doc_id", "") or _get(doc, "filename", "doc"))
        filename = str(_get(doc, "filename", doc_id))
        stored_path = str(_get(doc, "stored_path", "") or "")
        if not stored_path:
            continue
        md = _load_markdown(stored_path)
        if not md.strip():
            continue
        chunks = chunk_attachment_markdown(md, document_id=doc_id, document_name=filename)
        ranked = rank_chunks(user_goal, chunks, top_k=top_k_per_doc)
        all_chunks.extend(ranked)

    if not all_chunks:
        return {"relevant_attachment_context": ""}

    # 跨文档再排序一次，按相关度取最终 top，并截断到 max_context_chars
    final_ranked = rank_chunks(user_goal, all_chunks, top_k=len(all_chunks))
    parts: list[str] = []
    used = 0
    for ch in final_ranked:
        block = f"【{ch.document_name} · {ch.section}】\n{ch.content}"
        if used + len(block) > max_context_chars:
            break
        parts.append(block)
        used += len(block) + 2

    return {"relevant_attachment_context": "\n\n".join(parts)}
