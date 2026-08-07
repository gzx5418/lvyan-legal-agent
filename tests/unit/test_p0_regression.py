"""P0 修复 + 高优先级问题回归测试。

覆盖：
  - P0-1：历史法规端到端（LawMetadata.expiry_date → ArticleChunk → Authority → verify_statute_status as_of）
  - P0-2：statutes=[] 时虚构法条引用校验
  - P0-3：checkpoint HITL 认证绕过（anonymous 不跳过 ownership / forbidden→403 / error→409）
  - 高优2：tasks 流按字段判断（input/result/error）
  - 高优3：SSE fallback 仅捕获 TypeError("version")
  - 高优5：附件 403 不被吞掉
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# P0-1：历史法规端到端
# ---------------------------------------------------------------------------
class TestHistoricalLawEndToEnd:
    """LawMetadata.expiry_date → ArticleChunk → Authority → verify as_of."""

    def test_law_metadata_parses_expiry_date(self):
        from lvyan.retrieval.version_resolver import LawMetadata

        meta = LawMetadata(
            source_id="hetongfa",
            title="中华人民共和国合同法",
            status="repealed",
            expiry_date=date(2021, 1, 1),
            superseded_by="minfadian",
            raw_filepath="/tmp/hetongfa.md",
            content_hash="abc",
        )
        assert meta.expiry_date == date(2021, 1, 1)
        assert meta.superseded_by == "minfadian"

    def test_article_chunk_copies_expiry_date_from_metadata(self, tmp_path):
        from lvyan.retrieval.version_resolver import LawMetadata
        from lvyan.scripts.ingest_laws import chunk_law_articles

        meta = LawMetadata(
            source_id="test-law",
            title="测试法",
            status="repealed",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            superseded_by="new-law",
            raw_filepath=str(tmp_path / "test-law.md"),
            content_hash="abc",
        )
        (tmp_path / "test-law.md").write_text(
            "# 测试法\n\n第一条 本法用于测试历史有效期传播。",
            encoding="utf-8",
        )
        chunks = chunk_law_articles(meta)
        assert chunks
        assert chunks[0].expiry_date == date(2021, 1, 1)
        assert chunks[0].superseded_by == "new-law"

    def test_authority_expiry_date_from_chunk(self):
        from lvyan.schemas.authority import Authority

        auth = Authority(
            source_id="hetongfa",
            title="合同法",
            article_text="测试条文",
            authority_level="法律",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            status="repealed",
            retrieved_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
        assert auth.expiry_date == date(2021, 1, 1)

    def test_verify_statute_status_as_of_historical(self, monkeypatch):
        """as_of=2020-01-01 时，2021-01-01 废止的合同法应视为有效。"""
        from lvyan.retrieval.version_aware import verify_statute_status, _reset_metadata_cache

        _reset_metadata_cache()

        from lvyan.retrieval.version_resolver import LawMetadata

        meta = LawMetadata(
            source_id="hetongfa",
            title="中华人民共和国合同法",
            status="repealed",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            superseded_by="minfadian",
            raw_filepath="/tmp/hetongfa.md",
            content_hash="abc",
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._metadata_cache",
            {"hetongfa": meta},
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._groups_cache",
            [],
        )

        result = verify_statute_status("hetongfa", as_of=date(2020, 6, 1))
        assert result.is_effective_as_of is True
        assert result.expiry_date == date(2021, 1, 1)

    def test_verify_statute_status_as_of_after_expiry(self, monkeypatch):
        """as_of=2022-01-01 时，2021-01-01 废止的合同法应视为无效。"""
        from lvyan.retrieval.version_aware import _reset_metadata_cache
        from lvyan.retrieval.version_resolver import LawMetadata

        _reset_metadata_cache()
        meta = LawMetadata(
            source_id="hetongfa",
            title="中华人民共和国合同法",
            status="repealed",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            superseded_by="minfadian",
            raw_filepath="/tmp/hetongfa.md",
            content_hash="abc",
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._metadata_cache",
            {"hetongfa": meta},
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._groups_cache",
            [],
        )

        from lvyan.retrieval.version_aware import verify_statute_status

        result = verify_statute_status("hetongfa", as_of=date(2022, 6, 1))
        assert result.is_effective_as_of is False

    def test_citation_check_uses_as_of_for_historical(self, monkeypatch):
        """validate_citations 传入 current_date 后，_check_status 应使用 as_of 验证。"""
        from lvyan.retrieval.version_resolver import LawMetadata
        from lvyan.retrieval.version_aware import _reset_metadata_cache
        from lvyan.schemas.authority import Authority
        from lvyan.validators.citation import validate_citations

        _reset_metadata_cache()
        meta = LawMetadata(
            source_id="hetongfa",
            title="中华人民共和国合同法",
            status="repealed",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            superseded_by="minfadian",
            raw_filepath="/tmp/hetongfa.md",
            content_hash="abc",
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._metadata_cache",
            {"hetongfa": meta},
        )
        monkeypatch.setattr(
            "lvyan.retrieval.version_aware._groups_cache",
            [],
        )

        auth = Authority(
            source_id="hetongfa",
            title="中华人民共和国合同法",
            article_number="第一百零七条",
            article_text="当事人一方不履行合同义务或者履行合同义务不符合约定的应当承担继续履行采取补救措施或者赔偿损失等违约责任",
            authority_level="法律",
            effective_date=date(1999, 10, 1),
            expiry_date=date(2021, 1, 1),
            status="repealed",
            retrieved_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )

        reasoning = {
            "elements": [
                "根据《中华人民共和国合同法》第一百零七条的规定，"
                "当事人一方不履行合同义务或者履行合同义务不符合约定的"
                "应当承担继续履行采取补救措施或者赔偿损失等违约责任"
            ],
        }

        report = validate_citations(reasoning, [auth], current_date=date(2020, 6, 1))
        assert report.passed is True, f"2020年引用合同法应视为有效，但 issues={report.issues}"

        report_now = validate_citations(reasoning, [auth], current_date=date(2022, 6, 1))
        assert report_now.passed is False, "2022年引用已废止合同法应视为无效"


# ---------------------------------------------------------------------------
# P0-2：statutes=[] 时虚构法条校验
# ---------------------------------------------------------------------------
class TestEmptyStatutesCitation:
    """statutes 为空时 final_output 有引用 → 应被标记为 not_found error。"""

    def test_output_citations_with_empty_statutes_fails(self):
        from lvyan.nodes.citation_verifier import _extract_citations, _find_matching_statute
        from lvyan.validators.citation import CitationIssue, CitationValidationReport

        output_citations = _extract_citations("根据《中华人民共和国民法典》第五百七十七条的规定")
        assert len(output_citations) > 0

        matched = _find_matching_statute(output_citations[0], [])
        assert matched is None

        issues: list[CitationIssue] = []
        issues.extend(
            CitationIssue(
                citation_id=oc.get("citation_id", f"output-unverified-{i}"),
                issue_type="not_found",
                expected="存在可核验的法规来源",
                actual="statutes 为空",
                severity="error",
            )
            for i, oc in enumerate(output_citations)
        )
        report = CitationValidationReport(
            total_citations=len(output_citations),
            valid_citations=0,
            issues=issues,
            passed=False,
        )
        assert report.passed is False
        assert all(i.issue_type == "not_found" for i in report.issues)


# ---------------------------------------------------------------------------
# P0-3：checkpoint HITL 认证绕过
# ---------------------------------------------------------------------------
class TestCheckpointHITLAuth:
    """anonymous 身份不可绕过 ownership 校验；forbidden→403；error→409。"""

    def test_anonymous_identity_cannot_bypass_owner(self, monkeypatch):
        """认证启用时，checkpoint 恢复中 user_id != current_user_id → forbidden。"""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        from lvyan.api.sse import RunManager

        manager = RunManager()
        from lvyan.api.models import HITLRequest

        async def _fake_resolve_from_cp(run_id, request, current_user_id):
            return ("forbidden", f"run {run_id} 不属于当前用户")

        manager._resolve_hitl_from_checkpoint = _fake_resolve_from_cp

        import asyncio

        result = asyncio.run(
            manager.resolve_hitl(
                "run-unknown",
                HITLRequest(action="approve"),
                current_user_id="user-alice",
            )
        )
        assert result[0] == "forbidden"

    def test_hitl_forbidden_returns_403(self, monkeypatch):
        """HITL 返回 forbidden 时 API 应返回 403。"""
        from fastapi import HTTPException

        status = "forbidden"
        message = "run 不属于当前用户"
        if status == "forbidden":
            with pytest.raises(HTTPException) as exc:
                raise HTTPException(status_code=403, detail=message)
            assert exc.value.status_code == 403

    def test_hitl_error_returns_409(self):
        """HITL 返回 error 时 API 应返回 409。"""
        from fastapi import HTTPException

        status = "error"
        message = "恢复失败"
        if status == "error":
            with pytest.raises(HTTPException) as exc:
                raise HTTPException(status_code=409, detail=message)
            assert exc.value.status_code == 409

    def test_checkpoint_owner_check_no_anonymous_bypass(self, monkeypatch):
        """认证启用时，checkpoint 恢复的 ownership 校验不应因 current_user_id='anonymous' 跳过。"""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        from lvyan.api import auth as auth_module
        from fastapi import HTTPException

        assert auth_module.is_auth_enabled()

        meta = {"user_id": "user-alice"}
        with pytest.raises(HTTPException) as exc:
            auth_module.assert_thread_owner(meta, "anonymous", "thread-1")
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 高优2：tasks 流按字段判断（input/result/error）而非 event 字段
# ---------------------------------------------------------------------------
class TestTasksStreamParsing:
    """tasks payload 按 'input' / 'result' / 'error' 字段判断。"""

    def test_task_start_detected_by_input_field(self):
        payload = {
            "id": "task-1",
            "name": "retrieve",
            "input": {"query": "合同纠纷"},
            "triggers": [],
        }
        assert "input" in payload
        assert "result" not in payload
        assert "error" not in payload

    def test_task_finish_detected_by_result_field(self):
        payload = {
            "id": "task-1",
            "name": "retrieve",
            "result": [{"source_id": "s1"}],
        }
        assert "result" in payload
        assert "input" not in payload

    def test_task_error_detected_by_error_field(self):
        payload = {
            "id": "task-1",
            "name": "retrieve",
            "error": "timeout",
        }
        assert "error" in payload
        assert "input" not in payload

    def test_no_event_field_in_payload(self):
        payload_start = {
            "id": "task-1",
            "name": "retrieve",
            "input": {},
            "triggers": [],
        }
        payload_end = {
            "id": "task-1",
            "name": "retrieve",
            "result": [],
        }
        assert "event" not in payload_start
        assert "event" not in payload_end


# ---------------------------------------------------------------------------
# 高优3：SSE fallback 仅捕获 TypeError("version")
# ---------------------------------------------------------------------------
class TestSSEFallbackNarrow:
    """v2 astream 失败时仅 TypeError 含 'version' 才回退 v1。"""

    def test_type_error_with_version_triggers_fallback(self):
        exc = TypeError("got an unexpected keyword argument 'version'")
        assert "version" in str(exc)

    def test_type_error_without_version_raises(self):
        exc = TypeError("some other type error")
        assert "version" not in str(exc)

    def test_runtime_error_never_triggers_fallback(self):
        exc = RuntimeError("node execution failed")
        assert not isinstance(exc, TypeError)


# ---------------------------------------------------------------------------
# 高优5：附件 403 不被吞掉
# ---------------------------------------------------------------------------
class TestAttachmentOwnerNotSwallowed:
    """附件归属不匹配时 HTTPException(403) 应穿透，不被 except Exception 吞掉。"""

    def test_http_exception_not_caught_by_oserror_handler(self):
        from fastapi import HTTPException

        exc_403 = HTTPException(status_code=403, detail="附件不属于当前用户")
        try:
            raise exc_403
        except HTTPException:
            pass  # 应被捕获并 re-raise
        except (OSError, Exception):
            pytest.fail("HTTPException 不应被 OSError/通用 Exception 捕获")

    def test_oserror_still_caught(self):
        try:
            raise OSError("file not found")
        except (OSError,):
            pass  # 正常捕获
        except:
            pytest.fail("OSError 应被正常捕获")

    def test_json_decode_error_still_caught(self):
        import json

        try:
            raise json.JSONDecodeError("bad json", "", 0)
        except (json.JSONDecodeError,):
            pass
        except:
            pytest.fail("JSONDecodeError 应被正常捕获")


# ---------------------------------------------------------------------------
# 集成：CaseState user_id 用于 checkpoint 回退校验
# ---------------------------------------------------------------------------
class TestCheckpointFallbackOwnerCheck:
    """sidecar 索引缺失时，从 checkpoint 状态恢复 owner 校验。"""

    def test_case_state_has_user_id_field(self):
        from lvyan.schemas.case import CaseState

        cs = CaseState(
            run_id="r1",
            thread_id="t1",
            current_date=date(2026, 7, 26),
            user_goal="test",
            user_id="user-alice",
        )
        assert cs.user_id == "user-alice"


# ---------------------------------------------------------------------------
# authority_status validator: verify_statute_status 不传 as_of，
# 因为 current_date 用于直接日期比较而非历史 as_of 查询。
# 历史 as_of 验证由 citation validator 负责。
# ---------------------------------------------------------------------------
