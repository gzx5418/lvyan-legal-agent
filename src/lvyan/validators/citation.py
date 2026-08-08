"""法条引用验证器（SubTask 13.1）。

验证 ``reasoning_result`` 中引用的每条法条：

1. **法条是否存在**：引用的 ``article_number + title`` 是否能在 ``state.statutes`` 中找到。
2. **条文内容是否匹配**：引用上下文文本与 ``statute.article_text`` 的关键词重叠度
   （字符 bigram Jaccard ≥ 0.1 或 ≥ 2 个共同 bigram）。
3. **法规是否有效**：调用 ``verify_statute_status`` 核验 ``status == "effective"``，
   查询失败或返回 ``unknown`` 时回退到 ``Authority.status`` 字段。

公开接口
--------
    validate_citations(reasoning_result, statutes, current_date=None) -> CitationValidationReport
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from lvyan.retrieval.version_aware import verify_statute_status

__all__ = [
    "CitationIssue",
    "CitationValidationReport",
    "validate_citations",
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class CitationIssue(BaseModel):
    """单条引用校验问题。"""

    citation_id: str
    issue_type: Literal["not_found", "content_mismatch", "invalid_status", "missing_article_number"]
    expected: str  # 期望值
    actual: str  # 实际值
    severity: Literal["error", "warning"]


class CitationValidationReport(BaseModel):
    """引用校验报告。"""

    total_citations: int
    valid_citations: int
    issues: list[CitationIssue]
    passed: bool  # 0 error 才算 passed


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


# 中文数字解析
_DIGITS = "零一二三四五六七八九"
_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _chinese_to_int(s: str) -> int | None:
    """解析中文数字为 int。

    支持：``三十二`` → 32，``一千零三十二`` → 1032，``四十七`` → 47。
    不含中文数字字符时返回 None。
    """
    if not s:
        return None
    if not any(c in _DIGITS or c in _UNITS for c in s):
        return None

    total = 0  # 累计总和（含万以上段）
    section = 0  # 当前段（万以内累计）
    current = 0  # 当前数字

    for ch in s:
        if ch in _DIGITS:
            current = _DIGITS.index(ch)
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if unit == 10000:
                # 万：把当前段并入 total，开新段
                section = (section + current) * unit
                total += section
                section = 0
                current = 0
            else:
                if current == 0:
                    current = 1  # 处理「十二」开头省略的「一」
                section += current * unit
                current = 0
        # 其他字符（如「零」）跳过
    return total + section + current


def _normalize_article_number(num_str: str) -> int | str:
    """将条文号归一化为 int（无法解析时返回 trimmed 原文）。

    支持：
    - ``"47"`` / ``"0047"`` → 47
    - ``"四十七"`` → 47
    - ``"一千零三十二"`` → 1032
    - ``"9999"`` → 9999
    """
    s = num_str.strip()
    if not s:
        return ""
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    result = _chinese_to_int(s)
    if result is not None:
        return result
    return s


# 法条引用提取正则：匹配「《XX法》第Y条」或「XX法第Y条」
_CITATION_RE = re.compile(
    r"(?:《)?(?P<law>[\u4e00-\u9fff]{2,20}(?:法典|法|条例|规定|解释|办法|细则|通则|意见))"
    r"(?:》)?"
    r"\s*第\s*(?P<article>[一二三四五六七八九十百千零0-9]+)\s*条"
)


def _extract_citations(text: str) -> list[dict[str, Any]]:
    """从文本中提取法条引用。

    返回去重后的引用列表，每项含：
        - citation_id: 引用唯一标识（``"{law}-第{article}条"``）
        - law: 法规名称
        - article_str: 条文号原文
        - article_norm: 归一化后的条文号（int 或原文）
        - context: 引用上下文（前后各 60 字符）
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _CITATION_RE.finditer(text):
        law = m.group("law").strip()
        article_str = m.group("article").strip()
        article_norm = _normalize_article_number(article_str)
        citation_id = f"{law}-第{article_str}条"
        if citation_id in seen:
            continue
        seen.add(citation_id)
        # 提取上下文（前后各 60 字符）用于内容匹配
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        context = text[start:end]
        results.append(
            {
                "citation_id": citation_id,
                "law": law,
                "article_str": article_str,
                "article_norm": article_norm,
                "context": context,
            }
        )
    return results


def _reasoning_text(reasoning_result: Any) -> str:
    """将 ReasoningResult 各字段拼接为单一文本，供引用提取使用。"""
    if reasoning_result is None:
        return ""
    if isinstance(reasoning_result, str):
        return reasoning_result
    parts: list[str] = []
    # 字符串字段
    legal_relationship = _get(reasoning_result, "legal_relationship", None)
    if legal_relationship:
        parts.append(str(legal_relationship))
    # 列表字段
    list_fields = (
        "elements",
        "disputed_focus",
        "plaintiff_arguments",
        "defendant_arguments",
        "evidence_mapping",
        "key_factors",
    )
    for field in list_fields:
        val = _get(reasoning_result, field, None)
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            parts.extend(str(v) for v in val if v)
        else:
            parts.append(str(val))
    # 枚举字段（转为中文描述）
    tendency = _get(reasoning_result, "judicial_tendency", None)
    if tendency:
        parts.append(f"裁判倾向：{tendency}")
    confidence = _get(reasoning_result, "evidence_confidence", None)
    if confidence:
        parts.append(f"证据置信度：{confidence}")
    return " ".join(parts)


