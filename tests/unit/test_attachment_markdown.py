"""M5：附件 Markdown 读取与路径穿越测试。

覆盖：
- 新格式（markdown_path 指向 .md 文件）正常读取；
- 旧格式（JSON 内嵌 markdown 字段）向后兼容读取；
- 无 markdown_path / markdown 时回退 text_preview；
- markdown_path 指向 ``_UPLOAD_DIR`` 之外的路径 → 404；
- markdown_path 指向不存在的 .md 文件 → 404；
- 不同用户的附件在 AUTH_ENABLED=true 下访问被拒（403）；
- 上传端点写入后，磁盘上存在 <file_id>.<ext> / <file_id>.md / <file_id>.json
  且 JSON 不含 markdown 字段（含 markdown_path）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException


def _make_meta(
    *,
    file_id: str = "f1",
    markdown_path: str | None = None,
    markdown: str | None = None,
    text_preview: str = "",
    user_id: str = "anonymous",
    filename: str = "contract.md",
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "file_id": file_id,
        "filename": filename,
        "user_id": user_id,
        "text_preview": text_preview,
    }
    if markdown_path is not None:
        meta["markdown_path"] = markdown_path
    if markdown is not None:
        meta["markdown"] = markdown
    return meta


# ---------------------------------------------------------------------------
# 1. _resolve_upload_path
# ---------------------------------------------------------------------------
def test_resolve_upload_path_accepts_inside(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    resolved = server._resolve_upload_path(str(upload_dir / "abc.md"))
    assert resolved == (upload_dir / "abc.md").resolve()


def test_resolve_upload_path_rejects_traversal(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    # 逃逸路径：构造一个存在于 upload_dir 之外的绝对路径
    outside = (tmp_path / "secret.txt").resolve()
    with pytest.raises(HTTPException) as exc:
        server._resolve_upload_path(str(outside))
    assert exc.value.status_code == 404


def test_resolve_upload_path_rejects_dotdot(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    with pytest.raises(HTTPException) as exc:
        server._resolve_upload_path(str(upload_dir / ".." / "etc" / "passwd"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 2. _load_attachment_markdown 优先级
# ---------------------------------------------------------------------------
def test_load_markdown_prefers_markdown_path(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    md_file = upload_dir / "f1.md"
    md_file.write_text("# 新格式\n正文内容", encoding="utf-8")

    meta = _make_meta(
        markdown_path=str(md_file),
        markdown="旧的全文内容（应该被忽略）",
        text_preview="preview",
    )
    result = server._load_attachment_markdown(meta, "f1")
    assert "新格式" in result
    assert "旧的全文内容" not in result


def test_load_markdown_falls_back_to_legacy_field(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    # 直接放旧字段（不通过 markdown_path，模拟 M5 之前的 JSON 结构）
    meta = {"markdown": "旧格式全文", "text_preview": "preview", "user_id": "anonymous"}
    result = server._load_attachment_markdown(meta, "f1")
    assert result == "旧格式全文"


def test_load_markdown_falls_back_to_preview(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    meta = _make_meta(text_preview="只是预览")
    result = server._load_attachment_markdown(meta, "f1")
    assert result == "只是预览"


def test_load_markdown_markdown_path_missing_returns_404(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    # 声明了 markdown_path 但文件不存在
    meta = _make_meta(
        markdown_path=str(upload_dir / "ghost.md"),
        markdown=None,
        text_preview="preview",
    )
    with pytest.raises(HTTPException) as exc:
        server._load_attachment_markdown(meta, "f1")
    assert exc.value.status_code == 404


def test_load_markdown_traversal_returns_404(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    from lvyan.api import server

    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    outside = (tmp_path / "evil.md").resolve()
    outside.write_text("secret", encoding="utf-8")
    meta = _make_meta(
        markdown_path=str(outside),
        markdown=None,
        text_preview="preview",
    )
    with pytest.raises(HTTPException) as exc:
        server._load_attachment_markdown(meta, "f1")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 3. 端到端：上传 → 三件套落盘；JSON 不含 markdown 全文
# ---------------------------------------------------------------------------
def _build_app(monkeypatch, tmp_path):
    """构造一个生产配置的 app，但把 _UPLOAD_DIR 重定向到 tmp_path。"""
    from lvyan.api import server
    from lvyan.api.server import create_app

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    # 关闭认证，避免 401 干扰
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    # 注入一个不依赖 PG / 图运行时的 runner。
    async def runner(*_args):
        return "ok"

    return create_app(runner=runner, memory=None, metadata_store=None), upload_dir


def test_upload_writes_three_files_and_json_has_no_markdown(monkeypatch, tmp_path):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    app, upload_dir = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            "/api/upload",
            files={"file": ("note.txt", "hello world\n第二行".encode("utf-8"), "text/plain")},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    file_id = data["file_id"]

    # 三件套
    raw = upload_dir / f"{file_id}.txt"
    md = upload_dir / f"{file_id}.md"
    js = upload_dir / f"{file_id}.json"
    assert raw.is_file()
    assert md.is_file()
    assert js.is_file()

    meta = json.loads(js.read_text(encoding="utf-8"))
    # M5：JSON 不再存 markdown 全文
    assert "markdown" not in meta
    # 但保留了 markdown_path
    assert "markdown_path" in meta
    assert Path(meta["markdown_path"]).name == f"{file_id}.md"
    # .md 文件内容应包含转换结果（直接读取的文本文件）
    md_text = md.read_text(encoding="utf-8")
    assert "hello world" in md_text


# ---------------------------------------------------------------------------
# 4. 不同用户附件访问测试（AUTH_ENABLED=true）
# ---------------------------------------------------------------------------
def test_cross_user_attachment_blocked_in_run(monkeypatch, tmp_path):
    """AUTH_ENABLED=true 下，user A 上传的附件被 user B 引用应返回 403。"""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api import server
    from lvyan.api.server import create_app

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(server, "_UPLOAD_DIR", upload_dir)
    monkeypatch.setenv("AUTH_ENABLED", "true")

    async def runner(*_args):
        return "不应执行"

    app = create_app(runner=runner, memory=None, metadata_store=None)
    with TestClient(app) as client:
        # user A 上传
        resp = client.post(
            "/api/upload",
            files={"file": ("a.txt", b"alice secret", "text/plain")},
            headers={"X-User-ID": "alice"},
        )
        assert resp.status_code == 200, resp.text
        file_id = resp.json()["file_id"]

        # user B 引用 → run 端点在启动 agent 前拒绝。
        meta_path = upload_dir / f"{file_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["user_id"] == "alice"
        response = client.post(
            "/api/agent/run",
            json={"query": "请分析附件", "attachments": [file_id]},
            headers={"X-User-ID": "bob"},
        )
    assert response.status_code == 404


def test_attachment_file_id_path_traversal_is_rejected(monkeypatch, tmp_path):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    app, _ = _build_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/agent/run",
            json={"query": "请分析附件", "attachments": ["../secret"]},
        )
    assert response.status_code == 422


def test_upload_failure_cleans_created_files(monkeypatch, tmp_path):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from lvyan.api import server

    app, upload_dir = _build_app(monkeypatch, tmp_path)

    def fail_conversion(*_args, **_kwargs):
        raise RuntimeError("converter unavailable")

    monkeypatch.setattr(server, "convert_to_markdown", fail_conversion)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
    assert response.status_code == 500
    assert list(upload_dir.iterdir()) == []
