# Multi-turn Conversation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Within a single conversation thread, let the agent recall prior turns (user questions + assistant answers) so follow-up questions like "上面那条法条再说细一点" or"那我这种情况能退多少" are answered with context, not from a blank slate.

**Architecture:** A new `conversation_summary` state field (string, overwrite semantics) carries a compact digest of recent turns. `default_runner` loads thread history via a callback on `RunContext` (injected by `RunManager` from `metadata_store`), formats the last N turns into the summary, and injects it into initial state. The three LLM nodes (fact_extractor, legal_reasoner, planner) consume `conversation_summary` with the same injection pattern already used for `relevant_attachment_context` — empty string = no-op, so the system is fully backward-compatible.

**Tech Stack:** Python 3.11, LangGraph, FastAPI, pytest.

---

## Scope

Single feature: **multi-turn memory within one thread**. Out of scope (separate plans):
- Cross-thread memory / long-term user profile.
- Summarization via LLM (this plan uses a deterministic truncation formatter; LLM-based compaction is a later optimization).
- Changes to the frontend conversation display (it already shows history by thread_id).

## File Structure

**Create:**
- `src/lvyan/tools/conversation_history.py` — formatter: `list[dict]` messages → compact summary string.
- `tests/unit/test_conversation_history_formatter.py`
- `tests/unit/test_runner_injects_history.py`

**Modify:**
- `src/lvyan/graph/state.py` — add `conversation_summary: str` (overwrite).
- `src/lvyan/schemas/case.py` — add `conversation_summary: str = ""` (default empty).
- `src/lvyan/api/sse.py` — `RunContext` gains `load_history: Callable | None`; `RunManager.start` injects it; `default_runner` calls it and writes `conversation_summary` into initial state.
- `src/lvyan/nodes/fact_extractor.py` — inject summary into prompt.
- `src/lvyan/nodes/legal_reasoner.py` — inject summary into prompt.
- `src/lvyan/nodes/planner.py` — inject summary into prompt.

Each file has one responsibility. Nodes stay pure functions of state.

---

## Task 1: conversation_summary state field + CaseState default

**Files:**
- Modify: `src/lvyan/graph/state.py` (~line 199, after `relevant_attachment_context`)
- Modify: `src/lvyan/schemas/case.py` (~line 113)

- [ ] **Step 1: Add field to GraphState**

In `src/lvyan/graph/state.py`, locate the `relevant_attachment_context: str` field added previously and add directly below it (still in the overwrite-semantics block, before the `# --- 案件元信息 ---` comment):

```python
    # 多轮记忆：本 thread 此前若干轮的紧凑摘要（覆盖语义）。
    # 由 default_runner 在 run 开始时写入；LLM 节点据此理解追问上下文。
    conversation_summary: str
```

- [ ] **Step 2: Add field to CaseState**

In `src/lvyan/schemas/case.py`, locate the `relevant_attachment_context: str = ""` field and add directly below it:

```python
    # 多轮记忆：本 thread 此前若干轮的紧凑摘要
    conversation_summary: str = ""
```

- [ ] **Step 3: Verify import works**

Run: `python -c "from lvyan.schemas import CaseState; s = CaseState(run_id='r', thread_id='t', current_date='2026-08-03', user_goal='q'); print(repr(s.conversation_summary))"`
Expected output: `''`

- [ ] **Step 4: Run regression (behavior unchanged — field is empty everywhere)**

Run: `python -m pytest tests/unit/test_graph_checkpoint.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/graph/state.py src/lvyan/schemas/case.py
git commit -m "feat(state): add conversation_summary field for multi-turn memory"
```

---

## Task 2: conversation history formatter

**Files:**
- Create: `src/lvyan/tools/conversation_history.py`
- Test: `tests/unit/test_conversation_history_formatter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_conversation_history_formatter.py`:

```python
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
    """超过 max_turns 的旧轮次被丢弃，避免 token 膨胀。"""
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
    """单条 assistant 输出超过 max_chars_per_msg 被截断。"""
    long_answer = "长答案。" * 500
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": long_answer},
    ]
    summary = format_conversation_summary(msgs, max_chars_per_msg=100)
    # 截断标记出现
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_conversation_history_formatter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lvyan.tools.conversation_history'`.

- [ ] **Step 3: Implement the formatter**

Create `src/lvyan/tools/conversation_history.py`:

```python
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

    # 过滤已知 role，并按顺序成对配位
    valid = [m for m in messages if m.get("role") in {"user", "assistant"}]
    if not valid:
        return ""

    # 取最后 max_turns*2 条（每轮 2 条）
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_conversation_history_formatter.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/tools/conversation_history.py tests/unit/test_conversation_history_formatter.py
git commit -m "feat(history): add conversation history formatter"
```

