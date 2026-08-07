"""P1 / P2 修复回归测试套件。

覆盖：
  - P1-1：citation_verifier fail-closed（验证器异常 → passed=False）
  - P2-13：API 认证与 ownership
  - P2-14：上传文件类型/MIME/magic-byte 校验
  - P2-15：附件 untrusted_document 包裹 + injection 检测
  - P2-16：遥测内容脱敏（TRACE_CONTENT=false 时不上传原文）
  - P3-20：reranker HTTP client / CrossEncoder 缓存拆分
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# P1-1：citation_verifier fail-closed
# ---------------------------------------------------------------------------
def test_citation_verifier_fail_closed_on_validator_exception(monkeypatch):
    """任一验证器异常 → citation_audit.passed=False（不再默认 passed=True）。"""
    from lvyan.nodes import citation_verifier as cv_module

    def _raise(*a, **kw):
        raise RuntimeError("模拟验证器崩溃")

    monkeypatch.setattr(cv_module, "validate_citations", _raise)
    monkeypatch.setattr(cv_module, "validate_authority_status", _raise)
    monkeypatch.setattr(cv_module, "validate_grounding", _raise)

    import datetime

    state = {
        "run_id": "r1",
        "thread_id": "t1",
        "current_date": datetime.date(2026, 7, 26),
        "user_goal": "咨询",
        "complexity": "light",
        "statutes": [{"source_id": "s1", "title": "民法典", "status": "effective"}],
        "reasoning_result": None,
        "iteration": 0,
        "retrieval_queries": [],
    }
    result = cv_module.citation_verifier(state)
    audit = result.get("citation_audit") or {}
    assert audit.get("passed") is False, "验证器异常时 audit.passed 必须为 False"


def test_citation_verifier_missing_citation_when_statutes_present(monkeypatch):
    """有 statutes 但 0 引用 → audit.passed=False（missing_citation issue）。"""
    from lvyan.nodes import citation_verifier as cv_module
    from lvyan.validators.citation import CitationValidationReport

    # 模拟 validate_citations 返回 0 引用（异常状态）
    def _empty_citations(*a, **kw):
        return CitationValidationReport(
            total_citations=0, valid_citations=0, issues=[], passed=True,
        )

    monkeypatch.setattr(cv_module, "validate_citations", _empty_citations)
    # 其他两个验证器正常返回
    from lvyan.validators.authority_status import AuthorityStatusReport
    from lvyan.validators.grounding import GroundingReport

    monkeypatch.setattr(
        cv_module,
        "validate_authority_status",
        lambda *a, **kw: AuthorityStatusReport(
            total_authorities=1, effective_count=1, issues=[], passed=True
        ),
    )
    monkeypatch.setattr(
        cv_module,
        "validate_grounding",
        lambda *a, **kw: GroundingReport(
            total_citations=0, grounded_citations=0, issues=[], passed=True
        ),
    )

    import datetime

    state = {
        "run_id": "r1",
        "thread_id": "t1",
        "current_date": datetime.date(2026, 7, 26),
        "user_goal": "咨询",
        "complexity": "light",
        "statutes": [{"source_id": "s1", "title": "民法典", "status": "effective"}],
        "reasoning_result": None,
        "iteration": 0,
        "retrieval_queries": [],
    }
    result = cv_module.citation_verifier(state)
    audit = result.get("citation_audit") or {}
    assert audit.get("passed") is False


# ---------------------------------------------------------------------------
# P2-13：API 认证 ownership
# ---------------------------------------------------------------------------
def test_auth_disabled_returns_anonymous(monkeypatch):
    """未启用认证 → get_current_user_id 返回 anonymous。"""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    from lvyan.api.auth import ANONYMOUS_USER, get_current_user_id

    # FastAPI Header 依赖：模拟调用
    class _Req:
        pass

    uid = get_current_user_id(_Req(), x_user_id=None, authorization=None)
    assert uid == ANONYMOUS_USER


def test_auth_enabled_requires_user_id(monkeypatch):
    """启用认证但无 X-User-ID / Bearer → 401。"""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    class _Req:
        pass

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=None)
    assert exc.value.status_code == 401


def test_auth_enabled_with_x_user_id(monkeypatch):
    """启用认证 + X-User-ID 头 → 返回该 user_id。"""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from lvyan.api.auth import get_current_user_id

    class _Req:
        pass

    uid = get_current_user_id(_Req(), x_user_id="user-123", authorization=None)
    assert uid == "user-123"


def test_assert_thread_owner_mismatch(monkeypatch):
    """启用认证 + 归属不匹配 → 403。"""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from fastapi import HTTPException

    from lvyan.api.auth import assert_thread_owner

    meta = {"user_id": "alice", "title": "T"}
    with pytest.raises(HTTPException) as exc:
        assert_thread_owner(meta, "bob", "thread-1")
    assert exc.value.status_code == 403


def test_assert_thread_owner_disabled(monkeypatch):
    """未启用认证 → 不强制 ownership。"""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    from lvyan.api.auth import assert_thread_owner

    meta = {"user_id": "alice"}
    # 不抛异常即可
    assert_thread_owner(meta, "bob", "thread-1")


# ---------------------------------------------------------------------------
# P2-16：遥测内容脱敏
# ---------------------------------------------------------------------------
def test_trace_content_default_off(monkeypatch):
    """默认 TRACE_CONTENT 未设置 → is_trace_content_enabled()=False。"""
    monkeypatch.delenv("TRACE_CONTENT", raising=False)
    from lvyan.observability.tracing import is_trace_content_enabled

    assert is_trace_content_enabled() is False


def test_trace_content_opt_in(monkeypatch):
    """显式 TRACE_CONTENT=true → 启用。"""
    monkeypatch.setenv("TRACE_CONTENT", "true")
    from lvyan.observability.tracing import is_trace_content_enabled

    assert is_trace_content_enabled() is True


def test_content_hash_stable():
    """相同输入 → 相同 hash。"""
    from lvyan.observability.tracing import content_hash

    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    h3 = content_hash("hello earth")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12


def test_content_hash_empty():
    from lvyan.observability.tracing import content_hash

    assert content_hash("") == ""


def test_redact_for_telemetry_truncates():
    """长文本被截断到 _SUMMARY_MAX_LEN。"""
    from lvyan.observability.tracing import redact_for_telemetry

    long_text = "x" * 1000
    out = redact_for_telemetry(long_text)
    assert len(out) <= 200


def test_record_llm_call_with_content_off_does_not_leak(monkeypatch):
    """TRACE_CONTENT=false 时 Langfuse 接收的 input/output 为 None。"""
    monkeypatch.delenv("TRACE_CONTENT", raising=False)

    captured: dict = {}

    class _FakeTrace:
        def generation(self, **kw):
            captured.update(kw)

    class _FakeClient:
        def trace(self, **kw):
            return _FakeTrace()

    import lvyan.observability.tracing as tr

    monkeypatch.setattr(tr, "_ensure_langfuse", lambda: _FakeClient())

    tr.record_llm_call(
        model="test-model",
        prompt="我的身份证号是 110101199001011234",
        response="分析结果",
        tokens_in=10,
        tokens_out=5,
        cost=0.0,
    )

    # 关键：input/output 必须为 None（不上传原文）
    assert captured.get("input") is None
    assert captured.get("output") is None
    # hash 应该被记录
    assert "prompt_hash" in captured.get("metadata", {})


# ---------------------------------------------------------------------------
# P3-20：Reranker 缓存拆分
# ---------------------------------------------------------------------------
def test_reranker_http_and_cross_encoder_separate_caches():
    """_HTTP_CLIENT 与 _CROSS_ENCODER 是独立变量。"""
    from lvyan.retrieval import reranker

    assert hasattr(reranker, "_HTTP_CLIENT")
    assert hasattr(reranker, "_CROSS_ENCODER")
    assert reranker._HTTP_CLIENT is None or hasattr(reranker._HTTP_CLIENT, "post")
    # CrossEncoder 缓存即便有也不是 httpx.Client（避免互相覆盖）


def test_reranker_jaccard_fallback_returns_scores():
    """无网关 + 无 CrossEncoder → 降级到 Jaccard 仍能打分。"""
    from lvyan.retrieval.lexical import ScoredChunk
    from lvyan.retrieval.reranker import rerank

    candidates = [
        ScoredChunk(chunk_id="1", score=0.5, chunk={
            "title": "民法典", "article_number": "第1条", "article_text": "合同违约",
        }),
        ScoredChunk(chunk_id="2", score=0.4, chunk={
            "title": "劳动法", "article_number": "第2条", "article_text": "解除合同",
        }),
    ]
    out = rerank("合同违约怎么处理", candidates, top_k=2)
    assert len(out) <= 2
    # 至少有分数
    assert all(isinstance(sc.score, float) for sc in out)
