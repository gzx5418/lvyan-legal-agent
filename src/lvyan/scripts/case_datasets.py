"""LeCaRDv2 / CAIL 公开数据适配器(U-03)。

把外部类案数据集转换为 :class:`lvyan.schemas.CaseDocument`,经 U-02 的
``ingest_cases`` 灌入 OpenSearch。

数据源(下载由部署方自行完成,本模块只做转换):
- **LeCaRDv2**(主):SIGIR 2024,800 queries / 55,192 候选刑事案例,法律专家
  按 characterization / penalty / procedure 三维标注。语料字段名在公开分发
  中存在变体,本适配器按候选键名容错映射。
- **CAIL**(补充):CAIL2018 等公开赛题数据,``{"fact": ..., "meta":
  {"accusation": [...], "criminals": [...], "date": ...}}``。

脱敏口径(计划 U-03 验收):
1. 先过 ``validators.privacy.redact_privacy``(身份证/手机/银行卡/住址等);
2. 再做**当事人姓名启发式匿名化**(角色锚定:被告人张三 → 被告人张某)。
   姓名无词典不可精确识别,采用"角色词 + 2-4 汉字 + 停用词表"保守启发式,
   误伤率由单测锁定;启发式是已知限制,不是保证。

CLI::

    python -m lvyan.scripts.case_datasets --source lecardv2 --input raw.jsonl \\
        --output cases.jsonl [--limit 1000]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

from lvyan.schemas import CaseDocument, CitedStatuteRef, SpanRef

_logger = logging.getLogger("lvyan.scripts.case_datasets")

__all__ = [
    "from_lecardv2",
    "from_cail",
    "convert_stream",
    "write_jsonl",
    "anonymize_names",
    "anonymize_text",
]

# ---------------------------------------------------------------------------
# 键名容错映射(公开分发的字段名存在变体)
# ---------------------------------------------------------------------------
_LECARDV2_ID_KEYS = ("case_id", "doc_id", "id", "name", "case_name")
_LECARDV2_NUMBER_KEYS = ("case_number", "case_no", "case_num", "caseNumber")
_LECARDV2_COURT_KEYS = ("court", "court_name", "tribunal")
_LECARDV2_CHARGE_KEYS = ("charge", "accusation", "crime", "charges", "case_type")
_LECARDV2_FACTS_KEYS = ("facts", "fact", "case_facts")
_LECARDV2_TEXT_KEYS = ("content", "text", "full_text", "document", "body")
_LECARDV2_REASONING_KEYS = ("reasoning", "court_opinion", "judgment_reason", "opinion")
_LECARDV2_DATE_KEYS = ("judgment_date", "date", "decided_date", "time")

_CITED_KEYS = ("cited_statutes", "relevant_articles", "law_articles", "articles")


def _first_str(entry: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            joined = "、".join(str(v) for v in value if v)
            if joined.strip():
                return joined.strip()
    return ""


def _first_list(entry: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, list) and value:
            return [str(v) for v in value if v]
    return []


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    match = re.match(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", value.strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 姓名启发式匿名化(保守,角色锚定)
# ---------------------------------------------------------------------------
_NAME_ROLES = (
    "原审被告人",
    "被上诉人",
    "上诉人",
    "被告人",
    "犯罪嫌疑人",
    "被申请人",
    "申请人",
    "被害人",
    "被侵害人",
    "自诉人",
    "原告",
    "被告",
    "证人",
    "被害人近亲属",
)
# 名字位置出现但显然是动词/描述词的停用表(降低误伤;启发式已知限制)
_NAME_STOPLIST = {
    "在逃",
    "认罪",
    "悔罪",
    "到案",
    "辩称",
    "提出",
    "要求",
    "同意",
    "拒绝",
    "供述",
    "交代",
    "如实",
    "自愿",
    "主动",
    "明知",
    "伙同",
    "结伙",
    "纠集",
    "携带",
    "等人",
    "某某",
    "到案后",
    "行为",
    "应当",
    "可以",
    "非法",
}
# 常见姓氏表:名字槽位首字锚定,显著降低动词误伤
_SURNAMES = "李王张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤常温康施文牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧关焦巴俞"
# 已匿名形态:X某 / X某某(公开裁判文书多已匿名;幂等保持)
_ANON_RE = re.compile("(" + "|".join(_NAME_ROLES) + r")([\u4e00-\u9fff]某(?:某)?)")
# 候选名:角色 + 姓氏 + 1 字(2 字名,主流形态);姓氏锚定 + 停用词消歧
_CANDIDATE_RE = re.compile(
    "(" + "|".join(_NAME_ROLES) + r")([" + "".join(_SURNAMES) + r"])([\u4e00-\u9fff])"
)


def anonymize_names(text: str) -> tuple[str, int]:
    """角色锚定的姓名匿名化:被告人张三 → 被告人张某(保留姓氏 + 某)。

    启发式边界(文档化已知限制):
    - 2 字名(姓氏 + 1 字)为主流形态,姓氏锚定 + 停用词消歧;
    - 已是"X某/X某某"形态的跳过(幂等);
    - 3 字实名会残留名字首字(保守策略:不贪心吞后续动词),不承诺完全匿名。
    返回 (脱敏文本, 替换数)。
    """
    count = 0

    def _anon_sub(match: re.Match) -> str:
        return match.group(0)  # 已匿名形态保持

    identified: dict[str, str] = {}  # 原名 → 匿名形态

    def _cand_sub_collect(match: re.Match) -> str:
        nonlocal count
        role, surname, given = match.group(1), match.group(2), match.group(3)
        if given == "某":
            return match.group(0)  # 已匿名形态(裸"X某"),幂等
        if surname + given in _NAME_STOPLIST or given in _NAME_STOPLIST:
            return match.group(0)
        original = surname + given
        anonymized_form = f"{surname}某"
        if original not in identified:
            identified[original] = anonymized_form
            count += 1
        return f"{role}{anonymized_form}"

    out = _ANON_RE.sub(_anon_sub, text)
    out = _CANDIDATE_RE.sub(_cand_sub_collect, out)
    # 全局替换已识别的姓名(覆盖无角色锚点的裸名复现:"被告人张三…张三辩称")
    for original, anon_form in identified.items():
        if original != anon_form:
            out = out.replace(original, anon_form)
    return out, count


def anonymize_text(text: str) -> tuple[str, int]:
    """完整脱敏管线:先 redact_privacy(号码/住址等),再姓名启发式。"""
    from lvyan.validators.privacy import redact_privacy

    redacted = redact_privacy(text)
    named, count = anonymize_names(redacted.redacted_text)
    return named, count + redacted.redaction_count


def _make_case_id(entry: dict[str, Any], fallback_text: str) -> str:
    for key in _LECARDV2_ID_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # 无显式 ID:文本哈希兜底(可复现,避免静默丢条)
    import hashlib

    return "auto-" + hashlib.sha256(fallback_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 数据源适配
# ---------------------------------------------------------------------------
def from_lecardv2(entry: dict[str, Any], *, anonymize: bool = True) -> CaseDocument:
    """LeCaRDv2 语料条目 → CaseDocument(键名容错)。缺文本字段的条目拒绝。"""
    facts = _first_str(entry, _LECARDV2_TEXT_KEYS) or _first_str(entry, _LECARDV2_FACTS_KEYS)
    reasoning = _first_str(entry, _LECARDV2_REASONING_KEYS)
    if not facts and not reasoning:
        raise ValueError(
            "LeCaRDv2 条目缺少文本字段(facts/content/text),keys="
            + ",".join(sorted(entry.keys()))[:200]
        )
    charge = _first_str(entry, _LECARDV2_CHARGE_KEYS)
    case_id = _make_case_id(entry, facts or reasoning)

    title = _first_str(entry, ("title", "case_title"))
    if not title:
        title = f"{charge}案" if charge else f"刑事判决书 {case_id}"

    brief_facts = facts
    ruling_summary = reasoning or (facts[:400] if facts and not reasoning else "")
    if anonymize:
        brief_facts, _ = anonymize_text(brief_facts)
        if ruling_summary and ruling_summary is not None:
            ruling_summary, _ = anonymize_text(ruling_summary)

    cited = _first_list(entry, _CITED_KEYS)
    cited_statutes = [
        CitedStatuteRef(title=str(item))
        if not isinstance(item, dict)
        else CitedStatuteRef(
            title=str(item.get("title") or item.get("name") or ""),
            article_number=str(item.get("article_number") or item.get("article") or ""),
        )
        for item in cited
    ]

    return CaseDocument(
        case_id=case_id,
        title=title,
        case_number=_first_str(entry, _LECARDV2_NUMBER_KEYS) or None,
        court=_first_str(entry, _LECARDV2_COURT_KEYS),
        case_type=charge or "刑事",
        effective_level="normal",
        brief_facts=brief_facts,
        ruling_summary=ruling_summary,
        judgment_date=_parse_date(
            entry.get(_LECARDV2_DATE_KEYS[0]) or _first_str(entry, _LECARDV2_DATE_KEYS)
        ),
        source_name="LeCaRDv2",
        legal_issues=[charge] if charge else [],
        reasoning_spans=(
            [
                SpanRef(
                    source_type="case",
                    source_id=case_id,
                    locator="本院认为",
                    text=ruling_summary[:500],
                )
            ]
            if ruling_summary
            else []
        ),
        cited_statutes=cited_statutes,
    )


def from_cail(entry: dict[str, Any], *, index: int = 0, anonymize: bool = True) -> CaseDocument:
    """CAIL2018 风格条目 → CaseDocument。"""
    fact = str(entry.get("fact", "") or "").strip()
    if not fact:
        raise ValueError("CAIL 条目缺少 fact 字段")
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    accusations = [str(a) for a in (meta.get("accusation") or []) if a]
    charge = accusations[0] if accusations else "刑事"
    if anonymize:
        fact, _ = anonymize_text(fact)
    case_id = _make_case_id(entry, fact) if entry.get("case_id") else f"cail-{index:08d}"
    return CaseDocument(
        case_id=case_id,
        title=f"{charge}案",
        case_type=charge,
        effective_level="normal",
        brief_facts=fact,
        ruling_summary=fact[:400],
        judgment_date=_parse_date(meta.get("date")),
        source_name="CAIL",
        legal_issues=accusations,
        reasoning_spans=(
            [SpanRef(source_type="case", source_id=case_id, locator="事实认定", text=fact[:500])]
        ),
        cited_statutes=[
            CitedStatuteRef(title=str(item)) for item in (meta.get("relevant_articles") or [])
        ],
    )


def convert_stream(
    rows: Iterable[dict[str, Any]], source: str, *, anonymize: bool = True, limit: int | None = None
) -> Iterator[CaseDocument]:
    """流式转换;损坏条目告警跳过(与灌库容错口径一致)。"""
    if source not in ("lecardv2", "cail"):
        raise ValueError(f"未知数据源: {source}(支持 lecardv2 / cail)")
    converted = 0
    for index, row in enumerate(rows):
        if limit is not None and converted >= limit:
            break
        try:
            if source == "lecardv2":
                yield from_lecardv2(row, anonymize=anonymize)
            else:
                yield from_cail(row, index=index, anonymize=anonymize)
            converted += 1
        except (ValueError, TypeError) as exc:
            _logger.warning("第 %d 条转换失败,跳过: %s", index, exc)


def write_jsonl(docs: Iterable[CaseDocument], output_path: Path) -> int:
    """写出 JSONL(供 ingest_cases 消费);返回条数。"""
    count = 0
    with open(output_path, "w", encoding="utf-8") as fh:
        for doc in docs:
            fh.write(doc.model_dump_json() + "\n")
            count += 1
    return count


def _read_rows(path: Path) -> Iterator[dict[str, Any]]:
    """支持 JSONL 与单个 JSON 数组两种输入。"""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        for row in json.loads(text):
            if isinstance(row, dict):
                yield row
        return
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            _logger.warning("第 %d 行损坏,跳过: %s", line_no, exc)
            continue
        if isinstance(row, dict):
            yield row


def main(args: argparse.Namespace | None = None) -> int:
    parsed = args or _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    input_path = Path(parsed.input)
    if not input_path.is_file():
        _logger.error("输入文件不存在: %s", input_path)
        return 2
    count = write_jsonl(
        convert_stream(_read_rows(input_path), parsed.source, limit=parsed.limit),
        Path(parsed.output),
    )
    _logger.info("转换完成: %d 条 → %s", count, parsed.output)
    return 0 if count > 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeCaRDv2/CAIL → CaseDocument JSONL(U-03)")
    parser.add_argument("--source", choices=["lecardv2", "cail"], required=True)
    parser.add_argument("--input", required=True, help="原始数据(JSONL 或 JSON 数组)")
    parser.add_argument("--output", required=True, help="CaseDocument JSONL 输出")
    parser.add_argument("--limit", type=int, default=None, help="最多转换 N 条")
    return parser


if __name__ == "__main__":
    sys.exit(main())
