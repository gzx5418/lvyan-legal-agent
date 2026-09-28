"""U-03:LeCaRDv2 / CAIL 数据适配器测试。

覆盖:
1. LeCaRDv2 键名容错映射(主键/变体键/缺失拒绝);
2. CAIL meta.accusation → case_type 映射;
3. 脱敏管线:身份证/手机号(redact_privacy)+ 姓名启发式(角色锚定/幂等/停用词);
4. 流式转换容错(损坏条目跳过)+ limit;
5. JSONL 往返 → ingest_cases 可直接消费。
"""

from __future__ import annotations

from datetime import date

import pytest

from lvyan.schemas import CaseDocument
from lvyan.scripts.case_datasets import (
    anonymize_names,
    anonymize_text,
    convert_stream,
    from_cail,
    from_lecardv2,
    write_jsonl,
)


def _lecardv2_entry() -> dict:
    return {
        "case_id": "lecard-0001",
        "case_number": "(2021)京0105刑初456号",
        "court": "北京市朝阳区人民法院",
        "charge": "盗窃罪",
        "facts": "被告人张三于2021年3月潜入某商场,盗窃财物若干。",
        "reasoning": "本院认为,被告人张三以非法占有为目的,秘密窃取他人财物,构成盗窃罪。",
        "judgment_date": "2021-09-10",
        "relevant_articles": ["中华人民共和国刑法第二百六十四条"],
    }


def test_from_lecardv2_full_mapping():
    doc = from_lecardv2(_lecardv2_entry())
    assert doc.case_id == "lecard-0001"
    assert doc.title == "盗窃罪案"
    assert doc.case_type == "盗窃罪"
    assert doc.court == "北京市朝阳区人民法院"
    assert doc.judgment_date == date(2021, 9, 10)
    assert doc.source_name == "LeCaRDv2"
    assert doc.legal_issues == ["盗窃罪"]
    # 姓名已匿名:张三 → 张某
    assert "张三" not in doc.brief_facts
    assert "被告人张某" in doc.brief_facts
    # 引用法条映射
    assert doc.cited_statutes[0].title == "中华人民共和国刑法第二百六十四条"
    # 理由段落 SpanRef
    assert doc.reasoning_spans[0].locator == "本院认为"
    assert doc.reasoning_spans[0].source_id == "lecard-0001"


def test_from_lecardv2_variant_keys_and_fallback_id():
    entry = {
        "doc_id": "doc-9",
        "accusation": ["诈骗罪"],
        "content": "被告人李四虚构投资项目骗取钱款。",
        "time": "2020/05/01",
    }
    doc = from_lecardv2(entry)
    assert doc.case_id == "doc-9"
    assert doc.case_type == "诈骗罪"
    assert doc.judgment_date == date(2020, 5, 1)
    # 无显式 ID 时用文本哈希兜底,可复现
    entry2 = {"content": "某段事实描述。"}
    doc2a = from_lecardv2(entry2)
    doc2b = from_lecardv2({"content": "某段事实描述。"})
    assert doc2a.case_id == doc2b.case_id
    assert doc2a.case_id.startswith("auto-")


def test_from_lecardv2_rejects_empty_entry():
    with pytest.raises(ValueError, match="缺少文本字段"):
        from_lecardv2({"case_id": "x", "court": "某法院"})


def test_from_cail_mapping():
    entry = {
        "fact": "被告人王五盗窃他人手机一部。",
        "meta": {
            "accusation": ["盗窃罪"],
            "criminals": ["王五"],
            "date": "2019-08-01",
            "relevant_articles": ["刑法第二百六十四条"],
        },
    }
    doc = from_cail(entry, index=5)
    assert doc.case_id == "cail-00000005"
    assert doc.case_type == "盗窃罪"
    assert doc.judgment_date == date(2019, 8, 1)
    assert "王五" not in doc.brief_facts  # 姓名已匿名
    assert doc.cited_statutes[0].title == "刑法第二百六十四条"


def test_from_cail_requires_fact():
    with pytest.raises(ValueError, match="fact"):
        from_cail({"meta": {"accusation": ["盗窃罪"]}})


def test_anonymize_names_role_anchored_and_idempotent():
    text = "被告人张三与被害人李四约定交易,证人王五作证。张三辩称无罪。"
    anonymized, count = anonymize_names(text)
    assert "张三" not in anonymized
    assert "李四" not in anonymized
    assert "王五" not in anonymized
    assert "被告人张某" in anonymized
    assert "被害人李某" in anonymized
    assert count >= 3
    # 幂等:再次处理不再变化
    again, count2 = anonymize_names(anonymized)
    assert again == anonymized
    assert count2 == 0


def test_anonymize_names_stoplist_avoids_verbs():
    """名字槽位出现动词/描述词时不误伤。"""
    text = "被告人到案后如实供述。被告人在逃。"
    anonymized, count = anonymize_names(text)
    assert "到案后" in anonymized
    assert "在逃" in anonymized
    assert count == 0


def test_anonymize_text_redacts_numbers_first():
    text = "被告人赵六,身份证号110101199001011234,电话13812345678。"
    anonymized, count = anonymize_text(text)
    # 号码类先被 redact_privacy 处理(占位符),姓名赵六被匿名
    assert "110101199001011234" not in anonymized
    assert "13812345678" not in anonymized
    assert "赵某" in anonymized
    assert count >= 3


def test_convert_stream_skips_bad_rows_and_respects_limit():
    rows = [
        _lecardv2_entry(),
        {"court": "无文本字段"},  # 应跳过
        {"content": "另一案件事实,被告人孙七盗窃。", "charge": "盗窃罪"},
        {"content": "第三条。", "charge": "故意伤害罪"},
    ]
    docs = list(convert_stream(iter(rows), "lecardv2"))
    assert len(docs) == 3
    limited = list(convert_stream(iter(rows), "lecardv2", limit=1))
    assert len(limited) == 1
    # 未知数据源
    with pytest.raises(ValueError, match="未知数据源"):
        list(convert_stream(iter([{"content": "x"}]), "bogus"))


def test_jsonl_roundtrip_fully_ingestible(tmp_path):
    """转换结果 JSONL → CaseDocument 校验 → 可被 ingest_cases 消费。"""
    from lvyan.scripts.ingest_cases import case_document_to_index_doc, load_documents_from_jsonl

    docs = list(
        convert_stream(
            iter([_lecardv2_entry(), {"content": "案件B,被告人钱八诈骗。", "charge": "诈骗罪"}]),
            "lecardv2",
        )
    )
    out = tmp_path / "cases.jsonl"
    assert write_jsonl(iter(docs), out) == 2

    raw = load_documents_from_jsonl(out)
    revalidated = [CaseDocument.model_validate(r) for r in raw]
    assert revalidated[0].case_id == "lecard-0001"
    # 灌库转换不抛异常
    assert case_document_to_index_doc(revalidated[0])["case_type"] == "盗窃罪"