---

## Task 3: LLM nodes consume conversation_summary (no-op when empty)

**Files:**
- Modify: `src/lvyan/nodes/fact_extractor.py` (~line 362, after the `context_block` build)
- Modify: `src/lvyan/nodes/legal_reasoner.py` (~line 621, after `attachment_block`)
- Modify: `src/lvyan/nodes/planner.py` (~line 150, after `context_block`)

Same injection pattern as `relevant_attachment_context`. Empty string = no-op, so behavior is unchanged until Task 5 populates it.

- [ ] **Step 1: fact_extractor — read summary and inject**

In `src/lvyan/nodes/fact_extractor.py`, in `_try_llm_extract_facts` (around line 350), add a parameter and inject. First change the signature (currently `def _try_llm_extract_facts(user_goal, case_type, attachment_context="")`):

```python
def _try_llm_extract_facts(
    user_goal: str,
    case_type: str | None,
    attachment_context: str = "",
    conversation_summary: str = "",
) -> tuple[list[Fact], list[TimelineEvent]] | None:
```

Then in the body, after the `context_block` build (around line 366), add:

```python
    history_block = (
        f"\n此前对话摘要：\n{conversation_summary}\n" if conversation_summary.strip() else ""
    )
```

And change the `user_prompt` to include `{history_block}`. The current line is:

```python
    user_prompt = (
        f"{case_hint}\n用户描述：{user_goal}\n{context_block}\n"
```

Change it to:

```python
    user_prompt = (
        f"{case_hint}\n用户描述：{user_goal}\n{context_block}{history_block}\n"
```

Then in `fact_extractor` (the node function, ~line 467), read the field and pass it:

```python
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)
    attachment_context = _get(state, "relevant_attachment_context", "") or ""
    conversation_summary = _get(state, "conversation_summary", "") or ""
    existing_facts = _get(state, "facts", []) or []

    # --- 优先 LLM 抽取 ---
    llm_result = _try_llm_extract_facts(
        user_goal, case_type, attachment_context, conversation_summary
    )
```

- [ ] **Step 2: legal_reasoner — read summary and inject**

In `src/lvyan/nodes/legal_reasoner.py`, in the LLM path (around line 621, right after `attachment_block = ...`), add:

```python
    conversation_summary = _get(state, "conversation_summary", "") or ""
    history_block = (
        f"\n此前对话摘要：\n{conversation_summary}\n" if conversation_summary.strip() else ""
    )
```

Then in the `user_prompt` (around line 633), the current block is:

```python
    user_prompt = (
        f"案由：{case_type}\n"
        f"用户目标：{user_goal}\n{attachment_block}"
```

Change it to:

```python
    user_prompt = (
        f"案由：{case_type}\n"
        f"用户目标：{user_goal}\n{attachment_block}{history_block}"
```

- [ ] **Step 3: planner — read summary and inject**

In `src/lvyan/nodes/planner.py`, first add the parameter to `_try_llm_plan` (currently `def _try_llm_plan(user_goal, case_type, facts, attachment_context="")`):

```python
def _try_llm_plan(
    user_goal: str,
    case_type: str | None,
    facts: list[Any],
    attachment_context: str = "",
    conversation_summary: str = "",
) -> tuple[list[RetrievalQuery], list[PlanStep]] | None:
```

In the body (around line 153, after `context_block = ...`), add:

```python
    history_block = (
        f"\n此前对话摘要：\n{conversation_summary}\n" if conversation_summary.strip() else ""
    )
```

Change the `user_prompt` (around line 159) from:

```python
    user_prompt = (
        f"{case_hint}\n用户目标：{user_goal}\n已知事实：{facts_summary}\n{context_block}\n"
```

to:

```python
    user_prompt = (
        f"{case_hint}\n用户目标：{user_goal}\n已知事实：{facts_summary}\n{context_block}{history_block}\n"
```

In the `planner` node function (around line 257), read and pass it:

```python
    user_goal = _get(state, "user_goal", "") or ""
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []
    attachment_context = _get(state, "relevant_attachment_context", "") or ""
    conversation_summary = _get(state, "conversation_summary", "") or ""

    # --- 优先 LLM 计划生成 ---
    llm_result = _try_llm_plan(
        user_goal, case_type, facts, attachment_context, conversation_summary
    )
```

- [ ] **Step 4: Run regression — behavior unchanged (summary empty in all existing tests)**