def _char_bigrams(text: str) -> set[str]:
    """提取所有连续汉字段的 2-gram 集合。

    非汉字字符（标点、数字、英文）作为分隔符，不进入 bigram。
    """
    grams: set[str] = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(seg) - 1):
            grams.add(seg[i : i + 2])
    return grams


def _jaccard(a: set[str], b: set[str]) -> float:
    """计算两个集合的 Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# 内容匹配阈值：Jaccard ≥ 0.1 或 ≥ 2 个共同 bigram 视为匹配
_CONTENT_JACCARD_THRESHOLD = 0.1
_CONTENT_COMMON_THRESHOLD = 2


def _find_matching_statute(citation: dict[str, Any], statutes: list[Any]) -> Any | None:
    """在 statutes 中查找与引用匹配的 Authority。

    匹配规则：
    - title 子串匹配（双向，citation.law 是 statute.title 的子串或反向）
    - article_number 归一化后相等

    返回匹配的 Authority 或 None。
    """
    target_law = citation["law"]
    target_article = citation["article_norm"]

    for auth in statutes:
        title = str(_get(auth, "title", "") or "")
        article_number = _get(auth, "article_number", None)

        # title 匹配：子串匹配（双向）
        if not title:
            continue
        if target_law not in title and title not in target_law:
            continue

        # article_number 匹配
        if article_number is None:
            continue
        # 去掉可能的前缀「第」和后缀「条」
        article_clean = re.sub(r"^第\s*", "", str(article_number))
        article_clean = re.sub(r"\s*条$", "", article_clean).strip()
        article_norm = _normalize_article_number(article_clean)

        if article_norm == target_article:
            return auth

    return None


def _check_status(authority: Any, as_of: date | None = None) -> str:
    """核验 Authority 当前状态（或在 as_of 时间点的有效性）。

    优先使用 ``verify_statute_status``，传入 as_of 以支持历史法规验证；
    查询失败或返回 ``unknown`` 时回退到 ``authority.status`` 字段。
    """
    source_id = str(_get(authority, "source_id", "") or "")
    own_status = str(_get(authority, "status", "unknown") or "unknown")

    if not source_id:
        return own_status

    try:
        verification = verify_statute_status(source_id, as_of=as_of)
    except Exception:  # noqa: BLE001 查询失败回退到 authority.status
        return own_status

    if as_of is not None:
        is_effective = _get(verification, "is_effective_as_of", None)
        if is_effective is True:
            return "effective"
        if is_effective is False:
            return "repealed"

    current_status = str(_get(verification, "current_status", "unknown") or "unknown")
    if current_status == "unknown":
        return own_status
    return current_status


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def validate_citations(
    reasoning_result: Any,
    statutes: list[Any],
    current_date: date | None = None,
) -> CitationValidationReport:
    """验证 ``reasoning_result`` 中的法条引用。

    Args:
        reasoning_result: ``ReasoningResult`` 实例（或 dict / None）
        statutes: ``list[Authority]``（``state.statutes``）
        current_date: 当前日期，传入后用于 as_of 历史法规校验

    Returns:
        CitationValidationReport：含 ``total_citations`` / ``valid_citations`` /
        ``issues`` / ``passed``。
    """
    text = _reasoning_text(reasoning_result)
    citations = _extract_citations(text)

    total = len(citations)
    issues: list[CitationIssue] = []
    error_citation_ids: set[str] = set()

    for citation in citations:
        citation_id = citation["citation_id"]

        if not citation["article_str"]:
            issues.append(
                CitationIssue(
                    citation_id=citation_id,
                    issue_type="missing_article_number",
                    expected="法条引用应包含条文号",
                    actual=citation_id,
                    severity="warning",
                )
            )
            continue

        matched = _find_matching_statute(citation, statutes)

        if matched is None:
            issues.append(
                CitationIssue(
                    citation_id=citation_id,
                    issue_type="not_found",
                    expected=f"在 statutes 中找到 {citation_id}",
                    actual="未找到匹配的法规条文",
                    severity="error",
                )
            )
            error_citation_ids.add(citation_id)
            continue

        article_text = str(_get(matched, "article_text", "") or "")
        article_grams = _char_bigrams(article_text)
        if article_grams:
            context_grams = _char_bigrams(citation["context"])
            common = context_grams & article_grams
            similarity = _jaccard(context_grams, article_grams)

            if len(common) < _CONTENT_COMMON_THRESHOLD and similarity < _CONTENT_JACCARD_THRESHOLD:
                issues.append(
                    CitationIssue(
                        citation_id=citation_id,
                        issue_type="content_mismatch",
                        expected=(
                            f"引用上下文与条文内容应有 ≥ {_CONTENT_COMMON_THRESHOLD} 个共同 bigram"
                            f" 或 Jaccard ≥ {_CONTENT_JACCARD_THRESHOLD}"
                        ),
                        actual=(
                            f"共同 bigram {len(common)} 个，Jaccard {similarity:.2f}；"
                            f"条文首段：{article_text[:50]}"
                        ),
                        severity="error",
                    )
                )
                error_citation_ids.add(citation_id)
                continue

        current_status = _check_status(matched, as_of=current_date)
        if current_status != "effective":
            issues.append(
                CitationIssue(
                    citation_id=citation_id,
                    issue_type="invalid_status",
                    expected="effective",
                    actual=current_status,
                    severity="error",
                )
            )
            error_citation_ids.add(citation_id)
            continue

    valid_citations = max(0, total - len(error_citation_ids))
    has_error = any(issue.severity == "error" for issue in issues)

    return CitationValidationReport(
        total_citations=total,
        valid_citations=valid_citations,
        issues=issues,
        passed=not has_error,
    )
