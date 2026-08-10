"""版本化节点 Prompt 注册表。

Prompt 与节点代码分离，便于灰度、审计和回归评测。注册表只保存系统约束；
用户事实与检索结果由调用方在运行时注入，避免把案件材料写入源码或日志。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    node: str
    version: str
    system: str


_PROMPTS: dict[str, PromptSpec] = {
    "jurisdiction_triage": PromptSpec(
        node="jurisdiction_triage",
        version="2026-08-10.v1",
        system=(
            "你是中国大陆法律服务分诊器。只输出 JSON。不得生成法律结论或虚构事实；"
            "涉外、人身安全、刑事风险和紧急期限必须保守标高风险。"
        ),
    ),
    "missing_fact_assessor": PromptSpec(
        node="missing_fact_assessor",
        version="2026-08-10.v1",
        system=(
            "你是法律案件关键事实审查器。只输出 JSON；只能指出缺失信息，不能补造事实。"
            "只有缺失后无法给出方向性结论的事实才标记 blocking。"
        ),
    ),
    "evidence_analyzer": PromptSpec(
        node="evidence_analyzer",
        version="2026-08-10.v1",
        system=(
            "你是证据审查专家。只允许评估给定证据清单，不得新增证据或事实。"
            "输出 JSON，并让每项 requirement_id 与输入完全一致。"
        ),
    ),
    "authority_resolver": PromptSpec(
        node="authority_resolver",
        version="2026-08-10.v1",
        system=(
            "你是法规权威排序器。只能重排给定 source_id，不得生成新法条、案号或来源。"
            "有效性、时间窗口和效力层级由确定性代码决定。只输出 JSON。"
        ),
    ),
    "critic": PromptSpec(
        node="critic",
        version="2026-08-10.v1",
        system=(
            "你是对抗性法律审稿人。检查遗漏反方观点、事实跳跃、证据不足、法规冲突和"
            "过度确定性。只能依据给定事实和来源，不得新增法条。只输出 JSON。"
        ),
    ),
}


def get_prompt(node: str) -> PromptSpec:
    """取得指定节点的固定版本 Prompt；未知节点直接报错，避免静默用错模板。"""
    try:
        return _PROMPTS[node]
    except KeyError as exc:
        raise ValueError(f"未注册的 prompt 节点: {node}") from exc


def prompt_versions() -> dict[str, str]:
    return {node: spec.version for node, spec in _PROMPTS.items()}


__all__ = ["PromptSpec", "get_prompt", "prompt_versions"]
