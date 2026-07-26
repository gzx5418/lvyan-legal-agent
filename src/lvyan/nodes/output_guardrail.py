"""输出守卫节点：隐私脱敏 + 结构校验 + 数字概率拦截 + Human-in-the-loop。

职责
----
1. 调用 :func:`lvyan.validators.privacy.redact_privacy` 对 ``final_output`` 进行
   隐私脱敏，替换 ``final_output``。
2. 调用 :func:`lvyan.validators.output.validate_output` 进行结构 / 引用 / 风险声明 /
   数字概率校验。
3. 按错误类型自动修复或回退：
   - ``numeric_probability`` → 直接拦截，移除数字概率并替换为定性标签。
   - ``missing_risk_disclaimer`` → 自动追加标准风险声明。
   - ``invalid_citation`` → 移除该引用并标注警告。
   - ``missing_section`` / ``missing_citation`` → 回退到 ``composer`` 重新生成
     （``output_iteration < MAX_OUTPUT_ITERATIONS``）；达到上限则强制放行并追加警告。
4. **Human-in-the-loop**：检测输出中是否涉及不可逆操作建议（发送律师函 / 提交法院 /
   代表用户联系 / 修改原始合同 / 删除案件材料 / 对外共享证据 / 提起诉讼）。
   命中则写入 ``state["pending_human_approval"]``，并在输出中标注
   「⚠ 以下操作需要您确认后才能执行」。
5. 若 ``risk_level == "high"`` 且输出未含高风险声明，追加高风险声明。

路由
----
``route_after_output_guardrail``：
- ``output_retry_needed`` 为 True → 回退 ``composer``
- 否则 → ``END``

公开接口
--------
    output_guardrail(state) -> dict[str, Any]
"""

from __future__ import annotations

import re
from typing import Any

from langgraph.types import interrupt

from lvyan.config import settings
from lvyan.schemas import CaseState
from lvyan.validators.output import (
    STANDARD_RISK_DISCLAIMER,
    OutputValidationResult,
    validate_output,
)
from lvyan.validators.privacy import redact_privacy

__all__ = ["output_guardrail", "MAX_OUTPUT_ITERATIONS"]


# 输出回退最大重试次数（建议 2）
MAX_OUTPUT_ITERATIONS: int = 2

# 高风险声明（与 composer 保持一致，guardrail 兜底追加）
_HIGH_RISK_DISCLAIMER: str = (
    "\n\n---\n⚠ 高风险声明：本案风险等级较高，上述结论存在较大不确定性，"
    "建议尽快咨询持证律师并收集补强证据，切勿仅凭本意见作出不可逆决定。"
)

# 不可逆操作关键词（命中即触发 Human-in-the-loop）
# Task 18.4 安全评测补全：新增「签署合同 / 签署协议 / 代为签署」等签署类敏感动作
_IRREVERSIBLE_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("发送律师函", "发送律师函"),
    ("提交法院", "向法院提交材料"),
    ("提起诉讼", "提起诉讼"),
    ("代表用户联系", "代表用户联系第三方"),
    ("联系第三方", "代表用户联系第三方"),
    ("修改原始合同", "修改原始合同"),
    ("删除案件材料", "删除案件材料"),
    ("对外共享证据", "对外共享证据"),
    ("签署合同", "签署合同/协议"),
    ("签署协议", "签署合同/协议"),
    ("代为签署", "代为签署"),
)

# 数字概率移除模式（与 validators.output 一致）
_NUMERIC_PROBABILITY_RES = tuple(
    (
        re.compile(r"\d+(?:\.\d+)?\s*[%％]"),
        re.compile(r"\d+(?:\.\d+)?\s*(?:概率|胜诉率|胜率|胜诉概率)"),
        re.compile(r"(?:概率|胜诉率|胜率|胜诉概率)\s*[:：]?\s*\d+(?:\.\d+)?"),
    )
)

