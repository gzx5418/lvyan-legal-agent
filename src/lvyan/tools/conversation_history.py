"""对话历史格式化器：把 thread 的 messages 列表压成紧凑摘要字符串。

供 default_runner 在 run 开始时构造 ``conversation_summary``，让 LLM 节点
理解同一 thread 内的追问上下文（指代消解、细节追问）。

设计权衡：使用确定性截断而非 LLM 摘要 —— 快、零成本、可测试。
LLM 摘要可作为后续优化叠加。
"""

from __future__ import annotations

from typing import Any


def format_conversation_summary(
    messages: list[dict[str, Any]],
    max_turns: int = 3,
    max_chars_per_msg: int = 800,
) -> str:
    """把 messages 列表格式化为紧凑的多轮摘要。

    Args:
        messages: ``metadata_store.list_messages`` 返回的列表，每项含
            ``role``（"user"/"assistant"）与 ``content``。其他 role 被忽略。
        max_turns: 保留最近多少「轮」（一轮 = 一条 user + 一条 assistant）。
        max_chars_per_msg: 单条消息正文的最大字符数，超出截断。

    Returns:
        摘要字符串；无有效消息时返回空串。
    """
    if not messages:
        return ""

    valid = [m for m in messages if m.get("role") in {"user", "assistant"}]
    if not valid:
        return ""

    tail = valid[-(max_turns * 2) :]

    def _truncate(text: str) -> str:
        text = (text or "").strip()
        if len(text) <= max_chars_per_msg:
            return text
        return text[:max_chars_per_msg] + f"…（已截断，原长度 {len(text)} 字符）"

    lines: list[str] = []
    role_label = {"user": "用户", "assistant": "助手"}
    for m in tail:
        role = m.get("role", "")
        content = _truncate(str(m.get("content", "")))
        if not content:
            continue
        lines.append(f"【{role_label.get(role, role)}】{content}")

    if not lines:
        return ""

    return "\n\n".join(lines)
