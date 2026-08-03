"""attachment_retriever 节点测试。"""
from __future__ import annotations

from lvyan.nodes.attachment_retriever import attachment_retriever


def test_no_documents_returns_empty_context():
    state = {"user_goal": "问题", "uploaded_documents": []}
    result = attachment_retriever(state)
    assert result == {"relevant_attachment_context": ""}


def test_builds_context_from_uploaded_documents(tmp_path):
    md_file = tmp_path / "lease.md"
    md_file.write_text(
        "# 当事人\n甲方张三。\n\n# 押金条款\n押金 3000 元，租期届满返还。\n\n# 物业\n物业费缴纳。",
        encoding="utf-8",
    )
    doc = {
        "doc_id": "f1",
        "filename": "lease.md",
        "doc_type": "contract",
        "content_hash": "h",
        "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    state = {"user_goal": "房东不退押金", "uploaded_documents": [doc]}
    result = attachment_retriever(state, max_context_chars=2000)
    ctx = result["relevant_attachment_context"]
    assert "押金" in ctx
    assert ctx.index("押金") < ctx.index("物业") if "物业" in ctx else True


def test_context_respects_char_cap(tmp_path):
    md_file = tmp_path / "big.md"
    md_file.write_text("# 押金\n" + "押金。 " * 500, encoding="utf-8")
    doc = {
        "doc_id": "f1", "filename": "big.md", "doc_type": "contract",
        "content_hash": "h", "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    result = attachment_retriever(
        {"user_goal": "押金", "uploaded_documents": [doc]}, max_context_chars=300
    )
    assert len(result["relevant_attachment_context"]) <= 300


def test_stored_path_is_read_directly(tmp_path):
    md_file = tmp_path / "doc.md"
    md_file.write_text("# 押金\n押金 3000 元。", encoding="utf-8")
    doc = {
        "doc_id": "f1", "filename": "doc.md", "doc_type": "contract",
        "content_hash": "h", "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    result = attachment_retriever({"user_goal": "押金", "uploaded_documents": [doc]})
    assert "押金 3000" in result["relevant_attachment_context"]
