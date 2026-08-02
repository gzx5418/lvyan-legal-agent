"""legal_answer_finalizer 节点：在 output_guardrail 之后重建结构化输出。

P0-1 修复：原实现中 composer 在 output_guardrail 之前构建 legal_answer，
导致结构化数据未经隐私脱敏、引用修复、风险调整，与 Markdown 不一致。

本节点在 output_guardrail 完成后执行，确保：
1. 所有结构化字符串字段经过隐私脱敏（与 final_output 一致）；
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


def legal_answer_finalizer(state: CaseState) -> dict[str, Any]:
    """在 output_guardrail 之后重建并校验结构化法律输出。

    返回更新字典：
        - ``legal_answer``: dict | None（脱敏 + 校验后的结构化数据；校验失败或
          HITL 编辑后为 None，前端回退到 Markdown）
    """
    complexity = str(_get(state, "complexity", "light") or "light")

    # P0-2：Document 模式不构建 legal_answer，避免覆盖文书输出
    if complexity == "document":
        return {"legal_answer": None}

    # P0-1：HITL 编辑后清空 legal_answer（编辑内容无法保证结构一致）
    pending_approval = _get(state, "pending_human_approval", None)
    if isinstance(pending_approval, dict) and pending_approval.get("status") == "edited":
        _logger.info("HITL 编辑后清空 legal_answer，前端将使用 Markdown 回退")
        return {"legal_answer": None}

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

    return {"legal_answer": legal_answer_dict}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """安全读取 dict 或 Pydantic 模型的属性。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
