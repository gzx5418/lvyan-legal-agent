"""P0-A 回归测试：SSE 聚合器必须显式覆盖 legal_answer=None。

验证核心不变量：
1. composer 先写入 legal_answer（非 None 初稿）；
2. legal_answer_finalizer 在 document 模式 / HITL edited / 校验失败时返回
   legal_answer=None（key 存在，值为 None）；
3. ``_stream_graph_events`` 必须用 ``"legal_answer" in update`` 显式覆盖，
   而非 ``if la:`` truthy 判断——否则 None 是 falsy 不会覆盖旧值，
   导致 composer 旧版结构化答案残留在内存中，前端继续收到 stale answer。

本测试直接驱动 ``_stream_graph_events``，用 fake graph 依次产出 composer
update 与 finalizer update，验证最终返回的 legal_answer 为 None。
"""

from __future__ import annotations

import asyncio
from typing import Any

from lvyan.api.sse import RunContext, _stream_graph_events


class _FakeGraph:
    """Fake graph：astream 依次产出预设的 update chunk 列表。

    模拟 LangGraph v2 格式：每个 chunk 是 ``{"type": "updates", "data": {node: update}}``。
    """

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = list(chunks)

    async def astream(self, source: Any, config: Any, *, stream_mode=None, version=None):
        for chunk in self._chunks:
            yield chunk


def _make_ctx() -> RunContext:
    """构造一个带 event loop 的 RunContext（_stream_graph_events 需要 publish）。"""
    ctx = RunContext("run-p0a", "thread-p0a", user_id="u1")
    return ctx


def _updates_chunk(node: str, update: dict[str, Any]) -> dict[str, Any]:
    """构造 v2 格式的 updates chunk。"""
    return {"type": "updates", "data": {node: update}}


# ---------------------------------------------------------------------------
# 1. 核心：finalizer 返回 legal_answer=None 必须覆盖 composer 旧值
# ---------------------------------------------------------------------------
def test_finalizer_none_overrides_composer_legal_answer():
    """composer 写入 legal_answer 初稿 → finalizer 返回 None → 最终应为 None。

    这是 P0-A 的核心场景：若用 ``if la:`` 判断，None 是 falsy 不会覆盖，
    导致 stale answer 残留。
    """
    ctx = _make_ctx()
    graph = _FakeGraph(
        [
            # 1) composer 写入初稿 legal_answer（非 None）
            _updates_chunk(
                "composer",
                {
                    "final_output": "# 起诉状草稿\n\n原告...",
                    "legal_answer": {"schema_version": "legal_answer_v1", "old": True},
                },
            ),
            # 2) finalizer 在 document 模式返回 legal_answer=None
            _updates_chunk(
                "legal_answer_finalizer",
                {
                    "final_output": "# 起诉状草稿\n\n原告...\n\n文书文件：xxx.docx",
                    "legal_answer": None,
                    "document_file": {"success": True, "output_path": "xxx.docx"},
                },
            ),
        ]
    )

    final_output, legal_answer, document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert legal_answer is None, (
        "finalizer 返回 legal_answer=None 时，SSE 聚合器必须显式覆盖旧值；"
        "若仍为旧 dict 则是 P0-A falsy 漏洞未修复"
    )
    assert "文书文件" in final_output


# ---------------------------------------------------------------------------
# 2. HITL edited 路径：finalizer 清空 legal_answer
# ---------------------------------------------------------------------------
def test_hitl_edit_clears_legal_answer():
    """HITL edited → finalizer 返回 legal_answer=None → SSE 应传播 None。"""
    ctx = _make_ctx()
    graph = _FakeGraph(
        [
            _updates_chunk(
                "composer",
                {
                    "final_output": "deep 分析报告",
                    "legal_answer": {"schema_version": "legal_answer_v1", "stale": True},
                },
            ),
            # HITL edited 后 finalizer 清空 legal_answer
            _updates_chunk(
                "legal_answer_finalizer",
                {"legal_answer": None, "document_file": None},
            ),
        ]
    )

    _, legal_answer, _document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert legal_answer is None, "HITL edited 后 legal_answer 必须为 None"


# ---------------------------------------------------------------------------
# 3. 校验失败路径：finalizer 降级为 None
# ---------------------------------------------------------------------------
def test_validator_failure_clears_legal_answer():
    """finalizer 校验失败时 legal_answer_dict 保持 None → SSE 应传播 None。"""
    ctx = _make_ctx()
    graph = _FakeGraph(
        [
            _updates_chunk(
                "composer",
                {
                    "final_output": "报告",
                    "legal_answer": {"schema_version": "legal_answer_v1", "draft": True},
                },
            ),
            # 校验失败 → legal_answer_dict 为 None
            _updates_chunk(
                "legal_answer_finalizer",
                {"legal_answer": None, "document_file": None},
            ),
        ]
    )

    _, legal_answer, _document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert legal_answer is None, "校验失败后 legal_answer 必须为 None"


# ---------------------------------------------------------------------------
# 4. 正常路径：finalizer 返回有效 legal_answer，不应被清空
# ---------------------------------------------------------------------------
def test_normal_path_keeps_valid_legal_answer():
    """finalizer 返回有效 dict 时，SSE 应正确覆盖 composer 初稿（而非保留旧值）。"""
    ctx = _make_ctx()
    final_answer = {"schema_version": "legal_answer_v1", "redacted": True}
    graph = _FakeGraph(
        [
            _updates_chunk(
                "composer",
                {
                    "final_output": "报告",
                    "legal_answer": {"schema_version": "legal_answer_v1", "draft": True},
                },
            ),
            _updates_chunk(
                "legal_answer_finalizer",
                {"legal_answer": final_answer, "document_file": None},
            ),
        ]
    )

    _, legal_answer, _document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert legal_answer == final_answer, "finalizer 的有效 legal_answer 应覆盖 composer 初稿"


# ---------------------------------------------------------------------------
# 5. 仅 composer（无 finalizer）：保留初稿（不应被误清空）
# ---------------------------------------------------------------------------
def test_composer_only_keeps_legal_answer():
    """只有 composer update、无 finalizer update 时，legal_answer 应保留初稿。"""
    ctx = _make_ctx()
    draft = {"schema_version": "legal_answer_v1", "draft": True}
    graph = _FakeGraph(
        [
            _updates_chunk("composer", {"final_output": "报告", "legal_answer": draft}),
        ]
    )

    _, legal_answer, _document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert legal_answer == draft, "无 finalizer 时应保留 composer 初稿"


# ---------------------------------------------------------------------------
# 6. final_output=None 也应显式覆盖（防御性）
# ---------------------------------------------------------------------------
def test_final_output_none_overrides_old_value():
    """若某节点返回 final_output=None，应显式覆盖（而非保留旧值）。

    虽然 finalizer 不会清空 final_output，但此测试确保 ``"key" in update``
    语义对 final_output 也生效，防御未来节点行为变化。
    """
    ctx = _make_ctx()
    graph = _FakeGraph(
        [
            _updates_chunk("composer", {"final_output": "旧正文"}),
            # 假设某节点显式清空 final_output（异常路径）
            _updates_chunk("some_node", {"final_output": None}),
        ]
    )

    final_output, _legal_answer, _document_file = asyncio.new_event_loop().run_until_complete(
        _stream_graph_events(graph, {}, {}, ctx, final_output="")
    )

    assert final_output is None, "final_output=None 应显式覆盖旧值"
