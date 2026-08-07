"""案件工作空间 API 的真实流程与租户隔离测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lvyan.api.server import create_app
from lvyan.memory.case_workspace import InMemoryCaseWorkspaceStore


async def _unused_runner(*_args, **_kwargs):
    return "unused"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "trusted_proxy")
    app = create_app(
        runner=_unused_runner,
        memory=object(),
        workspace_store=InMemoryCaseWorkspaceStore(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _create_case(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/cases",
        headers=headers,
        json={"title": "上海房屋买卖纠纷", "description": "定金返还与违约责任"},
    )
    assert response.status_code == 201
    return response.json()


def test_case_workspace_hides_cross_tenant_metadata(client: TestClient):
    alice = {"X-User-ID": "alice"}
    bob = {"X-User-ID": "bob"}
    case = _create_case(client, alice)

    response = client.get(f"/api/cases/{case['case_id']}", headers=bob)
    assert response.status_code == 404
    assert response.json()["detail"] == "资源不存在"

    response = client.get("/api/cases", headers=bob)
    assert response.status_code == 200
    assert response.json() == []


def test_document_review_and_approval_lifecycle(client: TestClient):
    headers = {"X-User-ID": "alice"}
    case = _create_case(client, headers)

    response = client.post(
        f"/api/cases/{case['case_id']}/documents",
        headers=headers,
        json={
            "title": "民事起诉状",
            "document_type": "complaint",
            "content": "请求判令被告返还定金。",
        },
    )
    assert response.status_code == 201
    created = response.json()
    document = created["document"]
    version = created["version"]
    assert document["status"] == "draft"
    assert version["version_number"] == 1

    response = client.post(
        f"/api/legal-documents/{document['document_id']}/findings",
        headers=headers,
        json={
            "version_id": version["version_id"],
            "severity": "high",
            "title": "缺少管辖依据",
            "description": "未说明被告住所地或合同履行地。",
        },
    )
    assert response.status_code == 201
    finding = response.json()

    response = client.post(
        f"/api/legal-documents/{document['document_id']}/approvals",
        headers=headers,
        json={"version_id": version["version_id"], "decision": "approved"},
    )
    assert response.status_code == 409

    response = client.patch(
        f"/api/review-findings/{finding['finding_id']}",
        headers=headers,
        json={"status": "resolved"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"

    response = client.post(
        f"/api/legal-documents/{document['document_id']}/approvals",
        headers=headers,
        json={"version_id": version["version_id"], "decision": "approved"},
    )
    assert response.status_code == 201
    assert response.json()["decision"] == "approved"

    response = client.get(f"/api/legal-documents/{document['document_id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    response = client.get(f"/api/cases/{case['case_id']}/audit-events", headers=headers)
    assert response.status_code == 200
    actions = {event["action"] for event in response.json()}
    assert {"case.created", "document.created", "review.finding_created", "document.approved"} <= actions


def test_new_version_returns_document_to_draft(client: TestClient):
    headers = {"X-User-ID": "alice"}
    case = _create_case(client, headers)
    created = client.post(
        f"/api/cases/{case['case_id']}/documents",
        headers=headers,
        json={"title": "律师函", "document_type": "letter", "content": "初稿"},
    ).json()
    document = created["document"]
    first_version = created["version"]

    approval = client.post(
        f"/api/legal-documents/{document['document_id']}/approvals",
        headers=headers,
        json={"version_id": first_version["version_id"], "decision": "approved"},
    )
    assert approval.status_code == 201

    response = client.post(
        f"/api/legal-documents/{document['document_id']}/versions",
        headers=headers,
        json={"content": "第二稿", "change_summary": "补充付款期限"},
    )
    assert response.status_code == 201
    assert response.json()["version_number"] == 2

    response = client.get(f"/api/legal-documents/{document['document_id']}", headers=headers)
    assert response.json()["status"] == "draft"

