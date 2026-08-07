# Performance: Context-Bloat Reduction & Retrieval Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop stuffing full attachment text into every LLM call, dedup the final SSE payload, and run statute/case retrieval concurrently — so deep analysis gets materially faster without touching the DB layer or the document-generation architecture.

**Architecture:**
1. Attachments are split into chunks at upload time and stored as a sidecar on disk; a new `attachment_retriever` node BM25-ranks chunks against `user_goal` and writes a compact `relevant_attachment_context` into state; downstream LLM nodes read that context instead of a blob living inside `user_goal`.
2. The final SSE event stops emitting the duplicate `markdown_fallback` field; the frontend reads the fallback from `output`.
3. `parallel_retrieval` runs statute and case search concurrently in the existing thread pool.

**Tech Stack:** Python 3.11, LangGraph, FastAPI, pytest, vanilla JS frontend (no build step).

---

## Scope & Deferrals

This plan covers the **"context bloat + retrieval latency" cluster** only. The following first-phase items are intentionally deferred to separate plans (each produces working software on its own and carries independent risk):

- **P0-2 DOCX deferred until approval** — couples with the upcoming `LegalDocumentV1` document-workflow refactor; doing it twice would be wasted work.
- **P0-4 PostgreSQL connection pool** — pure infrastructure change; isolated risk; belongs in its own plan.
- **Retrieval-result caching** — depends on a corpus-version key strategy not yet decided.
- **History pagination** — frontend + API change; independent.

Rationale: attachment chunking is the single highest-impact item ("the most rewarding performance change") and is self-contained within the retrieval/context pipeline, so it anchors this plan.

---

## File Structure

**Create:**
- `src/lvyan/schemas/attachment.py` — `AttachmentChunk` model.
- `src/lvyan/tools/attachment_chunker.py` — markdown → chunks splitter.
- `src/lvyan/retrieval/attachment_ranker.py` — lightweight BM25 ranker over a small chunk set.
- `src/lvyan/nodes/attachment_retriever.py` — graph node that loads attachment chunks, ranks, writes `relevant_attachment_context`.
- `tests/unit/test_attachment_chunker.py`
- `tests/unit/test_attachment_ranker.py`
- `tests/unit/test_attachment_retriever_node.py`
- `tests/unit/test_sse_final_event_dedup.py`
- `tests/unit/test_parallel_retrieval_concurrent.py`

**Modify:**
- `src/lvyan/graph/state.py` — add `relevant_attachment_context: str` field (overwrite semantics).
- `src/lvyan/nodes/fact_extractor.py` — prompt reads `relevant_attachment_context`.
- `src/lvyan/nodes/legal_reasoner.py` — prompt reads `relevant_attachment_context`.
- `src/lvyan/nodes/planner.py` — prompt reads `relevant_attachment_context`.
- `src/lvyan/graph/builder.py` — register `attachment_retriever` between `preflight` and `jurisdiction_triage`.
- `src/lvyan/api/sse.py` — `RunContext` carries `attachment_refs`; `default_runner` populates `uploaded_documents`; drop `markdown_fallback` from final event.
- `src/lvyan/api/server.py` — stop concatenating attachment markdown into `query`; pass attachment refs to `RunContext`.
- `src/lvyan/api/static/app.js` — read fallback from `output` when `markdown_fallback` absent.
- `src/lvyan/nodes/retrieve_statutes.py` — concurrent statutes + cases.

Each file has one responsibility; nodes stay pure functions of state.

---

## Task 1: SSE final event dedup (P0-3 quick win)

**Files:**
- Modify: `src/lvyan/api/sse.py` (function `_build_final_output_event`, ~line 897)
- Modify: `src/lvyan/api/static/app.js` (final_output handler that reads `markdown_fallback`)
- Test: `tests/unit/test_sse_final_event_dedup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_sse_final_event_dedup.py`:

```python
"""P0-3: final_output 事件去重，不再重复发送 markdown_fallback。"""
from __future__ import annotations

from lvyan.api.sse import RunContext, _build_final_output_event


def _ctx(final_output: str = "报告正文", legal_answer: dict | None = None) -> RunContext:
    ctx = RunContext(run_id="r", thread_id="t")
    ctx.final_output = final_output
    ctx.legal_answer = legal_answer
    return ctx


def test_event_without_answer_only_has_output():
    """无结构化 answer 时，事件只含 output（旧行为兼容）。"""
    event = _build_final_output_event(_ctx(final_output="纯文本"))
    assert event["event"] == "final_output"
    assert event["output"] == "纯文本"
    assert "answer" not in event
    assert "markdown_fallback" not in event


def test_event_with_answer_drops_duplicate_markdown_fallback():
    """有结构化 answer 时不再发送重复的 markdown_fallback。"""
    event = _build_final_output_event(
        _ctx(final_output="报告正文", legal_answer={"schema_version": "legal_answer_v1"})
    )
    assert event["schema_version"] == "legal_answer_v1"
    assert event["answer"] == {"schema_version": "legal_answer_v1"}
    # output 保留（旧前端 + 作为 fallback 来源）
    assert event["output"] == "报告正文"
    # 不再重复发送同一份 markdown
    assert "markdown_fallback" not in event
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_sse_final_event_dedup.py -v`
Expected: FAIL — `test_event_with_answer_drops_duplicate_markdown_fallback` fails because current code sets `event["markdown_fallback"]`.

