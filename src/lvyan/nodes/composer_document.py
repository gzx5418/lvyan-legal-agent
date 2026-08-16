"""法律文书 Markdown 与文书载荷渲染。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lvyan.common.helpers import get_value as _get
from lvyan.config import AGENT_DIR
from lvyan.nodes.composer_common import (
    _format_article_number,
    _format_statute_full,
    _tendency_label,
)


_DOC_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("律师函", ("律师函", "发函", "催告函")),
    ("法律意见书", ("法律意见", "意见书")),
    ("答辩状", ("答辩", "答辩状")),
    ("起诉状", ("起诉", "起诉状", "立案", "诉讼")),
)


def _detect_doc_type(user_goal: str) -> str:
    """根据 user_goal 检测文书类型，默认「法律意见书」。"""
    text = user_goal or ""
    for doc_type, keywords in _DOC_TYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return doc_type
    return "法律意见书"


def doc_title(doc_type: str) -> str:
    """文书标题。"""
    titles = {
        "起诉状": "民事起诉状",
        "律师函": "律师函",
        "法律意见书": "法律意见书",
        "答辩状": "民事答辩状",
    }
    return titles.get(doc_type, "法律意见书")


def _resolve_template(doc_type: str, case_type: Any) -> str | None:
    """根据文书类型选择 .docx 模板路径，不存在时返回 None。

    优先使用 ``templates/official`` 下的官方示范文本；找不到则返回 None
    （render_docx 会回退为空白文档）。
    """
    official_dir = AGENT_DIR / "templates" / "official"
    case_type_str = str(case_type or "") or ""

    candidates: list[Path] = []
    if doc_type == "起诉状":
        # 按案由选择对应示范文本
        if "劳动" in case_type_str:
            candidates.append(official_dir / "劳动争议纠纷起诉状_官方示范.docx")
        elif "离婚" in case_type_str or "婚姻" in case_type_str:
            candidates.append(official_dir / "离婚纠纷起诉状_官方示范.docx")
        elif "买卖" in case_type_str or "合同" in case_type_str:
            candidates.append(official_dir / "买卖合同纠纷起诉状_官方示范.docx")
        candidates.append(official_dir / "部分案件起诉状答辩状示范文本_67类_官方汇编.docx")
    elif doc_type == "答辩状":
        candidates.append(official_dir / "部分案件起诉状答辩状示范文本_67类_官方汇编.docx")

    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def _resolve_output_path(state: Any, doc_type: str) -> str:
    """计算文书输出路径：``AGENT/outputs/{run_id}-{doc_type}.docx``。"""
    run_id = str(_get(state, "run_id", "run") or "run")
    safe_run_id = "".join(c for c in run_id if c.isalnum() or c in "-_") or "run"
    outputs_dir = AGENT_DIR / "outputs"
    return str(outputs_dir / f"{safe_run_id}-{doc_type}.docx")


def _extract_parties(facts: list[Any]) -> tuple[list[str], list[str]]:
    """从事实中抽取原告/被告当事人。

    简化策略：category==「当事人」的事实，按内容是否含「原告/被告」归类。
    返回 (plaintiffs, defendants)。
    """
    plaintiffs: list[str] = []
    defendants: list[str] = []
    for f in facts or []:
        if str(_get(f, "category", "")) != "当事人":
            continue
        content = str(_get(f, "content", "") or "").strip()
        if not content:
            continue
        if "被告" in content:
            defendants.append(content)
        elif "原告" in content:
            plaintiffs.append(content)
        else:
            # 无明确角色时归原告方
            plaintiffs.append(content)
    return plaintiffs, defendants


def _build_document_markdown(state: Any, doc_type: str) -> str:
    """根据文书类型构建 Markdown 正文。"""
    facts = _get(state, "facts", []) or []
    statutes = _get(state, "statutes", []) or []
    reasoning_result = _get(state, "reasoning_result", None)
    user_goal = str(_get(state, "user_goal", "") or "").strip()
    current_date = _get(state, "current_date", None)
    date_str = str(current_date) if current_date else "____年__月__日"

    plaintiffs, defendants = _extract_parties(facts)
    plaintiff_str = "；".join(plaintiffs) if plaintiffs else "___"
    defendant_str = "；".join(defendants) if defendants else "___"

    # 事实与理由
    fact_lines: list[str] = []
    for f in facts:
        content = str(_get(f, "content", "") or "").strip()
        if content:
            fact_lines.append(f"- {content}")
    if not fact_lines:
        fact_lines = ["- （请补充案件事实）"]
    facts_block = "\n".join(fact_lines)

    # 法律依据
    statute_lines = [_format_statute_full(a) for a in statutes]
    if not statute_lines:
        statute_lines = ["- （暂未检索到适用法条，请补充）"]
    statutes_block = "\n".join(statute_lines)

    # 主张 / 结论
    plaintiff_args = _get(reasoning_result, "plaintiff_arguments", []) or []
    claims_block = (
        "\n".join(f"- {a}" for a in plaintiff_args) if plaintiff_args else "- （请补充诉讼请求）"
    )
    tendency = _tendency_label(_get(reasoning_result, "judicial_tendency", None))
    relationship = str(_get(reasoning_result, "legal_relationship", "") or "").strip()

    title = doc_title(doc_type)

    if doc_type == "律师函":
        parts = [
            f"# {title}",
            "",
            f"致：{defendant_str}",
            "",
            "## 委托说明",
            f"委托人就「{user_goal or '相关事项'}」委托本律师发函。",
            "",
            "## 事实陈述",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 正式要求",
            claims_block,
            "",
            "## 期限与后果",
            "请于收到本函之日起 15 日内履行上述事项，否则我方将依法采取进一步法律措施。",
            "",
            "律师：___",
            "律师事务所：___",
            f"日期：{date_str}",
        ]
    elif doc_type == "答辩状":
        parts = [
            f"# {title}",
            "",
            "## 当事人",
            f"- 答辩人：{defendant_str}",
            f"- 被答辩人：{plaintiff_str}",
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 答辩意见",
            claims_block,
            "",
            "此致",
            "___人民法院",
            "",
            "答辩人：___",
            f"日期：{date_str}",
        ]
    elif doc_type == "起诉状":
        parts = [
            f"# {title}",
            "",
            "## 当事人信息",
            f"- 原告：{plaintiff_str}",
            f"- 被告：{defendant_str}",
            "",
            "## 诉讼请求",
            claims_block,
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "此致",
            "___人民法院",
            "",
            "具状人：___",
            f"日期：{date_str}",
        ]
    else:  # 法律意见书
        parts = [
            f"# {title}",
            "",
            "## 委托人",
            f"- 委托人：{plaintiff_str}",
            "",
            "## 委托事项",
            user_goal or "（请补充委托事项）",
            "",
            "## 事实与理由",
            facts_block,
            "",
            "## 法律依据",
            statutes_block,
            "",
            "## 法律分析",
            f"- 法律关系：{relationship or '待定'}",
            f"- 裁判倾向：{tendency}",
            "",
            "## 法律意见",
            claims_block,
            "",
            "落款：___",
            f"日期：{date_str}",
        ]

    # 文书通用风险声明（保证 output_validator 风险声明校验通过）
    parts.extend(
        [
            "",
            "---",
            "⚠ 本文书由律言 Agent 自动生成，仅供参考，不构成正式法律意见，使用前请持证律师审核。",
        ]
    )
    return "\n".join(parts)


def compose_document(state: Any) -> tuple[str, dict[str, Any]]:
    """Document 模式：构建文书 Markdown 草稿 + document_payload。

    P0-1 修复：本函数 **不再渲染 DOCX**。composer 在 citation_verifier /
    output_guardrail 之前执行，若此时落盘 DOCX，后续的引用修复、隐私脱敏、
    HITL 编辑都不会反映到已生成的文件中，导致最终文件与页面展示内容不一致。

    现在仅生成 Markdown 草稿 + 文书载荷（含 output_path / template_name /
    filled_fields），真正的 ``render_docx`` 由 legal_answer_finalizer 在
    output_guardrail 之后基于最终 ``final_output`` 执行。

    返回 ``(markdown, document_payload)``。
    """
    user_goal = str(_get(state, "user_goal", "") or "")
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []
    statutes = _get(state, "statutes", []) or []
    reasoning_result = _get(state, "reasoning_result", None)

    doc_type = _detect_doc_type(user_goal)
    markdown = _build_document_markdown(state, doc_type)
    output_path = _resolve_output_path(state, doc_type)
    template = _resolve_template(doc_type, case_type)

    # 构建 document_payload（template_name + filled_fields）
    plaintiffs, defendants = _extract_parties(facts)
    plaintiff_args = _get(reasoning_result, "plaintiff_arguments", []) or []
    fact_lines: list[str] = []
    for f in facts:
        content = str(_get(f, "content", "") or "").strip()
        if content:
            fact_lines.append(content)
    statute_refs: list[str] = []
    for auth in statutes:
        title = str(_get(auth, "title", "") or "")
        article = _format_article_number(_get(auth, "article_number", None))
        if title:
            statute_refs.append(f"《{title}》{article}")

    document_payload: dict[str, Any] = {
        "template_name": template or f"{doc_type}（无模板，Markdown 降级）",
        "doc_type": doc_type,
        "filled_fields": {
            "doc_type": doc_type,
            "title": doc_title(doc_type),
            "plaintiffs": plaintiffs,
            "defendants": defendants,
            "claims": list(plaintiff_args),
            "facts": fact_lines,
            "statutes": statute_refs,
            "user_goal": user_goal,
            "output_path": output_path,
            "template": template,
        },
    }

    return markdown, document_payload


_doc_title = doc_title
_compose_document = compose_document
