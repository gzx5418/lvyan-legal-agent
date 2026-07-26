"""引用校验节点：核对最终输出中法条 / 判例引用的真实性。

集成三个验证器：
  - :func:`lvyan.validators.citation.validate_citations`：法条引用存在性 / 内容匹配 / 状态
  - :func:`lvyan.validators.authority_status.validate_authority_status`：法规版本有效性
  - :func:`lvyan.validators.grounding.validate_grounding`：语义接地

不通过时调用 :func:`rewrite_for_reretrieval` 改写查询追加到 ``retrieval_queries``，
由 ``route_after_citation`` 路由回 ``parallel_retrieval`` 重检索；达到
``settings.max_retrieval_iterations`` 上限后强制通过并标记 ``risk_level="high"``。

公开接口
--------
    citation_verifier(state) -> dict[str, Any]
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

_logger = logging.getLogger(__name__)

from lvyan.config import settings
from lvyan.retrieval.query_rewriter import rewrite_for_reretrieval
from lvyan.schemas import CaseState, RetrievalQuery
from lvyan.schemas.output import CitationAudit, CitationDetail
from lvyan.validators.authority_status import validate_authority_status
from lvyan.validators.citation import (
    _extract_citations,
    _find_matching_statute,
    _reasoning_text,
    validate_citations,
)
from lvyan.validators.grounding import validate_grounding

__all__ = ["citation_verifier"]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取属性，``obj`` 为 None 时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_date(value: Any) -> date | None:
    """把任意值转换为 ``date``，无法转换时返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _build_citation_details(
    reasoning_result: Any,
    statutes: list[Any],
    citation_report: Any,
    authority_report: Any,
    grounding_report: Any,
) -> list[CitationDetail]:
    """根据三个验证器的报告构建 ``CitationDetail`` 列表。

    优先级（高 → 低）：
      fabricated > repealed > mismatched > unsupported > verified
    """
    # 构建问题索引：citation_id -> list[(issue_type, severity, detail)]
    citation_issues: dict[str, list[tuple[str, str, str]]] = {}
    for issue in _get(citation_report, "issues", []) or []:
        cid = _get(issue, "citation_id", "")
        if not cid:
            continue
        citation_issues.setdefault(cid, []).append(
            (
                str(_get(issue, "issue_type", "")),
                str(_get(issue, "severity", "")),
                str(_get(issue, "actual", "")),
            )
        )

    grounding_issues: dict[str, list[tuple[str, str, str]]] = {}
    for issue in _get(grounding_report, "issues", []) or []:
        cid = _get(issue, "citation_id", "")
        if not cid:
            continue
        grounding_issues.setdefault(cid, []).append(
            (
                str(_get(issue, "issue_type", "")),
                str(_get(issue, "severity", "")),
                str(_get(issue, "detail", "")),
            )
        )

    # 构建 source_id -> authority_status_issue 索引
    authority_issues: dict[str, list[tuple[str, str, str]]] = {}
    for issue in _get(authority_report, "issues", []) or []:
        sid = _get(issue, "source_id", "")
        if not sid:
            continue
        authority_issues.setdefault(sid, []).append(
            (
                str(_get(issue, "current_status", "")),
                str(_get(issue, "severity", "")),
                str(_get(issue, "detail", "")),
            )
        )

    # 提取所有引用
    text = _reasoning_text(reasoning_result)
    citations = _extract_citations(text)

    details: list[CitationDetail] = []
    for citation in citations:
        citation_id = citation["citation_id"]
        law = citation["law"]
        article_str = citation["article_str"]
        citation_text = f"《{law}》第{article_str}条"

        # 在 statutes 中查找匹配
        matched = _find_matching_statute(citation, statutes)
        matched_source_id = (
            str(_get(matched, "source_id", "") or "") if matched is not None else None
        )
        if not matched_source_id:
            matched_source_id = None

        # 收集该引用的所有问题
        notes: list[str] = []
        status: str = "verified"

        # 1. citation 验证器的问题
        for issue_type, severity, actual in citation_issues.get(citation_id, []):
            if issue_type == "not_found":
                status = _priority_status(status, "fabricated")
                notes.append(f"法条未找到：{actual}")
            elif issue_type == "content_mismatch":
                status = _priority_status(status, "mismatched")
                notes.append(f"条文内容不匹配：{actual}")
            elif issue_type == "invalid_status":
                # invalid_status 的 actual 字段是当前状态
                if actual == "repealed":
                    status = _priority_status(status, "repealed")
                else:
                    status = _priority_status(status, "mismatched")
                notes.append(f"法规状态无效（{actual}）")
            elif issue_type == "missing_article_number":
                status = _priority_status(status, "mismatched")
                notes.append("引用缺少条文号")

        # 2. authority_status 验证器的问题（按 source_id 匹配）
        if matched_source_id:
            for current_status, severity, detail in authority_issues.get(
                matched_source_id, []
            ):
                if current_status == "repealed":
                    status = _priority_status(status, "repealed")
                elif current_status in ("not_yet_effective",):
                    status = _priority_status(status, "mismatched")
                notes.append(detail)

        # 3. grounding 验证器的问题
        for issue_type, severity, detail in grounding_issues.get(citation_id, []):
            if issue_type == "no_support":
                status = _priority_status(status, "unsupported")
                notes.append(detail)
            elif issue_type == "weak_support":
                # 弱支持不改变 status（仍为 verified），但记录 note
                notes.append(detail)
            # unmatched 已由 citation 验证器的 not_found 处理

        # 截断 note 避免过长
        note = "；".join(notes) if notes else None
        if note and len(note) > 200:
            note = note[:200] + "..."

        details.append(
            CitationDetail(
                citation_text=citation_text,
                status=status,  # type: ignore[arg-type]
                matched_source_id=matched_source_id,
                note=note,
            )
        )

    return details