- [ ] **Step 3: Implement — drop markdown_fallback**

In `src/lvyan/api/sse.py`, replace `_build_final_output_event` (around line 897-908):

```python
def _build_final_output_event(ctx: "RunContext") -> dict[str, Any]:
    """构建 final_output 事件。

    P0-3 去重：不再单独发送 ``markdown_fallback``。``output`` 字段同时承担
    「旧前端 Markdown 来源」与「新前端 fallback 来源」两种角色，避免同一份
    完整 Markdown 在单次 SSE 中被发送两次。
    """
    event: dict[str, Any] = {"event": "final_output", "output": ctx.final_output}
    if ctx.legal_answer:
        event["schema_version"] = ctx.legal_answer.get("schema_version", "legal_answer_v1")
        event["answer"] = ctx.legal_answer
    return event
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_sse_final_event_dedup.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Update frontend fallback reader**

In `src/lvyan/api/static/app.js`, find the final_output handler that reads `markdown_fallback` (the line calling `updateLastAgentMessageStructured`). Change it to fall back to `output` when `markdown_fallback` is absent:

Locate the handler (search for `markdown_fallback`). Replace:

```javascript
const mdFallback = data.markdown_fallback ?? data.output ?? '';
```

with:

```javascript
// P0-3: 后端不再单独发送 markdown_fallback；output 即 fallback 来源
const mdFallback = data.markdown_fallback ?? data.output ?? '';
```

(If the existing code already reads `data.output` as a fallback, this is a no-op confirmation. If it reads only `data.markdown_fallback`, add `?? data.output`.)

Then bump the static version in `src/lvyan/api/static/index.html`: `app.js?v=12`.

- [ ] **Step 6: Run regression for SSE**

Run: `python -m pytest tests/unit/test_api_sse.py tests/unit/test_sse_final_event_dedup.py -q`
Expected: PASS (no regressions in existing SSE tests).

- [ ] **Step 7: Commit**

```bash
git add src/lvyan/api/sse.py src/lvyan/api/static/app.js src/lvyan/api/static/index.html tests/unit/test_sse_final_event_dedup.py
git commit -m "perf(sse): drop duplicate markdown_fallback from final_output event"
```

---

## Task 2: Concurrent statute + case retrieval (quick win)

**Files:**
- Modify: `src/lvyan/nodes/retrieve_statutes.py` (function `parallel_retrieval`, ~line 259)
- Test: `tests/unit/test_parallel_retrieval_concurrent.py`

Currently `parallel_retrieval` runs statute search (parallel across queries) → dedup → rerank → **then** case search serially. Case search does not depend on statute results, so they can overlap.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_parallel_retrieval_concurrent.py`:

```python
"""验证 statutes 与 cases 检索并发执行（总耗时 ≈ max 而非 sum）。"""
from __future__ import annotations

import time
from unittest.mock import patch

from lvyan.nodes.retrieve_statutes import parallel_retrieval


def _state(queries=None):
    return {
        "retrieval_queries": queries or [{"query_text": "押金返还"}],
        "user_goal": "房东不退押金",
        "law_as_of_date": None,
        "plan": [],
    }


def test_statute_and_case_search_run_concurrently():
    """两个检索各睡 0.3s；并发总耗时 < 0.55s（串行会 ≥ 0.6s）。"""
    def slow_statutes(query, **kw):
        time.sleep(0.3)
        return []

    def slow_cases(query, **kw):
        time.sleep(0.3)
        return None

    with patch("lvyan.nodes.retrieve_statutes.search_statutes", side_effect=slow_statutes), \
         patch("lvyan.nodes.retrieve_statutes.search_cases", side_effect=slow_cases):
        t0 = time.monotonic()
        parallel_retrieval(_state())
        elapsed = time.monotonic() - t0

    assert elapsed < 0.55, f"statutes/cases 未并发，耗时 {elapsed:.2f}s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_parallel_retrieval_concurrent.py -v`
Expected: FAIL — elapsed ≥ 0.6s because the two searches run serially.

- [ ] **Step 3: Implement — overlap the two searches**

In `src/lvyan/nodes/retrieve_statutes.py`, refactor `parallel_retrieval` so statute and case retrieval are submitted to the same `ThreadPoolExecutor` and awaited together. Replace the body of `parallel_retrieval` (from `queries = _get(...)` through the `return {...}`) with:

```python
    queries = _get(state, "retrieval_queries", []) or []
    law_as_of_date = _get(state, "law_as_of_date", None)
    rerank_query = _get(state, "user_goal", "") or ""

    case_query_text = ""
    for q in queries:
        qt = _get(q, "query_text", "") or ""
        if qt.strip():
            case_query_text = qt
            break
    if not case_query_text:
        case_query_text = rerank_query

    def _search_statutes_job() -> list[Authority]:
        raw = _parallel_search_statutes(queries, as_of=law_as_of_date)
        statutes = _dedup_authorities(raw)
        if rerank_query.strip() and statutes:
            pool_k = min(len(statutes), 10 * _RERANK_POOL_MULTIPLIER)
            statutes = _rerank_authorities(
                query=rerank_query, authorities=statutes[:pool_k], top_k=10
            )
        return statutes

    def _search_cases_job() -> list[CaseAuthority]:
        if not case_query_text.strip():
            return []
        try:
            result = search_cases(case_query_text, top_k=10)
        except Exception:  # noqa: BLE001
            return []
        cases: list[CaseAuthority] = []
        hits = _get(result, "results", []) or [] if result is not None else []
        for hit in hits:
            try:
                cases.append(_to_case_authority(hit))
            except Exception:  # noqa: BLE001
                continue
        return cases

    # P0 性能：法规与类案不相互依赖，提交到同一线程池并发执行
    global _CONCURRENT_EXECUTOR
    if _CONCURRENT_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor
        _CONCURRENT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lvyan-search")

    stat_future = _CONCURRENT_EXECUTOR.submit(_search_statutes_job)
    case_future = _CONCURRENT_EXECUTOR.submit(_search_cases_job)

    try:
        statutes = stat_future.result(timeout=30.0)
    except Exception:  # noqa: BLE001
        statutes = []
    try:
        cases = case_future.result(timeout=30.0)
    except Exception:  # noqa: BLE001
        cases = []

    plan = _get(state, "plan", []) or []
    updated_plan = _mark_plan_done(
        plan, tools_to_complete=("statute_retrieval", "case_retrieval")
    )

    return {
        "statutes": statutes,
        "cases": cases,
        "plan": updated_plan,
    }
```

Note: `_search_statutes_job` still uses the internal `_parallel_search_statutes` (which itself may submit to the pool for multi-query). To avoid pool starvation, keep `max_workers=4`; statutes job occupies workers only for its own sub-futures when there are multiple queries.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_parallel_retrieval_concurrent.py -v`
Expected: PASS (elapsed < 0.55s).

- [ ] **Step 5: Run existing retrieval regression**

Run: `python -m pytest tests/unit/test_retrieve_statutes.py tests/unit/test_parallel_retrieval_concurrent.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lvyan/nodes/retrieve_statutes.py tests/unit/test_parallel_retrieval_concurrent.py
git commit -m "perf(retrieval): run statute and case search concurrently"
```

---

## Task 3: AttachmentChunk schema + chunker

**Files:**
- Create: `src/lvyan/schemas/attachment.py`
- Create: `src/lvyan/tools/attachment_chunker.py`
- Modify: `src/lvyan/schemas/__init__.py` (export `AttachmentChunk`)
- Test: `tests/unit/test_attachment_chunker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_attachment_chunker.py`:

```python
"""附件 Markdown 分块器测试。"""
from __future__ import annotations

from lvyan.tools.attachment_chunker import chunk_attachment_markdown


def test_splits_by_markdown_headings():
    md = "# 当事人\n甲方：张三\n\n# 租金条款\n月租金 3000 元。\n\n# 违约责任\n逾期付款违约金。"
    chunks = chunk_attachment_markdown(md, document_id="f1", document_name="lease.md")
    sections = [c.section for c in chunks]
    assert sections == ["当事人", "租金条款", "违约责任"]
    assert all(c.document_id == "f1" for c in chunks)
    assert chunks[0].content == "甲方：张三"
    assert chunks[0].char_offset == 0


def test_long_section_split_by_paragraph():
    para = "第一句内容。" * 200  # 远超 max_chars
    md = f"# 正文\n{para}"
    chunks = chunk_attachment_markdown(md, document_id="f1", document_name="x.md", max_chars=120)
    assert len(chunks) > 1
    # 每个 chunk 不超 max_chars（允许最后一段略超，因按段落切）
    assert all(len(c.content) <= 120 + 20 for c in chunks)


def test_empty_markdown_returns_empty():
    assert chunk_attachment_markdown("", "f1", "x.md") == []


def test_no_heading_treated_as_body_section():
    chunks = chunk_attachment_markdown("纯正文，无标题。", "f1", "x.md")
    assert len(chunks) == 1
    assert chunks[0].section == "正文"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_attachment_chunker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lvyan.tools.attachment_chunker'`.

- [ ] **Step 3: Create the schema**

Create `src/lvyan/schemas/attachment.py`:

```python
"""附件分块相关数据模型。"""
from __future__ import annotations

from pydantic import BaseModel


class AttachmentChunk(BaseModel):
    """单个附件分块：按 markdown 标题/段落切分后的最小检索单元。"""

    chunk_id: str          # f"{document_id}#{序号}"
    document_id: str       # 所属附件 file_id
    document_name: str
    section: str           # 所属标题；无标题时为 "正文"
    content: str
    char_offset: int       # 在原始 markdown 中的起始字符偏移
```

- [ ] **Step 4: Export the schema**

In `src/lvyan/schemas/__init__.py`, add to the import from the case/output modules and to `__all__`:

```python
from .attachment import AttachmentChunk
```
and append `"AttachmentChunk"` to `__all__`.

- [ ] **Step 5: Create the chunker**

Create `src/lvyan/tools/attachment_chunker.py`:

```python
"""附件 Markdown 分块器：按标题与段落切分，控制单块长度。"""
from __future__ import annotations

import re

from lvyan.schemas.attachment import AttachmentChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _split_long_section(
    body: str, max_chars: int
) -> list[str]:
    """将过长的 section 正文按段落/句子边界继续切分。"""
    if len(body) <= max_chars:
        return [body] if body.strip() else []
    paragraphs = re.split(r"\n\s*\n", body)
    pieces: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}".strip() if buf else p
        else:
            if buf:
                pieces.append(buf)
            # 单段仍超限 → 硬切
            while len(p) > max_chars:
                pieces.append(p[:max_chars])
                p = p[max_chars:]
            buf = p
    if buf.strip():
        pieces.append(buf)
    return pieces


def chunk_attachment_markdown(
    md: str,
    document_id: str,
    document_name: str,
    max_chars: int = 800,
) -> list[AttachmentChunk]:
    """把附件 markdown 切成 ``AttachmentChunk`` 列表。

    策略：
      1. 以 markdown 标题（``# .. ######``）为一级边界；
      2. 标题下的正文若超过 ``max_chars``，按段落继续切分；
      3. 无任何标题时整篇视为 "正文" section。

    每个 chunk 记录在原始 md 中的 ``char_offset``。
    """
    if not md or not md.strip():
        return []

    headings = list(_HEADING_RE.finditer(md))
    chunks: list[AttachmentChunk] = []
    idx = 0

    if not headings:
        for piece in _split_long_section(md.strip(), max_chars):
            offset = md.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section="正文",
                    content=piece,
                    char_offset=offset if offset >= 0 else 0,
                )
            )
            idx += 1
        return chunks

    # 标题前的导言（若有）
    first_start = headings[0].start()
    if first_start > 0:
        preamble = md[:first_start].strip()
        for piece in _split_long_section(preamble, max_chars):
            offset = md.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section="导言",
                    content=piece,
                    char_offset=offset if offset >= 0 else 0,
                )
            )
            idx += 1

    for i, h in enumerate(headings):
        section_name = h.group(2).strip() or "正文"
        body_start = h.end()
        body_end = headings[i + 1].start() if i + 1 < len(headings) else len(md)
        body = md[body_start:body_end].strip()
        if not body:
            continue
        base_offset = body_start
        for piece in _split_long_section(body, max_chars):
            rel = body.find(piece)
            chunks.append(
                AttachmentChunk(
                    chunk_id=f"{document_id}#{idx}",
                    document_id=document_id,
                    document_name=document_name,
                    section=section_name,
                    content=piece,
                    char_offset=base_offset + (rel if rel >= 0 else 0),
                )
            )
            idx += 1
    return chunks
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_attachment_chunker.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
git add src/lvyan/schemas/attachment.py src/lvyan/schemas/__init__.py src/lvyan/tools/attachment_chunker.py tests/unit/test_attachment_chunker.py
git commit -m "feat(attachments): add AttachmentChunk schema and markdown chunker"
```

---

## Task 4: Lightweight attachment ranker

**Files:**
- Create: `src/lvyan/retrieval/attachment_ranker.py`
- Test: `tests/unit/test_attachment_ranker.py`

A self-contained BM25 over a small chunk set (do NOT reuse the 85k-doc global index).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_attachment_ranker.py`:

```python
"""附件分块排序器测试。"""
from __future__ import annotations

from lvyan.retrieval.attachment_ranker import rank_chunks
from lvyan.schemas.attachment import AttachmentChunk


def _chunk(cid: str, content: str) -> AttachmentChunk:
    return AttachmentChunk(
        chunk_id=cid, document_id="f1", document_name="x.md",
        section="正文", content=content, char_offset=0,
    )


def test_ranks_relevant_chunk_first():
    chunks = [
        _chunk("c0", "通用条款，与押金无关。"),
        _chunk("c1", "押金 3000 元，租期届满应予返还。"),
        _chunk("c2", "物业费缴纳说明。"),
    ]
    ranked = rank_chunks("房东不退押金怎么办", chunks, top_k=2)
    assert ranked[0].chunk_id == "c1"
    assert len(ranked) == 2


def test_empty_chunks_returns_empty():
    assert rank_chunks("q", [], top_k=5) == []


def test_top_k_respected():
    chunks = [_chunk(f"c{i}", f"押金 押金 {i}") for i in range(10)]
    ranked = rank_chunks("押金", chunks, top_k=3)
    assert len(ranked) == 3


