"""证据缺口分析与权威解析节点。

本模块容纳两个节点函数：

- ``evidence_analyzer``：从 ``facts`` 与 ``case_type`` 识别待证事实与证据缺口，
  生成 ``EvidenceRequirement`` 列表。
- ``authority_resolver``：解析 ``statutes`` 中的权威条目，去重、有效性检查、
  版本冲突检测、效力层级排序，输出 ``statutes``（去重排序后）与 ``conflicts``。

两个节点均为规则+模板实现，后续接入 LLM 增强判断。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from lvyan.retrieval.version_aware import verify_statute_status as _verify_status
from lvyan.schemas import Authority, AuthorityConflict, CaseState, EvidenceRequirement
from lvyan.tools.calculators import generate_evidence_checklist

__all__ = ["evidence_analyzer", "authority_resolver"]


# ---------------------------------------------------------------------------
# 效力层级排序表（数值越小，位阶越高）
# ---------------------------------------------------------------------------
_AUTHORITY_LEVEL_ORDER: dict[str, int] = {
    "宪法": 0,
    "法律": 1,
    "行政法规": 2,
    "司法解释": 3,
    "监察法规": 4,
    "地方性法规": 5,
}
"""效力层级 → 排序权重，未列出的层级取 99（排最后）。"""

_DEFAULT_LEVEL_ORDER = 99


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


def _short_id() -> str:
    """生成 8 位短 id（uuid4 hex 前缀）。"""
    return uuid.uuid4().hex[:8]


def _authority_score(auth: Authority) -> float:
    """取 Authority 的最高分（lexical / dense / rerank）。"""
    return max(
        float(_get(auth, "lexical_score", 0.0) or 0.0),
        float(_get(auth, "dense_score", 0.0) or 0.0),
        float(_get(auth, "rerank_score", 0.0) or 0.0),
    )


def _level_weight(level: str | None) -> int:
    """效力层级 → 排序权重；未匹配返回默认最大值。"""
    if not level:
        return _DEFAULT_LEVEL_ORDER
    return _AUTHORITY_LEVEL_ORDER.get(str(level).strip(), _DEFAULT_LEVEL_ORDER)


# ---------------------------------------------------------------------------
# SubTask 11.3: evidence_analyzer
# ---------------------------------------------------------------------------
def _collect_existing_evidence(facts: list[Any]) -> list[str]:
    """从 ``state.facts`` 中收集 ``category="证据"`` 的事实 content 文本。"""
    existing: list[str] = []
    for f in facts or []:
        if _get(f, "category", "") == "证据":
            content = _get(f, "content", "")
            if content:
                existing.append(str(content))
    return existing


def _fuzzy_evidence_obtained(name: str, obtained_set: set[str]) -> bool:
    """模糊匹配证据名称是否在已持有集合中（任一方包含另一方即视为命中）。"""
    if not name or not obtained_set:
        return False
    if name in obtained_set:
        return True
    for ob in obtained_set:
        if not ob:
            continue
        if ob in name or name in ob:
            return True
    return False


def _determine_status(item: Any, obtained_set: set[str]) -> tuple[str, str | None]:
    """根据 ``EvidenceItem.obtained`` 与模糊匹配结果决定 current_status。

    返回 ``(current_status, gap_description)``：
        - ``met``：required 证据已持有
        - ``partial``：recommended / optional 证据缺失（部分满足）
        - ``missing``：required 证据缺失
    """
    name = str(_get(item, "name", "") or "")
    purpose = str(_get(item, "purpose", "") or "")
    status = str(_get(item, "status", "required") or "required")
    obtained_flag = bool(_get(item, "obtained", False))

    if obtained_flag or _fuzzy_evidence_obtained(name, obtained_set):
        return "met", None

    if status == "required":
        return "missing", f"缺失必要证据「{name}」（用途：{purpose}）"

    # recommended / optional 缺失视为 partial
    return "partial", f"建议补充证据「{name}」（用途：{purpose}）"


def evidence_analyzer(state: CaseState) -> dict[str, Any]:
    """证据缺口分析节点。

    规则+模板实现，后续接入 LLM 增强判断。

    职责
    ----
    - 读取 ``state.facts`` 与 ``state.case_type``。
    - 调用 ``tools.calculators.generate_evidence_checklist(case_type, facts)``
      获取该案类型的标准证据清单。
    - 对比用户已有证据（``facts`` 中 ``category="证据"`` 的）与所需证据。
    - 生成 ``EvidenceRequirement`` 列表（每项含 current_status /
      gap_description）。

    返回更新字典（追加语义）：
        - ``evidence_requirements``: list[EvidenceRequirement]
    """
    # TODO: 接入 LLM 增强证据缺口判断
    case_type = _get(state, "case_type", None)
    facts = _get(state, "facts", []) or []

    if not case_type:
        return {"evidence_requirements": []}

    # generate_evidence_checklist 接受 facts: list[dict]，
    # 传入 dict 形态的 facts（含 obtained_evidence 字段会被识别）
    facts_for_checklist: list[dict] = []
    obtained_evidence = _collect_existing_evidence(facts)
    if obtained_evidence:
        facts_for_checklist.append({"obtained_evidence": obtained_evidence})

    try:
        checklist = generate_evidence_checklist(case_type, facts_for_checklist)
    except Exception:  # noqa: BLE001  清单生成失败时返回空列表
        return {"evidence_requirements": []}

    if not _get(checklist, "success", False):
        # 案类型未匹配模板，返回空列表（不阻塞主链）
        return {"evidence_requirements": []}

    required_items = _get(checklist, "required_evidence", []) or []
    obtained_set = set(obtained_evidence)

    requirements: list[EvidenceRequirement] = []
    for item in required_items:
        current_status, gap_description = _determine_status(item, obtained_set)
        name = str(_get(item, "name", "") or "")
        purpose = str(_get(item, "purpose", "") or "")
        # fact_to_prove：用证据用途作为待证事实
        fact_to_prove = purpose or name
        evidence_types = [name] if name else []
        requirements.append(
            EvidenceRequirement(
                requirement_id=_short_id(),
                fact_to_prove=fact_to_prove,
                evidence_types=evidence_types,
                current_status=current_status,  # type: ignore[arg-type]
                gap_description=gap_description,
            )
        )

    return {"evidence_requirements": requirements}


# ---------------------------------------------------------------------------
# SubTask 11.4: authority_resolver
# ---------------------------------------------------------------------------
def _dedup_authorities(authorities: list[Authority]) -> list[Authority]:
    """按 ``source_id + article_number`` 去重，保留最高分版本。"""
    bucket: dict[str, Authority] = {}
    order: list[str] = []
    for auth in authorities:
        source_id = str(_get(auth, "source_id", "") or "")
        article_number = _get(auth, "article_number", None)
        article_key = str(article_number) if article_number else ""
        key = f"{source_id}::{article_key}"
        if key not in bucket:
            bucket[key] = auth
            order.append(key)
            continue
        existing = bucket[key]
        if _authority_score(auth) > _authority_score(existing):
            bucket[key] = auth
    return [bucket[k] for k in order]


def _sort_by_authority_level(authorities: list[Authority]) -> list[Authority]:
    """按 ``authority_level`` 升序排序（位阶高者在前）。

    同位阶内按 ``lexical_score`` 降序，分数相同按 ``source_id`` 字典序稳定排序。
    """
    return sorted(
        authorities,
        key=lambda a: (
            _level_weight(_get(a, "authority_level", None)),
            -_authority_score(a),
            str(_get(a, "source_id", "") or ""),
        ),
    )


def _check_effective(authority: Authority) -> bool:
    """核验 ``Authority`` 当前是否有效。

    优先使用 ``verify_statute_status`` 查询版本解析器；查询失败或返回
    ``unknown`` 时，回退到 ``Authority.status`` 字段。
    """
    source_id = str(_get(authority, "source_id", "") or "")
    if not source_id:
        # 无 source_id 无法查询，回退到 authority.status
        return _get(authority, "status", "unknown") == "effective"

    try:
        verification = _verify_status(source_id)
    except Exception:  # noqa: BLE001  查询失败回退到 authority.status
        return _get(authority, "status", "unknown") == "effective"

    is_effective = bool(_get(verification, "is_effective_as_of", False))
    current_status = _get(verification, "current_status", "unknown")
    # is_effective_as_of 已综合 status + superseded 判定，优先采用
    if current_status == "unknown":
        # 元数据缺失，回退到 authority.status
        return _get(authority, "status", "unknown") == "effective"
    return is_effective


def _detect_version_conflicts(
    authorities: list[Authority],
) -> list[AuthorityConflict]:
    """检测同一 ``title`` 的多个 effective 版本，生成 version 类型冲突。

    规则：同一 title 下若存在多条 ``status="effective"`` 的条目且
    ``effective_date`` 不同，则视为版本冲突。
    """
    groups: dict[str, list[Authority]] = defaultdict(list)
    for auth in authorities:
        title = str(_get(auth, "title", "") or "").strip()
        if title:
            groups[title].append(auth)

    conflicts: list[AuthorityConflict] = []
    for title, items in groups.items():
        effective_items = [
            a for a in items if _get(a, "status", "unknown") == "effective"
        ]
        if len(effective_items) < 2:
            continue
        # 检查 effective_date 是否有差异
        dates = {
            _get(a, "effective_date", None) for a in effective_items
        }
        # 至少有两种不同的有效日期（None 与具体日期也视为差异）
        if len(dates) < 2 and None not in dates:
            continue
        if len(dates) == 1 and None in dates:
            # 全部 None，无法判定版本差异
            continue

        authority_ids: list[str] = []
        for a in effective_items:
            aid = str(_get(a, "source_id", "") or "")
            article = _get(a, "article_number", None)
            if article:
                aid = f"{aid}#{article}"
            authority_ids.append(aid)

        conflicts.append(
            AuthorityConflict(
                conflict_id=_short_id(),
                authority_ids=authority_ids,
                conflict_type="version",  # type: ignore[arg-type]
                description=(
                    f"「{title}」存在 {len(effective_items)} 个 effective 版本，"
                    f"需确认适用哪一版本"
                ),
                resolution="优先适用 effective_date 最新版本，其余视为被取代",
            )
        )
    return conflicts


def _detect_hierarchy_conflicts(
    authorities: list[Authority],
) -> list[AuthorityConflict]:
    """检测同事项不同效力等级的法规（hierarchy 冲突）。

    规则：按 ``title`` 聚合后，若同一 title 出现多种不同 ``authority_level``，
    则视为位阶冲突（如同一法规名同时被标为「法律」与「行政法规」）。
    另：若同事项（用 ``title`` 关键词近似匹配，此处简化为同 title）有不同位阶
    的不同法规，亦视为位阶冲突——为避免误报，本实现仅在同 title 不同 level
    时上报冲突。
    """
    groups: dict[str, set[str]] = defaultdict(set)
    auth_index: dict[str, list[Authority]] = defaultdict(list)
    for auth in authorities:
        title = str(_get(auth, "title", "") or "").strip()
        level = str(_get(auth, "authority_level", "") or "").strip()
        if title and level:
            groups[title].add(level)
            auth_index[title].append(auth)

    conflicts: list[AuthorityConflict] = []
    for title, levels in groups.items():
        if len(levels) < 2:
            continue
        items = auth_index[title]
        authority_ids: list[str] = []
        for a in items:
            aid = str(_get(a, "source_id", "") or "")
            article = _get(a, "article_number", None)
            if article:
                aid = f"{aid}#{article}"
            authority_ids.append(aid)
        conflicts.append(
            AuthorityConflict(
                conflict_id=_short_id(),
                authority_ids=authority_ids,
                conflict_type="hierarchy",  # type: ignore[arg-type]
                description=(
                    f"「{title}」存在多种效力等级：{' / '.join(sorted(levels))}，"
                    f"需按上位法优于下位法原则适用"
                ),
                resolution="按效力层级排序，上位法优先；下位法与上位法冲突部分无效",
            )
        )
    return conflicts


def authority_resolver(state: CaseState) -> dict[str, Any]:
    """权威解析节点：去重、有效性检查、版本冲突检测、效力层级排序。

    规则+模板实现，后续接入 LLM 增强判断。

    职责
    ----
    - 读取 ``state.statutes`` 中检索到的法规。
    - 按 ``source_id + article_number`` 去重。
    - 用 ``version_resolver`` 检查每个 ``Authority`` 的 ``status``，标记无效
      条目（不剔除，保留以供下游审计）。
    - 检测同一 ``title`` 的多个 effective 版本，生成 ``version`` 类型
      ``AuthorityConflict``；检测同 ``title`` 不同 ``authority_level``，
      生成 ``hierarchy`` 类型冲突。
    - 按 ``authority_level`` 排序（宪法 > 法律 > 行政法规 > 司法解释 >
      监察法规 > 地方性法规）。

    返回更新字典：
        - ``statutes``: 去重排序后的 list[Authority]（覆盖语义：调用方应
          使用本结果替换原 statutes；为兼容 LangGraph 追加 reducer，本节点
          返回的列表为完整替换集，由 ``composer`` / ``citation_verifier``
          直接消费）
        - ``conflicts``: list[AuthorityConflict]（追加语义）
    """
    # TODO: 接入 LLM 增强权威解析
    statutes = _get(state, "statutes", []) or []

    # --- 去重 ---
    deduped = _dedup_authorities(statutes)

    # --- 有效性检查 ---
    # verify_statute_status 依赖全库扫描，对每条 Authority 调用代价较高；
    # 此处仅做轻量标记：保留原 status，对能查到元数据的条目回写 status。
    # 不剔除无效条目（保留以供引用审计）。
    for auth in deduped:
        source_id = str(_get(auth, "source_id", "") or "")
        if not source_id:
            continue
        try:
            verification = _verify_status(source_id)
            current_status = _get(verification, "current_status", "unknown")
            if current_status and current_status != "unknown":
                # 回写 status（兼容 Authority 对象与 dict）
                try:
                    if isinstance(auth, dict):
                        auth["status"] = current_status  # type: ignore[index]
                    else:
                        auth.status = current_status  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001  回写失败不阻塞
                    pass
        except Exception:  # noqa: BLE001  查询失败保留原 status
            continue

    # --- 冲突检测 ---
    version_conflicts = _detect_version_conflicts(deduped)
    hierarchy_conflicts = _detect_hierarchy_conflicts(deduped)
    conflicts = version_conflicts + hierarchy_conflicts

    # --- 效力层级排序 ---
    sorted_statutes = _sort_by_authority_level(deduped)

    return {
        "statutes": sorted_statutes,
        "conflicts": conflicts,
    }