def _priority_status(current: str, new: str) -> str:
    """按优先级返回更高的状态。

    优先级（高 → 低）：
      fabricated > repealed > mismatched > unsupported > verified
    """
    priority = {
        "fabricated": 5,
        "repealed": 4,
        "mismatched": 3,
        "unsupported": 2,
        "verified": 1,
    }
    if priority.get(new, 0) > priority.get(current, 0):
        return new
    return current


def _summarize_audit(
    details: list[CitationDetail],
    citation_report: Any,
    authority_report: Any,
    grounding_report: Any,
    iteration: int,
) -> CitationAudit:
    """汇总三个验证器的结果为 ``CitationAudit``。"""
    # passed：三个验证器都通过才算通过
    citation_passed = bool(_get(citation_report, "passed", True))
    authority_passed = bool(_get(authority_report, "passed", True))
    grounding_passed = bool(_get(grounding_report, "passed", True))
    passed = citation_passed and authority_passed and grounding_passed

    # 统计各状态数量
    verified = sum(1 for d in details if d.status == "verified")
    fabricated = sum(1 for d in details if d.status == "fabricated")
    repealed_cited = sum(1 for d in details if d.status == "repealed")
    unsupported = sum(1 for d in details if d.status == "unsupported")
    total = len(details)

    return CitationAudit(
        passed=passed,
        total_citations=total,
        verified=verified,
        fabricated=fabricated,
        repealed_cited=repealed_cited,
        unsupported=unsupported,
        details=details,
        reretrieval_count=iteration,
    )


