"""P0-9 修复：MIME 一致性校验必须精确匹配，而非只匹配前缀。

回归覆盖：
  .doc 文件携带 application/pdf Content-Type 应被拒绝（同属 application/*
  但 MIME 完全不同）。.pdf 携带 application/msword 也应被拒绝。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """构造带内存后端的 app，关闭认证便于测试。"""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("PERSISTENCE_REQUIRED", "false")
    monkeypatch.setenv("CASE_VAULT_ALLOW_INSECURE", "true")
    from lvyan.api.server import create_app

    app = create_app()
    return TestClient(app)


def test_doc_rejects_pdf_content_type(client):
    """.doc 扩展名 + application/pdf Content-Type 必须被拒绝（415）。"""
    resp = client.post(
        "/api/upload",
        files={"file": ("test.doc", io.BytesIO(b"\xd0\xcf\x11\xe0"), "application/pdf")},
        data={"query": "测试"},
    )
    assert resp.status_code == 415, (
        f".doc + application/pdf 应被拒绝（MIME 不一致），实际 {resp.status_code}: {resp.text}"
    )


def test_pdf_rejects_msword_content_type(client):
    """.pdf 扩展名 + application/msword Content-Type 必须被拒绝（415）。"""
    resp = client.post(
        "/api/upload",
        files={
            "file": (
                "test.pdf",
                io.BytesIO(b"%PDF-1.4\n"),
                "application/msword",
            )
        },
        data={"query": "测试"},
    )
    assert resp.status_code == 415, (
        f".pdf + application/msword 应被拒绝，实际 {resp.status_code}: {resp.text}"
    )


def test_pdf_accepts_correct_content_type(client):
    """正例：.pdf + application/pdf 应通过 MIME 校验。"""
    resp = client.post(
        "/api/upload",
        files={
            "file": (
                "ok.pdf",
                io.BytesIO(b"%PDF-1.4\n"),
                "application/pdf",
            )
        },
        data={"query": "测试"},
    )
    # 不应是 415（其他错误可接受，例如下游转换失败）
    assert resp.status_code != 415, f".pdf + application/pdf 不应被 MIME 校验拒绝：{resp.text}"
