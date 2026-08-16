"""组装节点：选择 light/deep/document 渲染器并应用最终输出策略。

各模式的具体渲染逻辑分别位于 composer_light、composer_deep 和
composer_document，本模块保留稳定的图节点公开入口。
"""

from __future__ import annotations

import logging
from typing import Any

from lvyan.common.constants import HIGH_RISK_DISCLAIMER
from lvyan.common.helpers import get_value as _get
from lvyan.nodes.composer_common import CITATION_AUDIT_WARNING, format_online_sources
from lvyan.nodes.composer_deep import compose_deep
from lvyan.nodes.composer_document import compose_document, doc_title
from lvyan.nodes.composer_light import compose_light
from lvyan.schemas import CaseState

_logger = logging.getLogger("lvyan.nodes.composer")

# 兼容仍从本模块导入下划线符号的调用方
_doc_title = doc_title
_CITATION_AUDIT_WARNING = CITATION_AUDIT_WARNING

__all__ = ["composer", "doc_title", "_doc_title"]


def composer(state: CaseState) -> dict[str, Any]:
    """组装节点：按 complexity 模式组装最终意见正文。

    返回更新字典（覆盖语义）：
        - ``final_output``: str（组装后的最终输出）
        - ``document_payload``: dict | None（document 模式的文书载荷，含
          ``template_name`` + ``filled_fields``；非 document 模式为 None）
        - ``document_file``: None（composer 不再渲染文件；由 finalizer 写入）
        - ``legal_answer``: dict | None（结构化输出初稿，由 finalizer 覆盖）
    """
    complexity = str(_get(state, "complexity", "light") or "light")
    preferences = _get(state, "user_preferences", {}) or {}
    response_style = str(_get(preferences, "response_style", "brief") or "brief")
    # Preference controls presentation depth, not triage/retrieval/reasoning.
    # A user asking for detailed answers receives the deep renderer even when
    # the adaptive classifier selected a light analysis path.
    if response_style == "detailed" and complexity == "light":
        complexity = "deep"
    risk_level = str(_get(state, "risk_level", "low") or "low")
    citation_audit = _get(state, "citation_audit", None)

    # 1. 按模式组装
    document_payload: dict[str, Any] | None = None
    if complexity == "deep":
        output = compose_deep(state)
    elif complexity == "document":
        output, document_payload = compose_document(state)
    else:
        output = compose_light(state)

    # 2. citation_audit 未通过 → 开头加显著警告
    audit_passed = _get(citation_audit, "passed", True)
    if audit_passed is False:
        output = CITATION_AUDIT_WARNING + output

    # 3. risk_level == high → 末尾追加高风险声明
    if risk_level == "high" and "高风险声明" not in output:
        output = output + HIGH_RISK_DISCLAIMER

    # 4. 联网结果独立展示，绝不混入已校验的法条引用。
    online_section = format_online_sources(_get(state, "online_sources", []) or [])
    if online_section:
        output = output.rstrip() + "\n\n" + online_section

    # 5. 结构化输出：构建 LegalAnswerV1 并校验（与 final_output 并行）
    # P0-2：document 模式不构建 legal_answer，避免结构化分析页覆盖文书输出。
    #    document 模式的 Markdown 包含文书正文 + DOCX 信息，LegalAnswerV1
    #    无法承载，应让前端继续展示 Markdown。
    # P0-1：composer 在 output_guardrail 之前构建，此处的 legal_answer 是
    #    未脱敏的初稿。真正的结构化输出由 legal_answer_finalizer 节点在
    #    output_guardrail 之后重建。此处仍保留构建（供 checkpoint 恢复等
    #    非标准路径兜底），但 finalizer 会覆盖它。
    legal_answer_dict: dict[str, Any] | None = None
    if complexity != "document":
        try:
            from lvyan.nodes.answer_builder import build_legal_answer
            from lvyan.nodes.answer_validator import (
                ValidationError as AVError,
                validate_legal_answer,
            )

            cs = state if isinstance(state, CaseState) else CaseState.model_validate(state)
            answer = build_legal_answer(cs)
            validate_legal_answer(answer)
            legal_answer_dict = answer.model_dump(mode="json")
        except AVError as exc:
            _logger.warning("legal_answer 校验失败，仅返回 Markdown: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("legal_answer 构建失败，仅返回 Markdown: %s", exc)

    result: dict[str, Any] = {
        "final_output": output,
        "document_payload": document_payload,
        # P0-1：清空旧 document_file，确保重试 composer 时不会残留过期文件引用。
        # 真正的文件由 legal_answer_finalizer 在 guardrail 之后写入。
        "document_file": None,
        "legal_answer": legal_answer_dict,
    }
    return result
