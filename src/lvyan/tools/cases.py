"""案例工具：裁判规则检索与案例详情查询。

本模块是 SubTask 15.2 的桩实现，当前没有真实案例库，从精编知识库
``knowledge/curated/case_patterns.md`` 中检索裁判规则作为案例替代。

TODO: 后续接入真实案例库（如人民法院案例库 / OpenSearch cases 索引）。
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import BaseModel, Field

from lvyan.config import KNOWLEDGE_DIR
from lvyan.tools.base import ToolResult

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_CASE_PATTERNS_FILE = KNOWLEDGE_DIR / "case_patterns.md"
"""精编知识库中的裁判规则模式文件。"""

_CASE_TYPE_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
"""匹配 ``## 案件类型`` 标题行。"""

_BRIEF_FACTS_LIMIT = 200
_RULING_SUMMARY_LIMIT = 300


# ---------------------------------------------------------------------------
# 返回模型
# ---------------------------------------------------------------------------
class CaseHit(BaseModel):
    """单条案例命中结果。"""

    case_id: str
    case_number: str | None = None
    court: str | None = None
    case_type: str
    brief_facts: str
    ruling_summary: str
    similarity_score: float = 0.0
    source: str = "curated_knowledge"  # "curated_knowledge" | "database"


class CaseSearchResult(ToolResult):
    """案例检索结果。"""

    query: str
    total: int = 0
    results: list[CaseHit] = Field(default_factory=list)


class CaseDetailResult(ToolResult):
    """案例详情查询结果。"""

    found: bool = False
    case_id: str = ""
    case_number: str | None = None
    court: str | None = None
    case_type: str = ""
    brief_facts: str = ""
    ruling_summary: str = ""
    ruling_date: str | None = None
    source_url: str | None = None
    key_takeaways: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------
class _CuratedCase:
    """精编知识库中解析出的「案件类型」条目。"""

    def __init__(self, case_id: str, case_type: str, body: str) -> None:
        self.case_id = case_id
        self.case_type = case_type
        self.body = body
        self.ruling_rules: list[str] = []
        self.disputes: list[str] = []
        self.failure_reasons: list[str] = []
        self._parse_body(body)

    def _parse_body(self, body: str) -> None:
        """按 ``### 典型裁判规则`` / ``### 常见争议焦点`` / ``### 常见败诉原因``
        三个子节切分并提取条目。
        """
        sections: dict[str, list[str]] = {}
        current_key: str | None = None
        for line in body.splitlines():
            m = re.match(r"^###\s+(.+?)\s*$", line)
            if m:
                current_key = m.group(1).strip()
                sections.setdefault(current_key, [])
                continue
            if current_key is None:
                continue
            stripped = line.strip()
            if stripped.startswith("- "):
                sections[current_key].append(stripped[2:].strip())

        self.ruling_rules = sections.get("典型裁判规则", [])
        self.disputes = sections.get("常见争议焦点", [])
        self.failure_reasons = sections.get("常见败诉原因", [])

    def brief_facts(self) -> str:
        """从争议焦点+败诉原因中拼出简要事实摘要。"""
        parts = self.disputes[:2] + self.failure_reasons[:1]
        text = "；".join(parts)
        return text[:_BRIEF_FACTS_LIMIT]

    def ruling_summary(self) -> str:
        """从典型裁判规则中拼出裁判要旨摘要。"""
        text = "；".join(self.ruling_rules[:3])
        return text[:_RULING_SUMMARY_LIMIT]

    def key_takeaways(self) -> list[str]:
        """关键提示：裁判规则前 5 条。"""
        return self.ruling_rules[:5]


@lru_cache(maxsize=1)
def _load_curated_cases() -> tuple[_CuratedCase, ...]:
    """加载并解析 ``case_patterns.md``，返回精编案例条目元组。"""
    if not _CASE_PATTERNS_FILE.is_file():
        return ()
    try:
        text = _CASE_PATTERNS_FILE.read_text(encoding="utf-8")
    except OSError:
        return ()

    cases: list[_CuratedCase] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CASE_TYPE_HEADING_RE.match(lines[i])
        if not m:
            i += 1
            continue
        case_type = m.group(1).strip()
        case_id = _slugify(case_type)
        # 收集本节正文直到下一个 ``## `` 标题或文件末尾
        body_lines: list[str] = []
        j = i + 1
        while j < len(lines):
            if _CASE_TYPE_HEADING_RE.match(lines[j]):
                break
            body_lines.append(lines[j])
            j += 1
        cases.append(_CuratedCase(case_id, case_type, "\n".join(body_lines)))
        i = j
    return tuple(cases)


