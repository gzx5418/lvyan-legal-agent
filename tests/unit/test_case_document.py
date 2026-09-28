"""U-01:CaseDocument 要素化 schema 测试。

验收标准(计划 A 方向):
1. 模型可 JSON 序列化(往返一致);
2. 与 curated 桩数据(CaseHit 形态)双向转换;
3. 要素化字段可空但类型稳定(渐进补齐不破坏契约);
4. to_authority 映射正确(入库 → 图执行视图)。
"""

from __future__ import annotations

import json
from datetime import date

from lvyan.schemas import CaseDocument, CitedStatuteRef, SpanRef


def _full_document() -> CaseDocument:
    return CaseDocument(
        case_id="case-001",
        case_number="(2022)京01民终123号",
        court="北京市第一中级人民法院",
        case_type="劳动争议",
        effective_level="reference",
        brief_facts="劳动者主张用人单位违法解除劳动合同。",
        ruling_summary="用人单位未能就解除理由举证,应支付赔偿金。",
        judgment_date=date(2022, 6, 30),
        source_url="https://example.gov.cn/case/001",
        source_name="测试案例库",
        content_hash="abc123",
        legal_elements=[
            "劳动关系是否存在",
            "是否发生解除",
            "解除理由是否符合法定解除条件",
            "工作年限与月工资基数",
        ],
        legal_issues=["违法解除赔偿金能否获得支持"],
        claims=["支付违法解除赔偿金"],
        defenses=["劳动者严重违反规章制度"],
        evidence_summary=["劳动合同", "解除通知书", "工资流水"],
        reasoning_spans=[
            SpanRef(
                source_type="case",
                source_id="case-001",
                locator="本院认为#2",
                text="用人单位应对解除决定的合法性承担举证责任。",
            )
        ],
        cited_statutes=[
            CitedStatuteRef(
                title="中华人民共和国劳动合同法",
                article_number="第八十七条",
            )
        ],
    )


def test_case_document_json_round_trip():
    """模型可 JSON 序列化且往返一致。"""
    doc = _full_document()
    raw = doc.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["case_id"] == "case-001"
    assert parsed["effective_level"] == "reference"
    assert parsed["legal_elements"][0] == "劳动关系是否存在"
    assert parsed["reasoning_spans"][0]["locator"] == "本院认为#2"

    restored = CaseDocument.model_validate_json(raw)
    assert restored == doc


def test_element_fields_default_empty_but_typed():
    """要素化字段可空但类型稳定:最小文档缺省全部为空列表。"""
    doc = CaseDocument(case_id="minimal", case_type="劳动争议")
    assert doc.legal_elements == []
    assert doc.legal_issues == []
    assert doc.claims == []
    assert doc.defenses == []
    assert doc.evidence_summary == []
    assert doc.reasoning_spans == []
    assert doc.cited_statutes == []
    assert doc.effective_level == "unknown"
    # 序列化仍稳定
    assert "legal_elements" in json.loads(doc.model_dump_json())


def test_to_authority_mapping():
    """入库文档 → CaseAuthority 检索视图:字段映射 + ruling_summary 截断。"""
    doc = _full_document()
    authority = doc.to_authority(similarity_score=0.87)
    assert authority.case_id == "case-001"
    assert authority.case_number == "(2022)京01民终123号"
    assert authority.court == "北京市第一中级人民法院"
    assert authority.case_type == "劳动争议"
    assert authority.ruling_date == date(2022, 6, 30)
    assert authority.similarity_score == 0.87
    assert authority.source_url == "https://example.gov.cn/case/001"

    # 长理由截断到 500(完整内容经 reasoning_spans 按需取用)
    doc.ruling_summary = "很长的理由" * 200
    truncated = doc.to_authority(similarity_score=0.5)
    assert len(truncated.ruling_summary) <= 500


def test_from_curated_hit_like_object():
    """curated 桩数据(CaseHit 形态,duck-typing)→ CaseDocument 双向转换。

    CaseHit 字段:case_id/case_number/court/case_type/brief_facts/
    ruling_summary/similarity_score/source——要素化字段缺省为空。
    """
    from lvyan.schemas.evidence import CaseAuthority

    class _FakeHit:
        case_id = "curated-1"
        case_number = None
        court = None
        case_type = "劳动争议"
        brief_facts = "欠薪"
        ruling_summary = "支持劳动者"
        similarity_score = 0.9
        source = "curated_knowledge"

    doc = CaseDocument.model_validate(_FakeHit(), from_attributes=True)
    assert doc.case_id == "curated-1"
    assert doc.case_type == "劳动争议"
    assert doc.brief_facts == "欠薪"
    # 要素化字段缺省稳定
    assert doc.legal_elements == []
    assert doc.effective_level == "unknown"

    # 再转回检索视图,与既有 curated 检索路径兼容
    authority = doc.to_authority(similarity_score=0.9)
    assert isinstance(authority, CaseAuthority)
    assert authority.court == "未知法院"  # 缺省法院兜底


def test_span_ref_source_types():
    """SpanRef 支持 statute/case/document 三类来源(document 为 U-56 预留)。"""
    for source_type, locator in [
        ("statute", "第十三条第二款"),
        ("case", "本院认为#3"),
        ("document", "page:7#p13"),
    ]:
        ref = SpanRef(source_type=source_type, source_id="x", locator=locator)
        assert SpanRef.model_validate_json(ref.model_dump_json()) == ref
