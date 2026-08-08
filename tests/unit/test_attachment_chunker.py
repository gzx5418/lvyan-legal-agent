"""附件 Markdown 分块器测试。"""

from __future__ import annotations

from lvyan.tools.attachment_chunker import chunk_attachment_markdown


def test_splits_by_markdown_headings():
    md = "# 当事人\n甲方：张三\n\n# 租金条款\n月租金 3000 元。\n\n# 违约责任\n逾期付款违约金。"
    chunks = chunk_attachment_markdown(md, document_id="f1", document_name="lease.md")
    sections = [c.section for c in chunks]
    assert sections == ["当事人", "租金条款", "违约责任"]
    assert all(c.document_id == "f1" for c in chunks)
    assert chunks[0].content == "甲方：张三"
    # char_offset 是原始 md 中的绝对偏移：md[offset:].startswith(content)
    assert md[chunks[0].char_offset :].startswith(chunks[0].content)


def test_long_section_split_by_paragraph():
    para = "第一句内容。" * 200
    md = f"# 正文\n{para}"
    chunks = chunk_attachment_markdown(md, document_id="f1", document_name="x.md", max_chars=120)
    assert len(chunks) > 1
    assert all(len(c.content) <= 120 + 20 for c in chunks)


def test_empty_markdown_returns_empty():
    assert chunk_attachment_markdown("", "f1", "x.md") == []


def test_no_heading_treated_as_body_section():
    chunks = chunk_attachment_markdown("纯正文，无标题。", "f1", "x.md")
    assert len(chunks) == 1
    assert chunks[0].section == "正文"