# 引用行移除模式：匹配以「- 《...》第...条」或「《...》第...条」开头的行
_CITATION_LINE_RE = re.compile(
    r"^-?[ \t]*《[^》]+》第[一二三四五六七八九十百千零0-9]+条.*$(?:\n^[ \t]+.*$)*",
    re.MULTILINE,
)
# 单行内引用片段移除
_CITATION_INLINE_RE = re.compile(
    r"《[^》]+》第[一二三四五六七八九十百千零0-9]+条[^，,。.；;\n]*[，,。.；;]?"
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _remove_numeric_probability(text: str) -> str:
    """移除数字概率表达，替换为定性标签「信息不足（未校准）」。"""
    result = text
    for pattern in _NUMERIC_PROBABILITY_RES:
        result = pattern.sub("信息不足（未校准）", result)
    return result


def _remove_invalid_citation(text: str, detail: str) -> str:
    """移除输出中无效的法条引用。

    优先移除整行引用块；若无整行匹配，则移除行内引用片段。每次仅移除一处
    （与 detail 对应），并在末尾追加警告备注。
    """
    # 尝试从 detail 提取引用文本，如「《民法典》第9999条」
    citation_match = re.search(r"《[^》]+》第[一二三四五六七八九十百千零0-9]+条", detail)
    if not citation_match:
        # 无法定位具体引用，仅追加警告
        return text

    citation_text = citation_match.group(0)
    # 行块移除
    line_pattern = re.compile(
        r"^-?[ \t]*" + re.escape(citation_text) + r".*$(?:\n^[ \t]+.*$)*",
        re.MULTILINE,
    )
    new_text, n = line_pattern.subn("", text)
    if n:
        return new_text

    # 行内移除
    inline_pattern = re.compile(
        re.escape(citation_text) + r"[^，,。.；;\n]*[，,。.；;]?"
    )
    new_text, n = inline_pattern.subn("", text, count=1)
    return new_text


def _detect_irreversible_ops(text: str) -> list[str]:
    """检测输出中涉及的不可逆操作，返回去重后的操作描述列表。"""
    ops: list[str] = []
    seen: set[str] = set()
    for keyword, description in _IRREVERSIBLE_OPERATIONS:
        if keyword in text and description not in seen:
            ops.append(description)
            seen.add(description)
    return ops


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def output_guardrail(state: CaseState) -> dict[str, Any]:
    """输出守卫节点。

    返回更新字典（覆盖语义）：
        - ``final_output``: str（脱敏 + 修复后的最终输出）
        - ``output_retry_needed``: bool（是否回退 composer）
        - ``output_iteration``: int（回退时 +1）
        - ``pending_human_approval``: dict | None（HITL 待审批）
        - ``risk_level``: str（追加高风险声明时可能上调，但不主动修改）
    """
    final_output = str(_get(state, "final_output", "") or "")
    complexity = str(_get(state, "complexity", "light") or "light")
    statutes = _get(state, "statutes", []) or []
    risk_level = str(_get(state, "risk_level", "low") or "low")
    output_iteration = int(_get(state, "output_iteration", 0) or 0)

    notes: list[str] = []

    # --- 1. 隐私脱敏 ---
    privacy_result = redact_privacy(final_output)
    final_output = privacy_result.redacted_text
    if privacy_result.redaction_count:
        notes.append(
            f"已对输出进行隐私脱敏（共 {privacy_result.redaction_count} 处："
            f"{privacy_result.redaction_types}）"
        )

    # --- 2. 结构 / 引用 / 风险声明 / 数字概率校验 ---
    validation: OutputValidationResult = validate_output(
        final_output, complexity, statutes
    )

    # --- 3. 按错误类型自动修复或标记回退 ---
    retry_needed = False
    retry_reasons: list[str] = []

    for err in validation.errors:
        if err.error_type == "numeric_probability":
            final_output = _remove_numeric_probability(final_output)
            notes.append("已拦截并移除数字概率表达，替换为定性标签")
        elif err.error_type == "missing_risk_disclaimer":
            final_output = final_output.rstrip() + "\n\n" + STANDARD_RISK_DISCLAIMER
            notes.append("已自动追加标准风险声明")
        elif err.error_type == "invalid_citation":
            final_output = _remove_invalid_citation(final_output, err.detail)
            notes.append(f"已移除无效引用并标注警告：{err.detail}")
        elif err.error_type in ("missing_section", "missing_citation"):
            # 结构性缺失：回退 composer 重写（受 MAX_OUTPUT_ITERATIONS 约束）
            if output_iteration < MAX_OUTPUT_ITERATIONS:
                retry_needed = True
                retry_reasons.append(err.detail)
            else:
                notes.append(
                    f"已达输出重试上限，强制放行；遗留问题：{err.detail}（需人工复核）"
                )

    # --- 4. 回退 composer 重新生成 ---
    if retry_needed:
        notes.append(
            f"输出校验未通过（{'; '.join(retry_reasons)}），回退 composer 重新生成"
            f"（第 {output_iteration + 1} 次）"
        )
        final_output = final_output + "\n\n---\n校验备注：" + "；".join(notes)
        return {
            "final_output": final_output,
            "output_iteration": output_iteration + 1,
            "output_retry_needed": True,
        }

    # --- 5. Human-in-the-loop：不可逆操作检测 ---
    irreversible_ops = _detect_irreversible_ops(final_output)
    pending_human_approval: dict | None = None
    if irreversible_ops:
        pending_human_approval = {
            "operations": irreversible_ops,
            "message": "以下操作需要您确认后才能执行",
            "status": "pending",
        }
        if settings.hitl_enabled:
            # 通过 LangGraph interrupt 暂停图执行，等待用户批准 / 编辑 / 拒绝
            user_response = interrupt(
                {
                    "operations": irreversible_ops,
                    "message": (
                        "以下操作需要您确认后才能执行："
                        + "；".join(irreversible_ops)
                        + "。请回复 action=approve / action=reject / "
                        "action=edit（后者附带 edited_output 字段）。"
                    ),
                    "draft_output": final_output,
                }
            )

            # P0-2 修复：统一解析结构化响应（dict）和兼容字符串
            if isinstance(user_response, dict):
                action = user_response.get("action")
                edited_output = user_response.get("edited_output")
            elif isinstance(user_response, str):
                # 兼容旧式字符串响应：approve / reject / 视作 edit
                action = user_response.strip()
                edited_output = None
                if action not in ("approve", "reject"):
                    # 非明确 approve/reject 的字符串 → 当作 edit 内容
                    edited_output = action
                    action = "edit"
            else:
                action = "approve"
                edited_output = None

            # 拒绝：保留原分析正文，仅追加拒绝提示与状态标记
            if action == "reject":
                final_output = (
                    final_output
                    + "\n\n---\n⚠ 您已拒绝执行上述操作。Agent 不会自动执行。"
                )
                notes.append("用户已拒绝不可逆操作，原分析正文已保留")
                pending_human_approval["status"] = "rejected"
                # 直接进入最终输出阶段（跳过下面的「待确认」拼接）
                pending_human_approval = pending_human_approval  # noqa: B018

            elif action == "edit":
                if not edited_output:
                    raise ValueError("edit 操作缺少 edited_output")
                final_output = str(edited_output)
                pending_human_approval["status"] = "edited"
                notes.append("用户已编辑输出，已替换 final_output")

            elif action == "approve":
                pending_human_approval["status"] = "approved"
                notes.append("用户已批准不可逆操作")

            else:
                raise ValueError(f"未知 HITL action: {action}")

        # P1-2 修复：仅在 HITL 未启用（status 仍为 pending）时追加确认提示；
        # 已批准/拒绝/编辑后不应再要求确认
        if pending_human_approval.get("status") == "pending":
            final_output = (
                final_output
                + "\n\n---\n⚠ 以下操作需要您确认后才能执行："
                + "；".join(irreversible_ops)
                + "。Agent 不会自动执行，请回复确认后再处理。"
            )
            notes.append("检测到不可逆操作建议，已写入 pending_human_approval")

    # --- 6. 高风险声明兜底 ---
    if risk_level == "high" and "高风险声明" not in final_output:
        final_output = final_output + _HIGH_RISK_DISCLAIMER
        notes.append("本案风险等级较高，已追加高风险声明")

    # --- 6.5 最终校验：脱敏 + 修复后再跑一次完整校验 ---
    final_validation = validate_output(final_output, complexity, statutes)
    if not final_validation.passed:
        # 最终校验仍不通过 → 上调风险等级并附警告
        risk_level = "high"
        remaining_issues = "; ".join(e.detail for e in final_validation.errors)
        notes.append(f"最终校验未通过，已上调风险等级为 high：{remaining_issues}")

    # --- 7. 追加校验备注 ---
    if notes:
        final_output = final_output + "\n\n---\n校验备注：" + "；".join(notes)

    result: dict[str, Any] = {
        "final_output": final_output,
        "output_retry_needed": False,
    }
    if pending_human_approval is not None:
        result["pending_human_approval"] = pending_human_approval
    if risk_level == "high":
        result["risk_level"] = "high"
    return result
