from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lvyan.api.routes_preferences import create_preferences_router
from lvyan.graph.builder import NODE_NAMES, build_graph
from lvyan.llm.prompt_registry import prompt_versions
from lvyan.mcp_server import _registry
from lvyan.memory.case_vault import CaseVault
from lvyan.memory.user_preferences import UserPreferences
from lvyan.nodes.attachment_retriever import _load_markdown
from lvyan.nodes.evidence_analyzer import _llm_refine_evidence
from lvyan.nodes.triage import jurisdiction_triage
from lvyan.retrieval.version_quality import assess_version_quality
from lvyan.retrieval.version_resolver import LawMetadata, parse_law_metadata
from lvyan.schemas.evidence import EvidenceRequirement


def test_evidence_analyzer_is_in_main_graph():
    assert "evidence_analyzer" in NODE_NAMES
    graph = build_graph().get_graph()
    assert "evidence_analyzer" in graph.nodes
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("authority_resolver", "evidence_analyzer") in edges
    assert ("evidence_analyzer", "legal_reasoner") in edges


def test_all_llm_enhanced_nodes_have_versioned_prompts():
    versions = prompt_versions()
    assert {
        "jurisdiction_triage",
        "missing_fact_assessor",
        "evidence_analyzer",
        "authority_resolver",
        "critic",
    } <= versions.keys()
    assert all(version for version in versions.values())


def test_run_context_carries_only_supplied_preferences():
    from lvyan.api.run_context import RunContext

    ctx = RunContext(
        "run-1",
        "thread-1",
        user_preferences={"response_style": "brief", "preferred_doc_format": "docx"},
    )
    assert ctx.user_preferences == {
        "response_style": "brief",
        "preferred_doc_format": "docx",
    }


def test_detailed_preference_changes_presentation_not_state_complexity():
    from lvyan.nodes.composer import composer

    state = {
        "user_goal": "违约后怎么办",
        "complexity": "light",
        "user_preferences": {"response_style": "detailed"},
    }
    output = composer(state)["final_output"]
    assert "案件深度分析报告" in output
    assert state["complexity"] == "light"


def test_triage_accepts_only_valid_llm_schema(monkeypatch):
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda **_: {
            "jurisdiction": "中国大陆",
            "case_type": "工伤认定",
            "complexity": "deep",
            "risk_level": "medium",
        },
    )
    result = jurisdiction_triage({"user_goal": "单位不批准我的职业病待遇", "missing_facts": []})
    assert result["case_type"] == "工伤认定"
    assert result["complexity"] == "deep"


def test_evidence_llm_cannot_inject_new_requirement(monkeypatch):
    requirement = EvidenceRequirement(
        requirement_id="req-1",
        fact_to_prove="劳动关系",
        evidence_types=["劳动合同"],
        current_status="missing",
        gap_description="未提供",
    )
    monkeypatch.setattr("lvyan.llm.llm_available", lambda: True)
    monkeypatch.setattr(
        "lvyan.llm.chat_json",
        lambda **_: {
            "items": [
                {"requirement_id": "forged", "current_status": "met"},
                {
                    "requirement_id": "req-1",
                    "current_status": "partial",
                    "gap_description": "仅有工资流水",
                },
            ]
        },
    )
    result = _llm_refine_evidence([requirement], [])
    assert len(result) == 1
    assert result[0].requirement_id == "req-1"
    assert result[0].current_status == "partial"


def test_preferences_api_persists_only_whitelisted_fields(tmp_path):
    app = FastAPI()
    store = UserPreferences(base_dir=tmp_path / "prefs")
    app.include_router(create_preferences_router(store))
    client = TestClient(app)

    response = client.patch(
        "/api/preferences",
        json={
            "response_style": "detailed",
            "preferred_doc_format": "docx",
            "online_search_enabled": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["response_style"] == "detailed"
    assert response.json()["online_search_enabled"] is True
    assert client.get("/api/preferences").json()["preferred_doc_format"] == "docx"
    assert client.delete("/api/preferences").json() == {"deleted": True}


def test_case_vault_uri_is_read_by_attachment_node(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_VAULT_ALLOW_INSECURE", "true")
    vault = CaseVault(base_dir=tmp_path / "vault")
    vault.store("thread-1", "doc-1", "证据正文".encode(), {})
    monkeypatch.setattr("lvyan.memory.case_vault.CaseVault", lambda: vault)
    # expected_thread_id 与 vault 引用一致 → 正常读取
    assert _load_markdown("vault://thread-1/doc-1", expected_thread_id="thread-1") == "证据正文"
    # 与 vault 引用不一致（跨 thread / 空）→ 返回空串，不读取他人材料
    assert _load_markdown("vault://thread-1/doc-1", expected_thread_id="thread-2") == ""
    assert _load_markdown("vault://thread-1/doc-1") == ""


def test_mcp_registry_exposes_bounded_legal_tools():
    tools = _registry()
    assert {
        "search_statutes",
        "get_statute_article",
        "verify_statute_status",
        "search_procedure_rules",
        "extract_document",
        "analyze_contract_clause",
        "calculate_legal_deadline",
        "render_docx",
        "search_official_web",
    } <= tools.keys()


def test_metadata_override_and_quality_gate(tmp_path, monkeypatch):
    override = tmp_path / "overrides.yaml"
    override.write_text(
        "overrides:\n  law-1:\n    status: repealed\n    expiry_date: 2024-01-01\n",
        encoding="utf-8",
    )
    law = tmp_path / "law-1.md"
    law.write_text(
        "---\nid: law-1\ntitle: 测试法\nstatus: 有效\neffective_date: 2020-01-01\n"
        "urls: [https://example.gov.cn/law-1]\n---\n第一条 测试。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LAW_METADATA_OVERRIDES", str(override))
    from lvyan.retrieval import version_resolver

    version_resolver._metadata_overrides.cache_clear()
    try:
        metadata = parse_law_metadata(law)
    finally:
        version_resolver._metadata_overrides.cache_clear()
    assert metadata.status == "repealed"
    assert metadata.expiry_date == date(2024, 1, 1)

    report = assess_version_quality([metadata])
    assert report.ready is True


def test_quality_gate_rejects_unknown_status():
    metadata = LawMetadata(
        source_id="unknown-1",
        title="未知法规",
        status="unknown",
        raw_filepath="unknown.md",
        content_hash="abc",
        official_urls=["https://example.gov.cn/unknown"],
    )
    report = assess_version_quality([metadata])
    assert report.ready is False
    assert report.unknown_status == 1
