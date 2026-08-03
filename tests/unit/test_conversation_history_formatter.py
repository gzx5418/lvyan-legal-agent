"""对话历史格式化器测试：把 messages 列表压成紧凑摘要字符串。"""
from __future__ import annotations

from lvyan.tools.conversation_history import format_conversation_summary


def test_empty_messages_returns_empty():
    assert format_conversation_summary([]) == ""


def test_single_turn_formatted():
    msgs = [
        {"role": "user", "content": "房东不退押金怎么办？"},
        {"role": "assistant", "content": "根据《民法典》第七百零四条…"},
    ]
    summary = format_conversation_summary(msgs)
    assert "房东不退押金怎么办？" in summary
    assert "根据《民法典》第七百零四条…" in summary


def test_keeps_only_last_n_turns():
    msgs = []
    for i in range(10):
        msgs.append({"role": "user", "content": f"用户问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})
    summary = format_conversation_summary(msgs, max_turns=3)
    assert "用户问题9" in summary
    assert "回答9" in summary
    assert "用户问题0" not in summary
    assert "用户问题6" not in summary


def test_assistant_content_truncated():
    long_answer = "长答案。" * 500
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": long_answer},
    ]
    summary = format_conversation_summary(msgs, max_chars_per_msg=100)
    assert "…（已截断" in summary
    assert len(summary) < len(long_answer)


def test_ignores_unknown_roles():
    msgs = [
        {"role": "system", "content": "忽略我"},
        {"role": "user", "content": "用户"},
    ]
    summary = format_conversation_summary(msgs)
    assert "忽略我" not in summary
    assert "用户" in summary
