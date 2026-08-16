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


__all__ = [
    "authority_score",
    "count_satisfied_elements",
    "deduplicate_authorities",
    "get_compat_counter",
    "get_value",
    "short_id",
]
