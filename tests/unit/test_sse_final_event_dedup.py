"""P0-3: final_output 事件去重，不再重复发送 markdown_fallback。"""

from __future__ import annotations

from lvyan.api.sse import RunContext, _build_final_output_event


def _ctx(final_output: str = "报告正文", legal_answer: dict | None = None) -> RunContext:
    ctx = RunContext(run_id="r", thread_id="t")
    ctx.final_output = final_output
    ctx.legal_answer = legal_answer
    return ctx


def test_event_without_answer_only_has_output():
    event = _build_final_output_event(_ctx(final_output="纯文本"))
    assert event["event"] == "final_output"
    assert event["output"] == "纯文本"
    assert "answer" not in event
    assert "markdown_fallback" not in event


def test_event_with_answer_drops_duplicate_markdown_fallback():
    event = _build_final_output_event(
        _ctx(final_output="报告正文", legal_answer={"schema_version": "legal_answer_v1"})
    )
    assert event["schema_version"] == "legal_answer_v1"
    assert event["answer"] == {"schema_version": "legal_answer_v1"}
    assert event["output"] == "报告正文"
    assert "markdown_fallback" not in event
