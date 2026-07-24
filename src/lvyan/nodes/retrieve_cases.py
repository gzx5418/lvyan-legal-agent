"""类案检索与差异比较节点。

本节点不是主链节点（``parallel_retrieval`` 已含类案检索），而是供
``legal_reasoner`` 等下游节点调用的辅助节点：对 ``state.cases`` 中的类案
与当前案件做差异比较，输出结构化差异分析，辅助推理节点判断裁判倾向。

差异维度
--------
- 案由（case_type）
- 事实（brief_facts vs state.facts）
- 证据（已有证据 vs 类案关键证据）
- 裁判结果（ruling_summary）

当前为桩实现：用关键词重叠度比较；后续接入 LLM 做语义级差异抽取。
"""

from __future__ import annotations

from typing import Any

from lvyan.schemas import CaseState

__all__ = ["case_difference_compare"]


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


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """2-gram 关键词重叠度评分（0~1）。

    与 ``tools.cases._keyword_overlap`` 实现保持一致，便于跨模块复用。
    """
    if not text_a or not text_b:
        return 0.0
    a_grams = {text_a[i:i + 2] for i in range(len(text_a) - 1) if text_a[i:i + 2].strip()}
    b_grams = {text_b[i:i + 2] for i in range(len(text_b) - 1) if text_b[i:i + 2].strip()}
    if not a_grams or not b_grams:
        return 0.0
    overlap = len(a_grams & b_grams)
    return round(overlap / max(len(a_grams), len(b_grams)), 3)


def _gather_fact_text(facts: list[Any]) -> str:
    """把 ``state.facts`` 拼成单一文本，便于与类案 ``brief_facts`` 比较。"""
    parts: list[str] = []
    for f in facts or []:
        content = _get(f, "content", "")
        if content:
            parts.append(str(content))
    return " ".join(parts)


def _build_difference(
    case: Any,
    current_case_type: str | None,
    current_facts_text: str,
    current_evidence_text: str,
) -> dict[str, Any]:
    """构建单个类案与当前案件的差异分析。

    返回 dict（兼容下游 reasoning_result 的辅助数据结构）：
        - ``case_id``
        - ``case_type_diff``: 案由是否一致
        - ``facts_similarity``: 事实相似度（0~1）
        - ``evidence_similarity``: 证据相似度（0~1）
        - ``ruling_summary``: 类案裁判要旨（供下游引用）
        - ``differences``: list[str]，关键差异点描述
    """
    case_id = str(_get(case, "case_id", "") or "")
    case_type = str(_get(case, "case_type", "") or "")
    brief_facts = str(_get(case, "brief_facts", "") or "")
    ruling_summary = str(_get(case, "ruling_summary", "") or "")

    facts_sim = _keyword_overlap(brief_facts, current_facts_text)
    evidence_sim = _keyword_overlap(brief_facts, current_evidence_text)

    differences: list[str] = []
    if current_case_type and case_type and current_case_type != case_type:
        differences.append(f"案由不同：当前为「{current_case_type}」，类案为「{case_type}」")
    if facts_sim < 0.3:
        differences.append("事实情节差异显著")
    if evidence_sim < 0.3:
        differences.append("证据构成差异显著")

    return {
        "case_id": case_id,
        "case_type": case_type,
        "case_type_diff": bool(
            current_case_type and case_type and current_case_type != case_type
        ),
        "facts_similarity": facts_sim,
        "evidence_similarity": evidence_sim,
        "ruling_summary": ruling_summary,
        "differences": differences,
    }


# ---------------------------------------------------------------------------
# 节点函数
# ---------------------------------------------------------------------------
def case_difference_compare(state: CaseState) -> dict[str, Any]:
    """类案差异比较节点（非主链，供 ``legal_reasoner`` 调用）。

    规则+模板实现，后续接入 LLM 做语义级差异抽取。

    职责
    ----
    - 读取 ``state.cases`` 中的类案。
    - 比较各案例与当前案件的差异（案由 / 事实 / 证据 / 裁判结果）。
    - 返回差异分析结果，写入 ``reasoning_result`` 的辅助数据
      （字段 ``case_differences``）。

    返回更新字典：
        - ``reasoning_result``: 仅当 ``state.reasoning_result`` 存在时
          回写其 ``case_differences``；否则返回 ``case_differences`` 作为
          顶层字段（由调用方按需合并）。
    """
    # TODO: 接入 LLM 做语义级差异抽取
    cases = _get(state, "cases", []) or []
    current_case_type = _get(state, "case_type", None)

    facts = _get(state, "facts", []) or []
    current_facts_text = _gather_fact_text(facts)

    # 提取已持有的证据事实（category="证据"）
    evidence_parts: list[str] = []
    for f in facts:
        if _get(f, "category", "") == "证据":
            content = _get(f, "content", "")
            if content:
                evidence_parts.append(str(content))
    current_evidence_text = " ".join(evidence_parts)

    differences = [
        _build_difference(
            case, current_case_type, current_facts_text, current_evidence_text
        )
        for case in cases
    ]

    # 写入 reasoning_result.case_differences（若已存在）；否则作为顶层字段返回
    reasoning_result = _get(state, "reasoning_result", None)
    if reasoning_result is not None:
        try:
            if isinstance(reasoning_result, dict):
                reasoning_result["case_differences"] = differences
            else:
                setattr(reasoning_result, "case_differences", differences)
            return {"reasoning_result": reasoning_result}
        except Exception:  # noqa: BLE001  回写失败则降级为顶层字段
            pass

    return {"case_differences": differences}
