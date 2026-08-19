"""跨节点共享、无业务副作用的辅助函数。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def get_value(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 dict 或对象读取字段；``obj`` 为 None 时返回默认值。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_compat_counter(state: Any, key: str) -> int:
    """读取新计数器；旧 checkpoint 未显式包含该字段时回退 ``iteration``。"""
    if isinstance(state, dict):
        supplied = key in state
    else:
        fields_set = getattr(state, "model_fields_set", None)
        supplied = key in fields_set if fields_set is not None else hasattr(state, key)
    value = get_value(state, key, 0) if supplied else get_value(state, "iteration", 0)
    return int(value or 0)


def short_id() -> str:
    return uuid4().hex[:8]


def count_satisfied_elements(elements: list[str]) -> tuple[int, int]:
    return sum(1 for item in elements if "已满足" in item), len(elements)


def authority_score(authority: Any) -> float:
    return max(
        float(get_value(authority, "lexical_score", 0.0) or 0.0),
        float(get_value(authority, "dense_score", 0.0) or 0.0),
        float(get_value(authority, "rerank_score", 0.0) or 0.0),
    )


def deduplicate_authorities(authorities: list[Any]) -> list[Any]:
    """按来源与条号去重，同键保留最高检索分。"""
    bucket: dict[tuple[str, str], Any] = {}
    order: list[tuple[str, str]] = []
    for authority in authorities:
        key = (
            str(get_value(authority, "source_id", "") or ""),
            str(get_value(authority, "article_number", "") or ""),
        )
        if key not in bucket:
            bucket[key] = authority
            order.append(key)
        elif authority_score(authority) > authority_score(bucket[key]):
            bucket[key] = authority
    return [bucket[key] for key in order]


def _build_fallback_output(state: dict[str, Any], query: str) -> str:
    """当图提前结束（如 ask_user 路由）时，生成用户友好的 fallback 输出。

    issue #16：自 ``lvyan.api.sse`` 下沉到中性位置——核心入口
    （``lvyan.main``）此前反向依赖 API 层私有函数。仅依赖 dict/getattr
    读取状态字段，无 API 层依赖；``lvyan.api.sse`` 保留同名再导出以兼容
    现有导入。``query`` 参数保留以兼容既有签名（当前未参与拼接）。
    """
    parts: list[str] = []

    case_type = state.get("case_type")
    if case_type:
        parts.append(f"**案件类型识别**：{case_type}\n")

    missing_facts = state.get("missing_facts", [])
    if missing_facts:
        parts.append("为了提供更准确的法律分析，请补充以下信息：\n")
        for i, mf in enumerate(missing_facts, 1):
            if isinstance(mf, dict):
                question = mf.get("question", "")
                reason = mf.get("reason", "")
            else:
                question = getattr(mf, "question", "")
                reason = getattr(mf, "reason", "")
            parts.append(f"{i}. **{question}**")
            if reason:
                parts.append(f"   _原因：{reason}_")
            parts.append("")

    facts = state.get("facts", [])
    if facts:
        parts.append("**已了解的事实**：")
        for f in facts:
            if isinstance(f, dict):
                content = f.get("content", "")
            else:
                content = getattr(f, "content", "")
            if content:
                parts.append(f"- {content}")
        parts.append("")

    if not parts:
        return (
            "我已收到您的问题，但在当前分析模式下无法生成完整回复。\n"
            "请尝试切换到**深度**模式，或提供更多细节信息。"
        )

    parts.append("---")
    parts.append("_以上为初步分析，补充信息后可获得更完整的法律意见。_")
    return "\n".join(parts)


__all__ = [
    "_build_fallback_output",
    "authority_score",
    "count_satisfied_elements",
    "deduplicate_authorities",
    "get_compat_counter",
    "get_value",
    "short_id",
]
