"""律言标准工具 MCP Server。

仅暴露有界、可审计的法律工具。文件读取使用 ``file_id``，导出目录固定在
``data/exports``，从协议层阻止任意路径读取/覆盖。类案工具继续使用当前精编规则库，
不会伪装成真实裁判文书数据库。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from lvyan.config import AGENT_DIR
from lvyan.memory.case_vault import CaseVault
from lvyan.tools import (
    analyze_contract_clause,
    build_case_timeline,
    calculate_claim_amount,
    calculate_legal_deadline,
    generate_evidence_checklist,
    get_case_detail,
    get_statute_article,
    render_docx,
    search_cases,
    search_procedure_rules,
    search_statutes,
    verify_statute_status,
)
from lvyan.tools.documents import extract_document

_UPLOAD_DIR = (AGENT_DIR / "data" / "uploads").resolve()
_EXPORT_DIR = (AGENT_DIR / "data" / "exports").resolve()
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {"success": True, "result": value}


def _uploaded_metadata(file_id: str) -> dict[str, Any]:
    if not _SAFE_ID.fullmatch(file_id):
        raise ValueError("非法 file_id")
    meta_path = (_UPLOAD_DIR / f"{file_id}.json").resolve()
    meta_path.relative_to(_UPLOAD_DIR)
    if not meta_path.is_file():
        raise FileNotFoundError("上传文件不存在")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("上传元数据格式无效")
    return meta


def _uploaded_raw_path(file_id: str) -> Path:
    meta = _uploaded_metadata(file_id)
    raw = Path(str(meta.get("raw_path", ""))).resolve()
    raw.relative_to(_UPLOAD_DIR)
    if not raw.is_file():
        raise FileNotFoundError("上传文件正文不存在")
    return raw


def extract_uploaded_document(file_id: str) -> dict[str, Any]:
    """按上传 ID 解析文档，拒绝 MCP 调用方提交任意本地路径。"""
    meta = _uploaded_metadata(file_id)
    markdown_ref = str(meta.get("markdown_path", ""))
    if markdown_ref.startswith("vault://"):
        try:
            thread_id, doc_id = markdown_ref[len("vault://") :].split("/", 1)
        except ValueError as exc:
            raise ValueError("上传文件的加密引用无效") from exc
        content = CaseVault().retrieve(thread_id, doc_id)
        if content is None:
            raise FileNotFoundError("上传文件不存在或已过期")
        return {
            "success": True,
            "file_id": file_id,
            "filename": str(meta.get("filename", file_id)),
            "markdown": content.decode("utf-8", errors="replace"),
            "char_count": int(meta.get("char_count", 0)),
            "source": "case_vault",
        }
    return _dump(extract_document(str(_uploaded_raw_path(file_id))))


def render_docx_safe(markdown_text: str, filename: str = "legal_document.docx") -> dict[str, Any]:
    """把 Markdown 导出到固定目录，文件名只允许安全字符。"""
    safe = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", filename)[:120]
    if not safe.lower().endswith(".docx"):
        safe += ".docx"
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = (_EXPORT_DIR / safe).resolve()
    output.relative_to(_EXPORT_DIR)
    return _dump(render_docx(markdown_text, str(output)))


def _registry() -> dict[str, Callable[..., dict[str, Any]]]:
    """供测试与非 MCP 调用方复用的纯 Python 工具注册表。"""
    return {
        "search_statutes": lambda query, as_of=None, top_k=10: _dump(
            search_statutes(query, as_of=as_of, top_k=top_k)
        ),
        "get_statute_article": lambda source_id, article_number: _dump(
            get_statute_article(source_id, article_number)
        ),
        "verify_statute_status": lambda source_id, as_of=None: _dump(
            verify_statute_status(source_id, as_of=as_of)
        ),
        "search_procedure_rules": lambda query, as_of=None, top_k=10: _dump(
            search_procedure_rules(query, as_of=as_of, top_k=top_k)
        ),
        "search_cases": lambda query, top_k=10: _dump(search_cases(query, top_k=top_k)),
        "get_case_detail": lambda case_id: _dump(get_case_detail(case_id)),
        "extract_document": extract_uploaded_document,
        "analyze_contract_clause": lambda clause_text, clause_type=None: _dump(
            analyze_contract_clause(clause_text, clause_type=clause_type)
        ),
        "build_case_timeline": lambda events: _dump(build_case_timeline(events)),
        "calculate_legal_deadline": lambda **kwargs: _dump(calculate_legal_deadline(**kwargs)),
        "calculate_claim_amount": lambda **kwargs: _dump(calculate_claim_amount(**kwargs)),
        "generate_evidence_checklist": lambda case_type, facts=None: _dump(
            generate_evidence_checklist(case_type, facts or [])
        ),
        "render_docx": render_docx_safe,
    }


def build_mcp_server() -> Any:
    """构建 FastMCP 实例；导入延迟到启动阶段，核心 Runtime 可独立使用。"""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("lvyan-legal-tools")
    for name, func in _registry().items():
        server.tool(name=name)(func)
    return server


def main() -> None:
    build_mcp_server().run(transport="stdio")


__all__ = ["build_mcp_server", "extract_uploaded_document", "render_docx_safe", "_registry"]


if __name__ == "__main__":
    main()
