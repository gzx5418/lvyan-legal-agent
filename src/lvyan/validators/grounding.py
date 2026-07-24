"""语义接地验证器（SubTask 13.3）。

验证 ``reasoning_result`` 中的法条引用是否在语义上支持推理结论：

1. 从 ``reasoning_result`` 中提取每条法条引用及其上下文（结论片段）。
2. 在 ``statutes`` 中匹配对应 ``Authority``。
3. 计算引用上下文与 ``article_text`` 的字符 bigram Jaccard 相似度
   及共同 bigram 数。
4. 若 Jaccard < 0.2 **且** 共同 bigram < 2，标记为 ``unsupported`` 问题
   （引用与结论无语义支持关系）。

公开接口
--------
    validate_grounding(reasoning_result, statutes) -> GroundingReport
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from lvyan.validators.citation import (
    _char_bigrams,
    _extract_citations,
    _find_matching_statute,
    _jaccard,
    _reasoning_text,
)

__all__ = [
    "GroundingIssue",
    "GroundingReport",
    "validate_grounding",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class GroundingIssue(BaseModel):
    """单条语义接地问题。"""

    citation_id: str
    issue_type: Literal["no_support", "weak_support", "unmatched"]
    conclusion: str  # 引用上下文（推理结论片段）
    cited_text: str  # 法条条文（匹配失败时为空）
    jaccard: float
    common_bigrams: int
    severity: Literal["error", "warning"]
    detail: str


class GroundingReport(BaseModel):
    """语义接地校验报告。"""

    total_citations: int
    grounded_citations: int
    issues: list[GroundingIssue]
    passed: bool  # 0 error 才算 passed


# ---------------------------------------------------------------------------
# 阈值常量
# ---------------------------------------------------------------------------
# Jaccard ≥ 0.2 或 ≥ 2 个共同 bigram 视为有语义支持
_GROUNDING_JACCARD_THRESHOLD = 0.2
_GROUNDING_COMMON_THRESHOLD = 2


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


def _truncate(text: str, max_len: int = 80) -> str:
    """截断文本到指定长度，末尾加省略号。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def validate_grounding(
    reasoning_result: Any,
    statutes: list[Any],
) -> GroundingReport:
    """验证 ``reasoning_result`` 中的法条引用是否语义支持结论。

    Args:
        reasoning_result: ``ReasoningResult`` 实例（或 dict / None）
        statutes: ``list[Authority]``（``state.statutes``）

    Returns:
        GroundingReport：含 ``total_citations`` / ``grounded_citations`` /
        ``issues`` / ``passed``。``passed=True`` 当且仅当无 ``error`` 级别问题。

    判定规则
    --------
    对每条引用：
      1. 在 ``statutes`` 中查找匹配的 ``Authority``（title + article_number）。
      2. 若未匹配到 → ``unmatched``（``warning``），不计入接地失败。
      3. 若匹配到，计算引用上下文与 ``article_text`` 的字符 bigram Jaccard
         与共同 bigram 数。
      4. 若 Jaccard < 0.2 **且** 共同 bigram < 2 →
         - Jaccard < 0.05 且共同 bigram == 0 → ``no_support``（``error``）
         - 否则 → ``weak_support``（``warning``）
    """
    text = _reasoning_text(reasoning_result)
    citations = _extract_citations(text)

    total = len(citations)
    issues: list[GroundingIssue] = []
    error_citation_ids: set[str] = set()

    for citation in citations:
        citation_id = citation["citation_id"]
        context = citation["context"]
        conclusion = _truncate(context)

        # 在 statutes 中匹配
        matched = _find_matching_statute(citation, statutes)
        if matched is None:
            # 未匹配到法条：交由 citation_verifier 处理，此处仅 warning
            issues.append(
                GroundingIssue(
                    citation_id=citation_id,
                    issue_type="unmatched",
                    conclusion=conclusion,
                    cited_text="",
                    jaccard=0.0,
                    common_bigrams=0,
                    severity="warning",
                    detail=f"引用 {citation_id} 在 statutes 中未找到匹配法条",
                )
            )
            continue

        article_text = str(_get(matched, "article_text", "") or "")
        if not article_text:
            # 条文文本为空，无法判定语义支持，跳过
            continue

        # 计算 bigram Jaccard 与共同 bigram 数
        context_grams = _char_bigrams(context)
        article_grams = _char_bigrams(article_text)
        common = context_grams & article_grams
        similarity = _jaccard(context_grams, article_grams)
        common_count = len(common)
        cited_excerpt = _truncate(article_text)

        # 判定语义支持
        has_support = (
            similarity >= _GROUNDING_JACCARD_THRESHOLD
            or common_count >= _GROUNDING_COMMON_THRESHOLD
        )

        if has_support:
            # 通过：有语义支持
            continue

        # 不通过：根据严重程度区分 error / warning
        if similarity < 0.05 and common_count == 0:
            # 完全无重叠 → error
            issues.append(
                GroundingIssue(
                    citation_id=citation_id,
                    issue_type="no_support",
                    conclusion=conclusion,
                    cited_text=cited_excerpt,
                    jaccard=similarity,
                    common_bigrams=common_count,
                    severity="error",
                    detail=(
                        f"引用 {citation_id} 与法条条文无任何 bigram 重叠"
                        f"（Jaccard {similarity:.2f}，共同 bigram {common_count}），"
                        f"不构成语义支持"
                    ),
                )
            )
            error_citation_ids.add(citation_id)
        else:
            # 弱支持 → warning
            issues.append(
                GroundingIssue(
                    citation_id=citation_id,
                    issue_type="weak_support",
                    conclusion=conclusion,
                    cited_text=cited_excerpt,
                    jaccard=similarity,
                    common_bigrams=common_count,
                    severity="warning",
                    detail=(
                        f"引用 {citation_id} 与法条条文语义重叠较弱"
                        f"（Jaccard {similarity:.2f}，共同 bigram {common_count}），"
                        f"建议补充更具体的关联说明"
                    ),
                )
            )

    grounded_citations = max(0, total - len(error_citation_ids))
    has_error = any(issue.severity == "error" for issue in issues)

    return GroundingReport(
        total_citations=total,
        grounded_citations=grounded_citations,
        issues=issues,
        passed=not has_error,
    )
