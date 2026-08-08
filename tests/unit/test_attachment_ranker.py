"""附件分块排序器测试。"""

from __future__ import annotations

from lvyan.retrieval.attachment_ranker import rank_chunks
from lvyan.schemas.attachment import AttachmentChunk


def _chunk(cid: str, content: str) -> AttachmentChunk:
    return AttachmentChunk(
        chunk_id=cid,
        document_id="f1",
        document_name="x.md",
        section="正文",
        content=content,
        char_offset=0,
    )


def test_ranks_relevant_chunk_first():
    chunks = [
        _chunk("c0", "通用条款，约定争议解决方式。"),
        _chunk("c1", "押金 3000 元，租期届满应予返还。"),
        _chunk("c2", "物业费缴纳说明。"),
    ]
    ranked = rank_chunks("房东不退押金怎么办", chunks, top_k=2)
    assert ranked[0].chunk_id == "c1"
    assert len(ranked) == 2


def test_empty_chunks_returns_empty():
    assert rank_chunks("q", [], top_k=5) == []


def test_top_k_respected():
    chunks = [_chunk(f"c{i}", f"押金 押金 {i}") for i in range(10)]
    ranked = rank_chunks("押金", chunks, top_k=3)
    assert len(ranked) == 3


def test_no_token_overlap_returns_input_order():
    chunks = [_chunk("c0", "aaa"), _chunk("c1", "bbb")]
    ranked = rank_chunks("zzz", chunks, top_k=5)
    assert [c.chunk_id for c in ranked] == ["c0", "c1"]
