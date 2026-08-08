"""LegalAnswerV1 结构化校验器：在输出进入前端前拦截法律幻觉与字段不一致。

校验项：
1. 每个 issue 的 supporting_facts / rules 引用必须真实存在
2. 法条引用必须包含 full_name + article_number
3. 不得出现「保证胜诉」「百分之百」等不当承诺
4. 已失效法律引用必须有对应 uncertainty 说明
"""

from __future__ import annotations

from lvyan.schemas.legal_answer import LegalAnswerV1


class ValidationError(Exception):
    """结构化校验失败。"""


_FORBIDDEN_PHRASES = [
    "保证胜诉",
    "百分之百",
    "100%胜诉",
    "必定胜诉",
    "稳赢",
    "绝对能赢",
]


def _check_reference_integrity(answer: LegalAnswerV1) -> None:
    fact_ids = {f.fact_id for f in answer.facts}
    citation_ids = {c.citation_id for c in answer.citations}
    for issue in answer.issues:
        for fid in issue.supporting_facts:
            if fid not in fact_ids:
                raise ValidationError(f"争点 {issue.issue_id} 引用了不存在的事实 {fid}")
        for cid in issue.rules:
            if cid not in citation_ids:
                raise ValidationError(f"争点 {issue.issue_id} 引用了不存在的法条 {cid}")


def _check_citations(answer: LegalAnswerV1) -> None:
    for c in answer.citations:
        if not c.article_number.strip():
            raise ValidationError(f"法条引用 {c.citation_id}（{c.full_name}）缺少条款序号")


def _check_forbidden_promises(answer: LegalAnswerV1) -> None:
    texts = [
        answer.executive_summary.conclusion,
        *answer.executive_summary.key_reasons,
        answer.executive_summary.main_uncertainty,
        *[i.conclusion for i in answer.issues],
        *[i.analysis for i in answer.issues],
    ]
    for text in texts:
        for phrase in _FORBIDDEN_PHRASES:
            if phrase in text:
                raise ValidationError(f"输出包含不当承诺措辞「{phrase}」，法律分析不得保证结果")


def _check_repealed_citations(answer: LegalAnswerV1) -> None:
    uncertainty_texts = " ".join(u.description for u in answer.uncertainties)
    for c in answer.citations:
        if c.status == "repealed" and c.full_name not in uncertainty_texts:
            raise ValidationError(f"引用了已失效法律「{c.full_name}」，但未在不确定性中说明")


def validate_legal_answer(answer: LegalAnswerV1) -> None:
    """校验 LegalAnswerV1，失败抛 ValidationError。

    校验失败时不应让模型自行润色放行，而应返回结构化错误触发重写。
    """
    _check_reference_integrity(answer)
    _check_citations(answer)
    _check_forbidden_promises(answer)
    _check_repealed_citations(answer)


__all__ = ["validate_legal_answer", "ValidationError"]
