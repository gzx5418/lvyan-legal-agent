"""legal_answer_finalizer 节点：在 output_guardrail 之后重建结构化输出 + 渲染文书。

P0-1 修复（核心）：DOCX 渲染从 composer 移到此处。

原实现中 composer 在 output_guardrail 之前就调用 render_docx 把 DOCX 落盘，
导致最终文件可能包含：
  - 已被 citation_verifier 判定为虚假的法条；
  - 未被 output_guardrail 脱敏的手机号 / 身份证；
  - HITL 编辑前的旧正文。

现在 composer 只生成 Markdown 草稿 + document_payload（含 output_path /
template），本节点在 output_guardrail（以及 HITL 编辑恢复）之后执行，
基于 **最终的** ``final_output`` 渲染 DOCX，保证文件与页面展示完全一致。

结构化输出（legal_answer）部分：
1. 所有字符串字段经过隐私脱敏（与 final_output 一致）；
2. HITL 编辑后清空旧 legal_answer（编辑内容无法保证结构一致）；
3. 风险等级与 guardrail 结果一致；
4. 校验失败时不发送旧结构化数据（fail-safe 降级为 Markdown）。
"""
from __future__ import annotations

import logging
from typing import Any

from lvyan.schemas import CaseState

_logger = logging.getLogger("lvyan.nodes.legal_answer_finalizer")

__all__ = ["legal_answer_finalizer"]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """安全读取 dict 或 Pydantic 模型的属性。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _redact_string_fields(answer: dict[str, Any]) -> dict[str, Any]:
    """对 legal_answer dict 中所有字符串字段做隐私脱敏。"""
    from lvyan.validators.privacy import redact_privacy

    def _redact_recursive(obj: Any) -> Any:
        if isinstance(obj, str):
            result = redact_privacy(obj)
            return result.redacted_text
        if isinstance(obj, dict):
            return {k: _redact_recursive(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact_recursive(item) for item in obj]
        return obj

    return _redact_recursive(answer)


def _render_document_file(state: Any) -> dict[str, Any]:
    """P0-1：在 output_guardrail 之后基于最终 final_output 渲染 DOCX。

    从 ``state.document_payload`` 读取 output_path / template，用经过引用校验、
    隐私脱敏、HITL 编辑后的 ``final_output`` 调用 ``render_docx`` 落盘。

    返回更新字典：
        - ``final_output``: 追加「文书文件：...」页脚后的最终正文（仅成功时）
        - ``document_file``: 文件信息 dict（output_path / format / file_size /
          success / error）；payload 缺失时为 None
    """
    payload = _get(state, "document_payload", None)
    final_output = str(_get(state, "final_output", "") or "")

    if not isinstance(payload, dict) or not final_output:
        # 非 document 模式 / 无正文：不渲染，清空 document_file
        return {"document_file": None}

    fields = payload.get("filled_fields") or {}
    output_path = str(fields.get("output_path") or "")
    template = fields.get("template") or payload.get("template_name")

    # template_name 在无模板时是「XX（无模板，Markdown 降级）」，不是真实路径
    if isinstance(template, str) and not template.endswith(".docx"):
        template = None

    if not output_path:
        _logger.warning("document_payload 缺少 output_path，跳过 DOCX 渲染")
        return {"document_file": None}

    from lvyan.tools.export import render_docx

    try:
        result = render_docx(final_output, output_path, template)
        file_info: dict[str, Any] = {
            "output_path": result.output_path,
            "format": result.format,
            "file_size": result.file_size,
            "success": bool(result.success),
            "error": result.error or None,
        }
        # 在正文末尾追加文书文件信息（与原 composer 行为一致，供前端展示下载入口）
        if result.success:
            footer = (
                f"\n\n---\n文书文件：{result.output_path}（格式：{result.format}）"
            )
            if result.error:
                footer += f"\n⚠ {result.error}"
            final_output = final_output + footer
        else:
            _logger.warning("DOCX 渲染失败，仅保留 Markdown：%s", result.error)
        return {"final_output": final_output, "document_file": file_info}
    except Exception as exc:  # noqa: BLE001  渲染异常不中断流程
        _logger.warning("DOCX 渲染异常，仅保留 Markdown：%s", exc)
        return {
            "document_file": {
                "output_path": output_path,
                "format": "md",
                "file_size": 0,
                "success": False,
                "error": f"渲染异常：{exc}",
            }
        }


def legal_answer_finalizer(state: CaseState) -> dict[str, Any]:
    """在 output_guardrail 之后重建并校验结构化法律输出；document 模式渲染 DOCX。

    返回更新字典：
        - ``legal_answer``: dict | None（脱敏 + 校验后的结构化数据；document 模式、
          校验失败或 HITL 编辑后为 None，前端回退到 Markdown）
        - ``document_file``: dict | None（document 模式渲染的文件信息；其他模式 None）
        - ``final_output``: document 模式渲染成功时追加文书文件页脚
    """
    complexity = str(_get(state, "complexity", "light") or "light")

    # P0-1：Document 模式 —— 在 guardrail 之后渲染 DOCX（不构建 legal_answer）
    if complexity == "document":
        result = _render_document_file(state)
        result["legal_answer"] = None
        return result

    # 非 document 模式：清空 document_file，重建结构化 legal_answer
    base_result: dict[str, Any] = {"document_file": None}

    # P0-1：HITL 编辑后清空 legal_answer（编辑内容无法保证结构一致）
    pending_approval = _get(state, "pending_human_approval", None)
    if isinstance(pending_approval, dict) and pending_approval.get("status") == "edited":
        _logger.info("HITL 编辑后清空 legal_answer，前端将使用 Markdown 回退")
        base_result["legal_answer"] = None
        return base_result

    legal_answer_dict: dict[str, Any] | None = None
    try:
        from lvyan.nodes.answer_builder import build_legal_answer
        from lvyan.nodes.answer_validator import (
            ValidationError as AVError,
            validate_legal_answer,
        )

        cs = state if isinstance(state, CaseState) else CaseState.model_validate(state)
        answer = build_legal_answer(cs)

        # P0-1：用 guardrail 后的 risk_level 同步 meta
        final_risk = _get(state, "risk_level", cs.risk_level)
        if final_risk in ("low", "medium", "high"):
            answer.meta.risk_level = final_risk  # type: ignore[assignment]

        validate_legal_answer(answer)

        answer_dict = answer.model_dump(mode="json")

        # P0-1：对所有字符串字段做隐私脱敏（与 final_output 一致）
        answer_dict = _redact_string_fields(answer_dict)

        legal_answer_dict = answer_dict
    except AVError as exc:
        _logger.warning("legal_answer finalizer 校验失败，降级为 Markdown: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("legal_answer finalizer 构建失败，降级为 Markdown: %s", exc)

    base_result["legal_answer"] = legal_answer_dict
    return base_result