def _slugify(text: str) -> str:
    """中文 case_type 转为 ASCII-safe 的 case_id（保留可读性）。"""
    # 用拼音转写会引入依赖；这里直接用「case-<idx>」+ 中文保留
    # 但 case_id 用作 dict key，需要稳定，故用 hash 取稳定数字
    cleaned = re.sub(r"\s+", "-", text.strip())
    return f"curated-{cleaned}"


def _keyword_overlap(query: str, candidate: str) -> float:
    """简单的关键词重叠评分：查询分词在候选文本中的命中比例（0~1）。

    使用 2-gram 切分中文，避免引入分词依赖。
    """
    if not query or not candidate:
        return 0.0
    q_grams = {query[i : i + 2] for i in range(len(query) - 1) if query[i : i + 2].strip()}
    if not q_grams:
        return 0.0
    hit = sum(1 for g in q_grams if g in candidate)
    return round(hit / len(q_grams), 3)


# ---------------------------------------------------------------------------
# 公开工具
# ---------------------------------------------------------------------------
def search_cases(query: str, top_k: int = 10) -> CaseSearchResult:
    """案例检索（桩实现）：从精编知识库 ``case_patterns.md`` 中检索裁判规则。

    Args:
        query: 自然语言查询。
        top_k: 返回结果数上限。

    Returns:
        CaseSearchResult：含 query / total / results。
    """
    if not query or not query.strip():
        return CaseSearchResult(
            tool_name="search_cases",
            success=False,
            error="查询不能为空",
            query=query,
            total=0,
            results=[],
        )

    try:
        curated = _load_curated_cases()
    except Exception as exc:  # noqa: BLE001
        return CaseSearchResult(
            tool_name="search_cases",
            success=False,
            error=f"精编知识库加载失败：{exc}",
            query=query,
            total=0,
            results=[],
        )

    scored: list[tuple[float, _CuratedCase]] = []
    for case in curated:
        candidate_text = f"{case.case_type} {' '.join(case.ruling_rules)} {' '.join(case.disputes)}"
        score = _keyword_overlap(query, candidate_text)
        # 即使零分也保留，便于无关键词命中时返回全部案例供上层 rerank
        scored.append((score, case))

    # 按分数降序，分数相同按 case_type 字典序
    scored.sort(key=lambda x: (-x[0], x[1].case_type))

    hits: list[CaseHit] = []
    for score, case in scored[:top_k]:
        hits.append(
            CaseHit(
                case_id=case.case_id,
                case_number=None,
                court=None,
                case_type=case.case_type,
                brief_facts=case.brief_facts(),
                ruling_summary=case.ruling_summary(),
                similarity_score=score,
                source="curated_knowledge",
            )
        )

    # TODO: 后续接入真实案例库（人民法院案例库 / OpenSearch cases 索引）
    return CaseSearchResult(
        tool_name="search_cases",
        success=True,
        query=query,
        total=len(hits),
        results=hits,
    )


def get_case_detail(case_id: str) -> CaseDetailResult:
    """查询案例详情（桩实现）：从精编知识库按 case_id 查找。

    Args:
        case_id: 案例标识（由 ``search_cases`` 返回的 case_id）。

    Returns:
        CaseDetailResult：found=True 表示命中。
    """
    if not case_id:
        return CaseDetailResult(
            tool_name="get_case_detail",
            success=False,
            error="case_id 不能为空",
            case_id=case_id,
        )

    try:
        curated = _load_curated_cases()
    except Exception as exc:  # noqa: BLE001
        return CaseDetailResult(
            tool_name="get_case_detail",
            success=False,
            error=f"精编知识库加载失败：{exc}",
            case_id=case_id,
        )

    for case in curated:
        if case.case_id == case_id:
            return CaseDetailResult(
                tool_name="get_case_detail",
                success=True,
                found=True,
                case_id=case.case_id,
                case_number=None,
                court=None,
                case_type=case.case_type,
                brief_facts=case.brief_facts(),
                ruling_summary=case.ruling_summary(),
                ruling_date=None,
                source_url=None,
                key_takeaways=case.key_takeaways(),
            )

    return CaseDetailResult(
        tool_name="get_case_detail",
        success=True,
        found=False,
        case_id=case_id,
    )


__all__ = [
    "CaseHit",
    "CaseSearchResult",
    "CaseDetailResult",
    "search_cases",
    "get_case_detail",
]
