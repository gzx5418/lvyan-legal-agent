"""U-02:类案库 OpenSearch 灌库测试。

覆盖:
1. CaseDocument → 索引文档转换(核心契约字段 + 要素化字段平铺);
2. ensure_case_index 幂等建索引 / --force 删重建;
3. ingest_case_documents bulk 调用与失败统计;
4. OpenSearch 不可达时优雅降级返回 (0, 0);
5. JSONL 加载容错(损坏行跳过)。
"""

from __future__ import annotations

import json
from datetime import date


from lvyan.schemas import CaseDocument, CitedStatuteRef, SpanRef
from lvyan.scripts import ingest_cases
from lvyan.scripts.ingest_cases import (
    case_document_to_index_doc,
    ensure_case_index,
    ingest_case_documents,
    load_documents_from_jsonl,
)


def _doc(case_id: str = "case-001") -> CaseDocument:
    return CaseDocument(
        case_id=case_id,
        title="张某诉某公司劳动争议案",
        case_number=f"({case_id[:4]})京01民终123号",
        court="北京市第一中级人民法院",
        case_type="劳动争议",
        effective_level="reference",
        brief_facts="劳动者主张违法解除。",
        ruling_summary="用人单位举证不能,支付赔偿金。",
        judgment_date=date(2022, 6, 30),
        legal_elements=["劳动关系", "解除事实", "工资基数"],
        legal_issues=["违法解除赔偿金"],
        claims=["支付赔偿金"],
        defenses=["严重违纪"],
        reasoning_spans=[SpanRef(source_type="case", source_id=case_id, locator="本院认为#2")],
        cited_statutes=[
            CitedStatuteRef(title="中华人民共和国劳动合同法", article_number="第八十七条")
        ],
    )


class _FakeIndices:
    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.created: list[tuple[str, dict | None]] = []
        self.deleted: list[str] = []
        self.refreshed: list[str] = []

    def exists(self, index: str) -> bool:
        return index in self.existing

    def create(self, index: str, body: dict | None = None) -> None:
        self.created.append((index, body))
        self.existing.add(index)

    def delete(self, index: str) -> None:
        self.deleted.append(index)
        self.existing.discard(index)

    def refresh(self, index: str) -> None:
        self.refreshed.append(index)


class _FakeClient:
    def __init__(self) -> None:
        self.indices = _FakeIndices()


def test_case_document_to_index_doc_contract_fields():
    """转换必须包含 OpenSearchCaseSource 的读取契约字段。"""
    doc = _doc()
    indexed = case_document_to_index_doc(doc)
    # 数据源核心契约
    assert indexed["case_id"] == "case-001"
    assert indexed["title"] == "张某诉某公司劳动争议案"
    assert indexed["case_type"] == "劳动争议"
    assert indexed["date"] == "2022-06-30"
    assert indexed["summary"] == doc.ruling_summary
    # full_text 由标题+事实+理由拼接
    assert "违法解除" in indexed["full_text"]
    # 要素化字段平铺
    assert indexed["legal_elements"] == ["劳动关系", "解除事实", "工资基数"]
    assert indexed["cited_statute_titles"] == ["中华人民共和国劳动合同法"]
    # dict 入参亦可(duck-typing)
    assert case_document_to_index_doc(json.loads(doc.model_dump_json()))["case_id"] == "case-001"


def test_ensure_case_index_idempotent_and_force():
    client = _FakeClient()
    assert ensure_case_index(client, "legal_cases") is True
    assert client.indices.created and client.indices.created[0][0] == "legal_cases"
    # mapping 随创建下发
    body = client.indices.created[0][1]
    assert "title" in body["mappings"]["properties"]

    # 已存在时不重建
    client2 = _FakeClient()
    client2.indices.existing.add("legal_cases")
    ensure_case_index(client2, "legal_cases")
    assert client2.indices.created == []

    # force 删重建
    ensure_case_index(client2, "legal_cases", force=True)
    assert client2.indices.deleted == ["legal_cases"]
    assert len(client2.indices.created) == 1


def test_ingest_bulk_actions_and_stats(monkeypatch):
    client = _FakeClient()
    captured: list[list[dict]] = []

    def _fake_bulk(_client, actions, raise_on_error=False, stats_only=False, chunk_size=500):
        captured.append(list(actions))
        return (len(captured[-1]), 0)

    import opensearchpy.helpers as _helpers

    monkeypatch.setattr(_helpers, "bulk", _fake_bulk)
    docs = [_doc("case-001"), _doc("case-002")]
    succeeded, failed = ingest_case_documents(docs, client=client)
    assert (succeeded, failed) == (2, 0)
    assert client.indices.refreshed == ["legal_cases"]
    actions = captured[0]
    assert len(actions) == 2
    assert actions[0]["_id"] == "case-001"
    assert actions[0]["_op_type"] == "index"
    # 索引文档含数据源契约字段
    assert actions[0]["_source"]["title"].startswith("张某诉")


def test_ingest_empty_and_unreachable_degrade():
    assert ingest_case_documents([], client=_FakeClient()) == (0, 0)
    # 未提供 client 且 OpenSearch 未配置/不可达 → (0, 0) 不抛异常
    succeeded, failed = ingest_case_documents([_doc()], client=None)
    assert (succeeded, failed) == (0, 0)


def test_jsonl_loader_skips_corrupt_lines(tmp_path):
    path = tmp_path / "cases.jsonl"
    good = _doc("case-good").model_dump_json()
    path.write_text(f"{good}\n{{not json}}\n\n  \n", encoding="utf-8")
    docs = load_documents_from_jsonl(path)
    assert len(docs) == 1
    assert docs[0]["case_id"] == "case-good"


def test_cli_main_happy_path(tmp_path, monkeypatch):
    input_path = tmp_path / "cases.jsonl"
    input_path.write_text(_doc("case-cli").model_dump_json(), encoding="utf-8")

    client = _FakeClient()
    monkeypatch.setattr(ingest_cases, "_default_client", lambda: client)
    captured: list[list[dict]] = []

    def _fake_bulk(_client, actions, raise_on_error=False, stats_only=False, chunk_size=500):
        captured.append(list(actions))
        return (len(captured[-1]), 0)

    import opensearchpy.helpers as _helpers

    monkeypatch.setattr(_helpers, "bulk", _fake_bulk)

    ns = ingest_cases._build_parser().parse_args(["--input", str(input_path)])
    rc = ingest_cases.main(ns)
    assert rc == 0
    assert captured and captured[0][0]["_id"] == "case-cli"


def test_cli_main_missing_input(tmp_path):
    ns = ingest_cases._build_parser().parse_args(["--input", str(tmp_path / "nope.jsonl")])
    assert ingest_cases.main(ns) == 2