def _select_query_for_rewrite(
    retrieval_queries: list[Any],
    user_goal: str,
) -> str:
    """选择用于改写的查询文本。

    优先使用最后一条 ``retrieval_queries`` 的 ``query_text``；
    若为空则回退到 ``user_goal``。
    """
    for q in reversed(retrieval_queries or []):
        qt = _get(q, "query_text", "") or ""
        if qt.strip():
            return qt.strip()
    return str(user_goal or "").strip()


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def citation_verifier(state: CaseState) -> dict[str, Any]:
    """引用校验节点：对最终输出文本做引用校验（P1-9b 修复后）。

    P1-9b 修复：
    旧流程 ``reasoner → critic → citation_verifier → composer`` 验证的是
    reasoning_result（中间推理），而非用户看到的最终文本。新流程中
    composer 在 citation_verifier 之前执行，因此本节点同时校验
    ``reasoning_result`` 和 ``final_output``，确保最终用户看到的文本中
    的引用也经过验证。

    职责
    ----
    1. 调用 :func:`validate_citations` / :func:`validate_authority_status` /
       :func:`validate_grounding` 三个验证器。
    2. 对 ``reasoning_result`` 和 ``final_output`` 分别提取引用并合并校验。
    3. 汇总结果为 :class:`CitationAudit`，写入 ``state.citation_audit``。
    4. 若 ``passed=False`` 且 ``iteration < settings.max_retrieval_iterations``：
       - 调用 :func:`rewrite_for_reretrieval` 改写最后一条查询
       - 追加新的 :class:`RetrievalQuery` 到 ``retrieval_queries``
       - ``iteration += 1``
       - 由 ``route_after_citation`` 路由回 ``parallel_retrieval``
    5. 若 ``passed=False`` 且已达迭代上限：
       - 标记 ``risk_level="high"`` / ``confidence="insufficient"``
       - 由 ``route_after_citation`` 路由到 ``output_guardrail``（强制通过）

    返回更新字典（覆盖语义）：
        - ``citation_audit``: dict（CitationAudit 序列化）
        - ``retrieval_queries``: list[RetrievalQuery]（重检索时追加）
        - ``iteration``: int（重检索时 +1）
        - ``risk_level``: str（达到上限时设为 "high"）
        - ``confidence``: str（达到上限时设为 "insufficient"）
    """
    reasoning_result = _get(state, "reasoning_result", None)
    final_output = str(_get(state, "final_output", "") or "")
    statutes = _get(state, "statutes", []) or []
    current_date = _to_date(_get(state, "current_date", None))
    iteration = int(_get(state, "iteration", 0) or 0)
    retrieval_queries = list(_get(state, "retrieval_queries", []) or [])
    user_goal = str(_get(state, "user_goal", "") or "")

    # --- 1. 调用三个验证器（P1-1：异常 fail-closed，不再默认 passed=True）---
    verification_error = False
    citation_report: Any = None
    authority_report: Any = None
    grounding_report: Any = None

    # P1-9b：先对 reasoning_result 做基础校验
    try:
        citation_report = validate_citations(
            reasoning_result, statutes, current_date
        )
    except Exception as exc:  # noqa: BLE001 验证器异常 → fail-closed
        _logger.exception("citation 验证器异常: %s", exc)
        verification_error = True

    try:
        authority_report = validate_authority_status(statutes, current_date)
    except Exception as exc:  # noqa: BLE001 验证器异常 → fail-closed
        _logger.exception("authority_status 验证器异常: %s", exc)
        verification_error = True

    try:
        grounding_report = validate_grounding(reasoning_result, statutes)
    except Exception as exc:  # noqa: BLE001 验证器异常 → fail-closed
        _logger.exception("grounding 验证器异常: %s", exc)
        verification_error = True

    # 任一验证器异常时构造 fail-closed 占位报告（passed=False）
    if verification_error:
        from lvyan.validators.citation import CitationValidationReport
        from lvyan.validators.authority_status import AuthorityStatusReport
        from lvyan.validators.grounding import GroundingReport

        if citation_report is None:
            citation_report = CitationValidationReport(
                total_citations=0, valid_citations=0, issues=[], passed=False,
            )
        if authority_report is None:
            authority_report = AuthorityStatusReport(
                total_authorities=0, effective_count=0, issues=[], passed=False,
            )
        if grounding_report is None:
            grounding_report = GroundingReport(
                total_citations=0, grounded_citations=0, issues=[], passed=False,
            )

    # P1-1：有 statutes 但 0 引用 → fail-closed（推理结果没有绑定任何法规引用）
    total_citations_in_report = int(_get(citation_report, "total_citations", 0) or 0)
    if statutes and total_citations_in_report == 0:
        from lvyan.validators.citation import (
            CitationValidationReport,
            CitationIssue,
        )
        existing_issues: list[Any] = list(_get(citation_report, "issues", []) or [])
        existing_issues.append(
            CitationIssue(
                citation_id="missing_all",
                issue_type="not_found",
                expected="至少一条法规引用",
                actual="0 引用",
                severity="error",
            )
        )
        citation_report = CitationValidationReport(
            total_citations=total_citations_in_report,
            valid_citations=int(_get(citation_report, "valid_citations", 0) or 0),
            issues=existing_issues,
            passed=False,
        )

    # P1-9b：对 final_output（composer 输出）做额外引用提取与校验
    # 检查最终输出中是否存在 reasoning_result 未覆盖的引用（如 composer 自行添加的）
    if final_output:
        output_citations = _extract_citations(final_output)
        if output_citations and statutes:
            # 对 output 中额外出现的引用做存在性检查
            for oc in output_citations:
                matched = _find_matching_statute(oc, statutes)
                if matched is None:
                    # final_output 中出现了 statutes 里没有的引用 → 标记为未验证
                    from lvyan.validators.citation import (
                        CitationValidationReport,
                        CitationValidationIssue,
                    )
                    existing_issues = list(_get(citation_report, "issues", []) or [])
                    oc_text = f"《{oc.get('law', '')}》第{oc.get('article_str', '')}条"
                    existing_issues.append(
                        CitationValidationIssue(
                            citation_id=oc.get("citation_id", "output-unverified"),
                            citation_text=oc_text,
                            issue_type="not_found",
                            severity="error",
                            detail=f"最终输出中的引用「{oc_text}」在检索结果中未找到对应法条",
                            actual=None,
                        )
                    )
                    citation_report = CitationValidationReport(
                        total_citations=int(_get(citation_report, "total_citations", 0) or 0) + 1,
                        valid_citations=int(_get(citation_report, "valid_citations", 0) or 0),
                        issues=existing_issues,
                        passed=False,
                    )

    # --- 2. 汇总为 CitationAudit ---
    details = _build_citation_details(
        reasoning_result,
        statutes,
        citation_report,
        authority_report,
        grounding_report,
    )
    audit = _summarize_audit(
        details, citation_report, authority_report, grounding_report, iteration
    )

    passed = audit.passed
    # spec 约束：citation_verifier 内部限制为 2 次（取 min(settings.max_retrieval_iterations, 2)）
    max_iterations = min(settings.max_retrieval_iterations, 2)

    # --- 3. 不通过且未达上限：触发重检索 ---
    if not passed and iteration < max_iterations:
        # 改写查询
        original_query = _select_query_for_rewrite(retrieval_queries, user_goal)
        rewritten = rewrite_for_reretrieval(original_query, iteration + 1)

        new_query = RetrievalQuery(
            query_id=f"rq-reretrieval-{iteration + 1}",
            query_text=rewritten,
            rewritten=original_query if original_query != rewritten else None,
            route="hybrid",
            result_count=0,
        )
        retrieval_queries.append(new_query)

        # 更新 audit 的 reretrieval_count
        audit = audit.model_copy(update={"reretrieval_count": iteration + 1})

        return {
            "citation_audit": audit.model_dump(),
            "retrieval_queries": retrieval_queries,
            "iteration": iteration + 1,
        }

    # --- 4. 不通过且已达上限：强制通过，标记高风险 ---
    if not passed and iteration >= max_iterations:
        return {
            "citation_audit": audit.model_dump(),
            "risk_level": "high",
            "confidence": "insufficient",
        }

    # --- 5. 通过：正常返回 ---
    return {
        "citation_audit": audit.model_dump(),
    }
