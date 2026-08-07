"""P0-1 回归测试：DOCX 必须在 output_guardrail 之后渲染，且与 final_output 一致。

验证核心不变量：
1. composer 不再直接渲染 DOCX（不写文件、document_file=None）；
2. legal_answer_finalizer 在 guardrail 之后基于 **最终** final_output 渲染 DOCX；
3. 当 final_output 在 guardrail 阶段被修改（脱敏 / 删除虚假法条 / HITL 编辑）后，
   渲染到 DOCX 的是修改后的内容，而非 composer 初稿。
"""
from __future__ import annotations

from datetime import date

import pytest

from lvyan.nodes.composer import composer
from lvyan.nodes.legal_answer_finalizer import legal_answer_finalizer
from lvyan.schemas import CaseState


def _make_base_state(**overrides) -> dict:
    """构造 document 模式的 state dict。"""
    defaults = {
        "run_id": "run-p0-1-doc-test",
        "thread_id": "thread-p0-1",
        "current_date": date(2026, 8, 7),
        "user_goal": "我要起诉对方违约，请帮我写起诉状",
        "jurisdiction": "中国大陆",
        "case_type": "合同纠纷",
        "complexity": "document",
        "facts": [],
        "disputed_facts": [],
        "timeline": [],
        "missing_facts": [],
        "uploaded_documents": [],
        "plan": [],
        "retrieval_queries": [],
        "statutes": [],
        "cases": [],
        "evidence_requirements": [],
        "conflicts": [],
        "reasoning_result": None,
        "citation_audit": None,
        "risk_level": "low",
        "confidence": "medium",
        "iteration": 0,
        "final_output": None,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# 1. composer 不再渲染 DOCX
# ---------------------------------------------------------------------------
def test_composer_does_not_render_docx(monkeypatch, tmp_path):
    """composer 不应调用 render_docx，也不应写入 document_file。"""
    called = {"render_docx": 0}

    import lvyan.tools.export as export_mod

    def _fake_render_docx(markdown_text, output_path, template=None):
        called["render_docx"] += 1
        # 即使被调用也不应真正写文件
        return export_mod.ExportResult(
            tool_name="render_docx",
            success=True,
            output_path=output_path,
            format="docx",
            file_size=0,
        )

    monkeypatch.setattr(export_mod, "render_docx", _fake_render_docx)

    state = _make_base_state()
    result = composer(state)

    assert called["render_docx"] == 0, "composer 不应再调用 render_docx"
    assert result.get("document_file") is None
    # document_payload 仍应包含 output_path / template，供 finalizer 使用
    payload = result["document_payload"]
    assert payload is not None
    assert "output_path" in payload["filled_fields"]
    assert "template" in payload["filled_fields"]


# ---------------------------------------------------------------------------
# 2. finalizer 在 guardrail 之后基于 final_output 渲染 DOCX
# ---------------------------------------------------------------------------
def test_finalizer_renders_docx_from_final_output(monkeypatch):
    """finalizer 应基于 state.final_output 调用 render_docx。"""
    captured = {}

    import lvyan.nodes.legal_answer_finalizer as fin_mod

    def _fake_render(markdown_text, output_path, template=None):
        captured["markdown"] = markdown_text
        captured["output_path"] = output_path
        captured["template"] = template
        from lvyan.tools.export import ExportResult

        return ExportResult(
            tool_name="render_docx",
            success=True,
            output_path=output_path,
            format="docx",
            file_size=123,
        )

    # render_docx 在函数内部 import，patch 源模块
    import lvyan.tools.export as export_mod

    monkeypatch.setattr(export_mod, "render_docx", _fake_render)

    # 模拟 composer 已生成 payload，guardrail 已修改 final_output
    state = _make_base_state()
    composer_result = composer(state)
    state["document_payload"] = composer_result["document_payload"]
    # 模拟 output_guardrail 在 final_output 末尾追加了高风险声明
    state["final_output"] = composer_result["final_output"] + "\n\n高风险声明"

    result = legal_answer_finalizer(state)

    assert captured.get("markdown") is not None
    assert "高风险声明" in captured["markdown"], "渲染的应是 guardrail 修改后的 final_output"
    assert result["legal_answer"] is None
    doc_file = result["document_file"]
    assert doc_file is not None
    assert doc_file["success"] is True
    assert doc_file["format"] == "docx"
    assert doc_file["file_size"] == 123
    # final_output 应被追加「文书文件」页脚
    assert "文书文件" in result["final_output"]


# ---------------------------------------------------------------------------
# 3. 核心不变量：guardrail 修改后的内容才是渲染源（非 composer 初稿）
# ---------------------------------------------------------------------------
def test_docx_uses_post_guardrail_content_not_draft(monkeypatch):
    """若 guardrail 删除了虚假法条 / 脱敏了手机号，DOCX 应反映修改后内容。"""
    captured = {}

    import lvyan.tools.export as export_mod

    def _fake_render(markdown_text, output_path, template=None):
        captured["markdown"] = markdown_text
        from lvyan.tools.export import ExportResult

        return ExportResult(
            tool_name="render_docx",
            success=True,
            output_path=output_path,
            format="docx",
            file_size=10,
        )

    monkeypatch.setattr(export_mod, "render_docx", _fake_render)

    state = _make_base_state()
    composer_result = composer(state)
    state["document_payload"] = composer_result["document_payload"]

    # composer 初稿含原始手机号；guardrail 后 final_output 已脱敏
    draft = composer_result["final_output"]
    assert "13800138000" not in draft  # 初稿本就不含，确保下面断言有意义
    state["final_output"] = draft + "\n\n联系电话：138****8000（已脱敏）"

    legal_answer_finalizer(state)

    rendered = captured["markdown"]
    # 渲染内容必须包含脱敏后的信息
    assert "138****8000" in rendered
    # 渲染源就是 final_output（含追加的脱敏行）
    assert rendered.endswith("联系电话：138****8000（已脱敏）")


# ---------------------------------------------------------------------------
# 4. finalizer 缺失 payload / final_output 时不崩溃
# ---------------------------------------------------------------------------
def test_finalizer_document_mode_without_payload_returns_none():
    """document 模式但无 document_payload（异常路径）应安全返回 document_file=None。"""
    state = _make_base_state(final_output="某些正文")
    # 不设置 document_payload
    result = legal_answer_finalizer(state)
    assert result["document_file"] is None
    assert result["legal_answer"] is None


def test_finalizer_non_document_mode_clears_document_file():
    """非 document 模式应清空 document_file（防止残留过期文件引用）。"""
    state = _make_base_state(complexity="deep", final_output="deep 报告正文")
    result = legal_answer_finalizer(state)
    assert result["document_file"] is None