Run: `python -m pytest tests/unit/test_fact_extractor_llm.py tests/unit/test_legal_reasoner.py tests/agent/test_triage_fact.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/nodes/fact_extractor.py src/lvyan/nodes/legal_reasoner.py src/lvyan/nodes/planner.py
git commit -m "feat(nodes): LLM nodes consume conversation_summary (no-op when empty)"
```

---

## Task 4: RunContext.load_history callback + RunManager injection

**Files:**
- Modify: `src/lvyan/api/sse.py`

The cleanest seam: `RunContext` gains an optional `load_history` callable. `RunManager.start` injects a closure that calls `self._metadata_store.list_messages(thread_id, user_id)`. `default_runner` calls `ctx.load_history()` if present.

- [ ] **Step 1: Add load_history to RunContext**

In `src/lvyan/api/sse.py`, in `RunContext.__init__` (around line 40), add a parameter and attribute. The current signature ends with `attachment_refs: list[dict] | None = None,`. After it:

```python
    def __init__(
        self,
        run_id: str,
        thread_id: str,
        user_id: str = "anonymous",
        law_as_of_date: date | None = None,
        attachment_refs: list[dict] | None = None,
        load_history: Any = None,
    ) -> None:
```

(Ensure `Any` is imported at the top of the file — it already is: `from typing import Any`.)

In the body, after `self.attachment_refs = ...`, add:

```python
        # 多轮记忆：由 RunManager 注入的回调，runner 调用它读取本 thread 历史。
        # 签名：() -> list[dict]；为 None 表示无持久化存储（无历史可读）。
        self.load_history: Any = load_history
```

- [ ] **Step 2: RunManager.start injects the callback**

In `src/lvyan/api/sse.py`, in `RunManager.create_run` (around line 404), the current `RunContext(...)` construction ends with `attachment_refs=attachment_refs,`. After that kwarg:

```python
        ctx = self._bind_context(
            RunContext(
                run_id,
                resolved_thread_id,
                user_id=user_id,
                law_as_of_date=law_as_of_date,
                attachment_refs=attachment_refs,
                load_history=self._make_history_loader(resolved_thread_id, user_id),
            )
        )
```

Then add a helper method on `RunManager` (place it right after `create_run`, before `_drive`):

```python
    def _make_history_loader(self, thread_id: str, user_id: str) -> Any:
        """构造读取本 thread 历史消息的闭包。

        返回的 callable 签名 ``() -> list[dict]``；无 metadata_store 时返回
        ``None``（runner 据此跳过历史注入）。
        """
        if self._metadata_store is None:
            return None

        store = self._metadata_store

        def _load() -> list[dict]:
            try:
                return store.list_messages(thread_id, user_id)
            except Exception:  # noqa: BLE001  历史读取失败不阻断主流程
                return []

        return _load
```

- [ ] **Step 3: Write a focused test for the loader injection**

Create `tests/unit/test_runner_injects_history.py`:

```python
"""验证 RunManager 为 RunContext 注入历史读取回调。"""
from __future__ import annotations

from lvyan.api.sse import RunManager


def test_make_history_loader_returns_callable_when_store_present():
    class FakeStore:
        def __init__(self):
            self.called = False

        def list_messages(self, thread_id, user_id):
            self.called = True
            return [{"role": "user", "content": "历史问题"}]

    store = FakeStore()
    mgr = RunManager(metadata_store=store)
    loader = mgr._make_history_loader("thread-1", "u1")
    assert callable(loader)
    msgs = loader()
    assert store.called is True
    assert msgs == [{"role": "user", "content": "历史问题"}]


def test_make_history_loader_returns_none_when_no_store():
    mgr = RunManager(metadata_store=None)
    assert mgr._make_history_loader("thread-1", "u1") is None


def test_make_history_loader_swallows_errors():
    class BadStore:
        def list_messages(self, thread_id, user_id):
            raise RuntimeError("db down")

    mgr = RunManager(metadata_store=BadStore())
    loader = mgr._make_history_loader("thread-1", "u1")
    assert loader() == []
```

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/unit/test_runner_injects_history.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/api/sse.py tests/unit/test_runner_injects_history.py
git commit -m "feat(runner): inject history loader callback into RunContext"
```

---

## Task 5: default_runner populates conversation_summary

**Files:**
- Modify: `src/lvyan/api/sse.py` (function `default_runner`, ~line 1188)

The switch-over: runner reads history via the callback, formats it, and writes `conversation_summary` into initial state.

- [ ] **Step 1: Extend the integration test**

Add a test to `tests/unit/test_runner_injects_history.py`:

```python
def test_default_runner_writes_conversation_summary_into_initial_state(monkeypatch):
    """default_runner 调用 load_history 并把格式化结果写入初始 state。"""
    import asyncio
    from lvyan.api.sse import RunContext, default_runner

    captured = {}

    async def fake_stream(graph, initial, config, ctx, **kw):
        captured["initial"] = initial
        return ("", None)

    monkeypatch.setattr("lvyan.api.sse._stream_graph_events", fake_stream)
    monkeypatch.setattr("lvyan.api.sse._get_graph", lambda: object())

    class FakeCaseMem:
        def register(self, *a, **kw):
            pass

    monkeypatch.setattr("lvyan.api.sse.get_case_memory", lambda: FakeCaseMem())
    monkeypatch.setattr("lvyan.observability.tracing.set_cost_thread", lambda _: None)

    ctx = RunContext("run-1", "thread-1", user_id="u1")
    ctx.load_history = lambda: [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    asyncio.run(default_runner("本轮问题", "thread-1", "deep", ctx))
    assert captured["initial"]["conversation_summary"].count("上一轮问题") == 1
    assert captured["initial"]["conversation_summary"].count("上一轮回答") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_runner_injects_history.py::test_default_runner_writes_conversation_summary_into_initial_state -v`
Expected: FAIL — captured initial has `conversation_summary == ""` (or KeyError).

- [ ] **Step 3: Implement — runner loads and formats history**

In `src/lvyan/api/sse.py`, in `default_runner` (around line 1188), just before the `initial = CaseState(...)` construction, add the history loading. First add the import at the top of the function body alongside the other local imports (around line 1159-1163):

```python
    from lvyan.tools.conversation_history import format_conversation_summary
```

Then, right before `initial = CaseState(...)`, add:

```python
        # 多轮记忆：读取本 thread 历史，格式化为紧凑摘要注入初始 state。
        history_msgs = ctx.load_history() if ctx.load_history else []
        conversation_summary = format_conversation_summary(history_msgs) if history_msgs else ""
```

Then add the field to the `CaseState(...)` construction. The current construction ends with `uploaded_documents=uploaded_docs,`. After it:

```python
        initial = CaseState(
            run_id=ctx.run_id,
            thread_id=thread_id,
            current_date=_date.today(),
            user_goal=query,
            complexity=complexity,
            user_id=ctx.user_id,
            law_as_of_date=ctx.law_as_of_date,
            uploaded_documents=uploaded_docs,
            conversation_summary=conversation_summary,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_runner_injects_history.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run full regression**

Run: `python -m pytest tests/ -q -m "not slow"`
Expected: all green (existing tests have empty history → summary is "" → no-op).

- [ ] **Step 6: Commit**

```bash
git add src/lvyan/api/sse.py tests/unit/test_runner_injects_history.py
git commit -m "feat(runner): populate conversation_summary from thread history"
```

---

## Task 6: Verify end-to-end

**Files:** none (verification only)

- [ ] **Step 1: Restart the server**

Run: `python -m uvicorn lvyan.api.server:create_app --factory --port 8000`
Expected: starts without import errors.

- [ ] **Step 2: Manual two-turn test**

1. Ask a question: "房东不退押金 3000 元怎么办？"
2. In the same thread, ask a follow-up referencing the first: "那如果合同里没写押金条款呢？上面说的民法典那条还适用吗？"
3. Verify the second answer demonstrates awareness of the first turn (references 押金/民法典 rather than treating it as a generic contract question).

Note: this requires Postgres metadata_store to be available (history is persisted there). Without it, `load_history` returns `None` and memory is disabled gracefully.

- [ ] **Step 3: Full regression final**

Run: `python -m pytest tests/ -q -m "not slow"`
Expected: all green.

---

## Self-Review Notes

**Spec coverage:**
- State carries prior turns → Task 1 (`conversation_summary` field) ✓
- Runner loads thread history → Task 4 + Task 5 (`load_history` callback, `default_runner` populates) ✓
- LLM nodes see history → Task 3 (fact_extractor, legal_reasoner, planner inject `conversation_summary`) ✓
- Compact (no token explosion) → Task 2 (formatter: max_turns + per-msg truncation) ✓
- Backward-compatible → empty summary = no-op, verified in every regression step ✓
- Graceful degradation without Postgres → Task 4 (`_make_history_loader` returns None when no store; Task 5 guards `if ctx.load_history`) ✓

**Placeholder scan:** none — every step contains complete code or exact edits with line anchors.

**Type consistency:**
- `conversation_summary: str` consistent across GraphState, CaseState, all three LLM node signatures, and `default_runner`.
- `load_history` callable signature `() -> list[dict]` consistent in RunContext doc, `_make_history_loader` return, and `default_runner` call site.
- `format_conversation_summary(messages, max_turns=3, max_chars_per_msg=800)` signature matches all call sites.