def test_no_token_overlap_returns_input_order():
    chunks = [_chunk("c0", "aaa"), _chunk("c1", "bbb")]
    ranked = rank_chunks("zzz", chunks, top_k=5)
    assert [c.chunk_id for c in ranked] == ["c0", "c1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_attachment_ranker.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the ranker**

Create `src/lvyan/retrieval/attachment_ranker.py`:

```python
"""附件分块排序：小语料 BM25。

与 ``retrieval.lexical`` 的全库 8.5 万文档索引解耦 —— 附件分块每次只有几十块，
现场计算 IDF 即可，避免加载全局索引。
"""
from __future__ import annotations

import math
import re

from lvyan.schemas.attachment import AttachmentChunk

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def rank_chunks(
    query: str,
    chunks: list[AttachmentChunk],
    top_k: int = 6,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[AttachmentChunk]:
    """对 ``chunks`` 用 BM25 打分，返回前 ``top_k`` 条（保持原序当并列）。"""
    if not chunks:
        return []

    tokenized = [_tokenize(c.content) for c in chunks]
    n_docs = len(tokenized)
    avgdl = sum(len(d) for d in tokenized) / n_docs

    # 文档频率
    df: dict[str, int] = {}
    for toks in tokenized:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    q_terms = _tokenize(query)
    scores = [0.0] * n_docs
    for i, toks in enumerate(tokenized):
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks) or 1
        norm = 1 - b + b * (dl / (avgdl or 1))
        score = 0.0
        for term in q_terms:
            if term not in tf:
                continue
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            f = tf[term]
            score += idf * (f * (k1 + 1)) / (f + k1 * norm)
        scores[i] = score

    # 稳定排序：分数降序，并列保持原序（Python sort 稳定）
    order = sorted(range(n_docs), key=lambda i: scores[i], reverse=True)
    # 过滤 0 分项？保留并列原序语义：0 分也返回（按原序），但截断到 top_k
    return [chunks[i] for i in order[:top_k]]
```

Note on `test_no_token_overlap_returns_input_order`: when all scores are 0, `sorted` is stable so original order is preserved — test passes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_attachment_ranker.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/retrieval/attachment_ranker.py tests/unit/test_attachment_ranker.py
git commit -m "feat(retrieval): add small-corpus BM25 ranker for attachment chunks"
```

---

## Task 5: LLM nodes read `relevant_attachment_context` (no-op until populated)

**Files:**
- Modify: `src/lvyan/graph/state.py` — add field
- Modify: `src/lvyan/nodes/fact_extractor.py`
- Modify: `src/lvyan/nodes/legal_reasoner.py`
- Modify: `src/lvyan/nodes/planner.py`

This task changes prompts to prefer a new `relevant_attachment_context` field but falls back to the current behavior when it is empty. **System behavior is unchanged at this commit** — safe to ship standalone.

- [ ] **Step 1: Add the state field**

In `src/lvyan/graph/state.py`, locate the `user_goal: str` field (around line 199) and add directly below it:

```python
    # P0 性能：附件按需检索后的紧凑上下文，替代「全文塞进 user_goal」。
    # 由 attachment_retriever 节点写入（覆盖语义）；LLM 节点优先读取它。
    relevant_attachment_context: str
```

(Place it among the overwrite-semantics fields, not in the `Annotated[..., operator.add]` block.)

- [ ] **Step 2: Update fact_extractor prompt**

In `src/lvyan/nodes/fact_extractor.py`, in `_extract_facts_with_llm` (around line 358), read the context and inject it. After `case_hint = ...` (line 363), add:

```python
    attachment_ctx = (state.get("relevant_attachment_context") if isinstance(state, dict)
                      else getattr(state, "relevant_attachment_context", "")) or ""
```

Then change the `user_prompt` (line 368) from:

```python
    user_prompt = (
        f"{case_hint}\n用户描述：{user_goal}\n\n"
        ...
```

to:

```python
    context_block = f"\n相关材料摘要：\n{attachment_ctx}\n" if attachment_ctx.strip() else ""
    user_prompt = (
        f"{case_hint}\n用户描述：{user_goal}\n{context_block}\n"
        ...
```

(Keep the rest of the prompt unchanged.)

- [ ] **Step 3: Update legal_reasoner prompt**

In `src/lvyan/nodes/legal_reasoner.py`, in the LLM path (around line 600-637), after gathering `missing_summary`/`critic_summary`, add:

```python
    attachment_ctx = _get(state, "relevant_attachment_context", "") or ""
    attachment_block = f"\n相关材料摘要：\n{attachment_ctx}\n" if attachment_ctx.strip() else ""
```

Then insert `{attachment_block}` into `user_prompt` right after the `用户目标：{user_goal}\n` line.

- [ ] **Step 4: Update planner prompt**

In `src/lvyan/nodes/planner.py`, in its LLM path (around line 143-170), apply the same pattern: read `relevant_attachment_context` and append a `相关材料摘要` block to the user prompt.

- [ ] **Step 5: Run regression — behavior unchanged**

Run: `python -m pytest tests/unit/test_fact_extractor.py tests/unit/test_legal_reasoner.py tests/unit/test_planner.py tests/unit/test_graph_checkpoint.py -q`
Expected: PASS (context is empty in existing tests → prompts behave as before).

- [ ] **Step 6: Commit**

```bash
git add src/lvyan/graph/state.py src/lvyan/nodes/fact_extractor.py src/lvyan/nodes/legal_reasoner.py src/lvyan/nodes/planner.py
git commit -m "feat(state): add relevant_attachment_context field consumed by LLM nodes"
```

---

## Task 6: attachment_retriever node

**Files:**
- Create: `src/lvyan/nodes/attachment_retriever.py`
- Test: `tests/unit/test_attachment_retriever_node.py`

The node loads each `uploaded_documents` entry's markdown from disk, chunks it, ranks against `user_goal`, and writes a char-capped `relevant_attachment_context`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_attachment_retriever_node.py`:

```python
"""attachment_retriever 节点测试。"""
from __future__ import annotations

from lvyan.nodes.attachment_retriever import attachment_retriever


def test_no_documents_returns_empty_context():
    state = {"user_goal": "问题", "uploaded_documents": []}
    result = attachment_retriever(state)
    assert result == {"relevant_attachment_context": ""}


def test_builds_context_from_uploaded_documents(tmp_path, monkeypatch):
    md_file = tmp_path / "lease.md"
    md_file.write_text(
        "# 当事人\n甲方张三。\n\n# 押金条款\n押金 3000 元，租期届满返还。\n\n# 物业\n物业费缴纳。",
        encoding="utf-8",
    )
    doc = {
        "doc_id": "f1",
        "filename": "lease.md",
        "doc_type": "contract",
        "content_hash": "h",
        "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    state = {"user_goal": "房东不退押金", "uploaded_documents": [doc]}
    result = attachment_retriever(state, max_context_chars=2000)
    ctx = result["relevant_attachment_context"]
    # 命中押金条款排在前面
    assert "押金" in ctx
    assert ctx.index("押金") < ctx.index("物业") if "物业" in ctx else True


def test_context_respects_char_cap(tmp_path):
    md_file = tmp_path / "big.md"
    md_file.write_text("# 押金\n" + "押金。 " * 500, encoding="utf-8")
    doc = {
        "doc_id": "f1", "filename": "big.md", "doc_type": "contract",
        "content_hash": "h", "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    result = attachment_retriever(
        {"user_goal": "押金", "uploaded_documents": [doc]}, max_context_chars=300
    )
    assert len(result["relevant_attachment_context"]) <= 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_attachment_retriever_node.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the node**

Create `src/lvyan/nodes/attachment_retriever.py`:

```python
"""附件按需检索节点：把附件切块、按 user_goal 排序，写入紧凑上下文。

取代「把附件全文塞进 user_goal」的旧做法 —— 下游 LLM 节点只看到与问题相关的
若干分块，且总字符数受 ``max_context_chars`` 控制。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from lvyan.retrieval.attachment_ranker import rank_chunks
from lvyan.schemas.attachment import AttachmentChunk
from lvyan.tools.attachment_chunker import chunk_attachment_markdown

__all__ = ["attachment_retriever"]

_logger = logging.getLogger("lvyan.nodes.attachment_retriever")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _load_markdown(stored_path: str) -> str:
    """从 stored_path 读取 markdown 正文；失败返回空串。"""
    try:
        p = Path(stored_path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _logger.warning("附件正文读取失败 (%s): %s", stored_path, exc)
        return ""


def attachment_retriever(
    state: Any,
    top_k_per_doc: int = 4,
    max_context_chars: int = 6000,
) -> dict[str, Any]:
    """读取 uploaded_documents → 切块 → 排序 → 写 relevant_attachment_context。

    返回 ``{"relevant_attachment_context": str}``（覆盖语义）。
    无附件或读取失败时写入空串（等价于无相关材料）。
    """
    user_goal = str(_get(state, "user_goal", "") or "")
    docs = _get(state, "uploaded_documents", []) or []

    if not docs or not user_goal.strip():
        return {"relevant_attachment_context": ""}

    all_chunks: list[AttachmentChunk] = []
    for doc in docs:
        doc_id = str(_get(doc, "doc_id", "") or _get(doc, "filename", "doc"))
        filename = str(_get(doc, "filename", doc_id))
        stored_path = str(_get(doc, "stored_path", "") or "")
        if not stored_path:
            continue
        md = _load_markdown(stored_path)
        if not md.strip():
            continue
        chunks = chunk_attachment_markdown(md, document_id=doc_id, document_name=filename)
        ranked = rank_chunks(user_goal, chunks, top_k=top_k_per_doc)
        all_chunks.extend(ranked)

    if not all_chunks:
        return {"relevant_attachment_context": ""}

    # 跨文档再排序一次，按相关度取最终 top，并截断到 max_context_chars
    final_ranked = rank_chunks(user_goal, all_chunks, top_k=len(all_chunks))
    parts: list[str] = []
    used = 0
    for ch in final_ranked:
        block = f"【{ch.document_name} · {ch.section}】\n{ch.content}"
        if used + len(block) > max_context_chars:
            break
        parts.append(block)
        used += len(block) + 2

    return {"relevant_attachment_context": "\n\n".join(parts)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_attachment_retriever_node.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/lvyan/nodes/attachment_retriever.py tests/unit/test_attachment_retriever_node.py
git commit -m "feat(attachments): add attachment_retriever node for on-demand context"
```

---

## Task 7: Wire attachment_retriever into the graph

**Files:**
- Modify: `src/lvyan/graph/builder.py`

Insert `attachment_retriever` between `preflight` and `jurisdiction_triage`. Until Task 8 populates `uploaded_documents`, the node is a no-op (`{}`-equivalent), so this commit is safe.

- [ ] **Step 1: Register and wire the node**

In `src/lvyan/graph/builder.py`:

1. Add import near the other node imports (around line 68):
```python
from lvyan.nodes.attachment_retriever import attachment_retriever
```

2. Add `"attachment_retriever"` to the `NODE_NAMES` list.

3. In the node-registration block (around line 102), add:
```python
    graph.add_node("attachment_retriever", attachment_retriever)
```

4. Change the edge chain. Replace:
```python
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "jurisdiction_triage")
```
with:
```python
    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "attachment_retriever")
    graph.add_edge("attachment_retriever", "jurisdiction_triage")
```

5. Update the docstring node list at the top of the file to reflect the new node.

- [ ] **Step 2: Update the node-count test**

In `tests/unit/test_graph_checkpoint.py`, the test `test_graph_contains_all_twelve_nodes` already asserts `len(NODE_NAMES) == 13`. After adding one node it must be 14. Update:

```python
def test_graph_contains_all_twelve_nodes():
    g = build_graph()
    graph_nodes = set(g.get_graph().nodes.keys())
    for name in NODE_NAMES:
        assert name in graph_nodes, f"节点 {name} 未注册到图中"
    assert len(NODE_NAMES) == 14
```

(Rename the function to `test_graph_contains_all_fourteen_nodes` if your convention prefers matching names; otherwise keep the name and just bump the count. Update both the assert and the docstring comment `# 13 个业务节点` → `# 14 个业务节点`.)

- [ ] **Step 3: Run graph tests**

Run: `python -m pytest tests/unit/test_graph_checkpoint.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/lvyan/graph/builder.py tests/unit/test_graph_checkpoint.py
git commit -m "feat(graph): wire attachment_retriever between preflight and triage"
```

---

## Task 8: Populate uploaded_documents via RunContext + runner; stop concatenating attachments into query

**Files:**
- Modify: `src/lvyan/api/sse.py` — `RunContext.__init__` + `default_runner`
- Modify: `src/lvyan/api/server.py` — `/api/agent/run` handler

This is the switch-over: attachments stop bloating `user_goal` and instead flow as `DocumentRef`s into `uploaded_documents`, which `attachment_retriever` consumes.

- [ ] **Step 1: Add attachment_refs to RunContext**

In `src/lvyan/api/sse.py`, extend `RunContext.__init__` signature (around line 40):

```python
    def __init__(
        self,
        run_id: str,
        thread_id: str,
        user_id: str = "anonymous",
        law_as_of_date: date | None = None,
        attachment_refs: list[dict] | None = None,
    ) -> None:
```

Inside the body, add:
```python
        # P0 性能：附件以 DocumentRef 形式传入，不再拼进 user_goal
        self.attachment_refs: list[dict] = list(attachment_refs or [])
```

- [ ] **Step 2: Populate uploaded_documents in default_runner**

In `default_runner` (around line 1174-1182), build `uploaded_documents` from `ctx.attachment_refs`. Replace the `initial = CaseState(...)` block:

```python
        from lvyan.schemas import CaseState, DocumentRef  # noqa: F401  (DocumentRef 已在 schemas)

        uploaded_docs: list[DocumentRef] = []
        for ref in ctx.attachment_refs:
            try:
                uploaded_docs.append(DocumentRef(**ref))
            except Exception:  # noqa: BLE001
                continue

        initial = CaseState(
            run_id=ctx.run_id,
            thread_id=thread_id,
            current_date=_date.today(),
            user_goal=query,
            complexity=complexity,
            user_id=ctx.user_id,
            law_as_of_date=ctx.law_as_of_date,
            uploaded_documents=uploaded_docs,
            relevant_attachment_context="",
        )
```

(Confirm `CaseState` accepts `uploaded_documents` and `relevant_attachment_context` kwargs — both are now model fields.)

- [ ] **Step 3: Stop concatenating attachments in server.py; build refs instead**

In `src/lvyan/api/server.py`, in the `/api/agent/run` handler (around lines 570-720):

- Remove the `attachment_parts` concatenation loop that builds the giant `query_text`. Keep the per-attachment validation (existence, ownership, size caps, prompt-injection detection).
- Build a `attachment_refs` list of dicts instead:

```python
        attachment_refs: list[dict] = []
        if req.attachments:
            unique_attachments: list[str] = []
            for fid in req.attachments:
                if fid and fid not in unique_attachments:
                    unique_attachments.append(fid)
            if len(unique_attachments) > _settings.max_attachment_count:
                raise HTTPException(status_code=400, detail=(
                    f"附件数量 {len(unique_attachments)} 超过上限 {_settings.max_attachment_count}"
                ))
            for fid in unique_attachments:
                meta_path = _metadata_path_for_file_id(fid)
                if not meta_path.is_file():
                    raise HTTPException(status_code=404, detail=(
                        f"附件 {fid} 不存在；可能已删除或上传至其他实例。请重新上传后再发起分析。"))
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise HTTPException(status_code=503, detail=f"附件 {fid} 元数据损坏：{exc}") from exc
                if is_auth_enabled():
                    if meta.get("user_id", ANONYMOUS_USER) != user_id:
                        raise HTTPException(status_code=403, detail=f"附件 {fid} 不属于当前用户")
                # 校验正文可读（仍提前失败，避免运行中才发现）
                md = _load_attachment_markdown(meta, fid)
                if not md:
                    raise HTTPException(status_code=422, detail=(
                        f"附件 {fid} 转换结果为空（attachment_conversion_incomplete）；请重新上传或移除该附件。"))
                attachment_refs.append({
                    "doc_id": fid,
                    "filename": meta.get("original_filename", fid),
                    "doc_type": meta.get("doc_type", "unknown"),
                    "content_hash": meta.get("content_hash", ""),
                    "stored_path": str(_resolve_upload_path(meta.get("markdown_path", ""))),
                    "uploaded_at": meta.get("uploaded_at"),
                })
```

- Keep `query_text = req.query` (no concatenation).
- Pass `attachment_refs=attachment_refs` into the run start call (the same place that currently passes `attachments=req.attachments`). Inspect the `RunManager.start` signature and `RunContext` construction site; thread the new kwarg through.

**Important — `stored_path` must point to the markdown file** (the chunker reads markdown). `_load_attachment_markdown` reads `markdown_path`; reuse the same resolved path here so `attachment_retriever._load_markdown` reads the same bytes.

- [ ] **Step 4: Write an integration-style test**

Add to `tests/unit/test_attachment_retriever_node.py` a test that simulates the full path: refs with `stored_path` → node → context. (Already covered by Task 6 tests; add one asserting the markdown path is read directly.)

```python
def test_stored_path_is_read_directly(tmp_path):
    md_file = tmp_path / "doc.md"
    md_file.write_text("# 押金\n押金 3000 元。", encoding="utf-8")
    doc = {
        "doc_id": "f1", "filename": "doc.md", "doc_type": "contract",
        "content_hash": "h", "stored_path": str(md_file),
        "uploaded_at": "2026-01-01T00:00:00",
    }
    result = attachment_retriever({"user_goal": "押金", "uploaded_documents": [doc]})
    assert "押金 3000" in result["relevant_attachment_context"]
```

- [ ] **Step 5: Run full regression**

Run: `python -m pytest tests/ -q -m "not slow"`
Expected: PASS. Pay attention to any server/runner test that previously asserted attachment text appears in `user_goal` — update those to assert it appears in `uploaded_documents` / `relevant_attachment_context` instead.

- [ ] **Step 6: Commit**

```bash
git add src/lvyan/api/sse.py src/lvyan/api/server.py tests/unit/test_attachment_retriever_node.py
git commit -m "perf(attachments): flow attachments as DocumentRef instead of stuffing user_goal"
```

---

## Task 9: Verify end-to-end + performance smoke

**Files:** none (verification only)

- [ ] **Step 1: Start the server**

Run: `python -m uvicorn lvyan.api.server:create_app --factory --port 8000`

- [ ] **Step 2: Manual deep-analysis with a long attachment**

Upload a multi-section contract (≥ 8 000 chars) and run a deep analysis. Verify in the server log:
- `user_goal` printed/used no longer contains the contract body (only the user's question).
- `attachment_retriever` runs before `jurisdiction_triage`.
- The final answer still references contract clauses (proof the compact context reached the LLM).

- [ ] **Step 3: Compare latency**

Compare wall-clock time of the same query pre/post change. Expect a noticeable drop for long attachments (fewer input tokens across 3 LLM nodes × any retries).

- [ ] **Step 4: Full regression**

Run: `python -m pytest tests/ -q -m "not slow"`
Expected: all green.

- [ ] **Step 5: Commit (if any test fixtures adjusted)**

```bash
git add -u
git commit -m "test: align fixtures with attachment context flow"
```

---

## Self-Review Notes

**Spec coverage:**
- P0-1 attachment chunking → Tasks 3, 4, 5, 6, 7, 8 ✓ (centerpiece)
- P0-3 SSE dedup → Task 1 ✓
- 法规/类案并行 → Task 2 ✓
- Deferred (documented in Scope & Deferrals): P0-2 DOCX defer, P0-4 DB pool, retrieval cache, history pagination.

**Placeholder scan:** none — every code step contains complete code or exact edits.

**Type consistency:**
- `AttachmentChunk` fields (`chunk_id`, `document_id`, `document_name`, `section`, `content`, `char_offset`) used consistently in chunker, ranker, retriever.
- `relevant_attachment_context` is the single state field name used in state.py, fact_extractor, legal_reasoner, planner, and attachment_retriever.
- `attachment_refs` flows server → RunContext → runner → `uploaded_documents: list[DocumentRef]`.
- `DocumentRef` field names (`doc_id`, `filename`, `doc_type`, `content_hash`, `stored_path`, `uploaded_at`) match across ref construction and node consumption.
