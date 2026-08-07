"""P0-4 回归测试：法律语料库就绪策略。

验证：
1. config.is_official_law_db_required() 按 REQUIRE_OFFICIAL_LAW_DB 与生产模式解析；
2. server._check_retrieval() 在「要求完整库 + 库缺失」时返回 degraded；
3. server._legal_corpus_status() 返回 mode/documents/chunks 等字段。
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def cfg(monkeypatch):
    """重新加载 config 模块，确保环境变量生效。"""
    import lvyan.config as cfg_mod

    importlib.reload(cfg_mod)
    yield cfg_mod
    importlib.reload(cfg_mod)  # 恢复


def test_required_explicit_true(monkeypatch, cfg):
    """显式 REQUIRE_OFFICIAL_LAW_DB=true → 要求完整库。"""
    monkeypatch.setenv("REQUIRE_OFFICIAL_LAW_DB", "true")
    monkeypatch.delenv("RUNTIME_MODE", raising=False)
    assert cfg.is_official_law_db_required() is True


def test_required_explicit_false(monkeypatch, cfg):
    """显式 REQUIRE_OFFICIAL_LAW_DB=false → 即使生产模式也不要求。"""
    monkeypatch.setenv("REQUIRE_OFFICIAL_LAW_DB", "false")
    monkeypatch.setenv("RUNTIME_MODE", "production")
    assert cfg.is_official_law_db_required() is False


def test_required_defaults_to_production(monkeypatch, cfg):
    """未显式设置时：生产模式要求，非生产不要求。"""
    monkeypatch.delenv("REQUIRE_OFFICIAL_LAW_DB", raising=False)
    monkeypatch.setenv("RUNTIME_MODE", "production")
    assert cfg.is_official_law_db_required() is True

    monkeypatch.setenv("RUNTIME_MODE", "development")
    assert cfg.is_official_law_db_required() is False


def test_check_retrieval_degraded_when_required_and_missing(monkeypatch):
    """要求完整库 + 官方库不可用 → _check_retrieval 返回 degraded。"""
    import lvyan.api.server as server_mod

    monkeypatch.setattr(server_mod, "is_official_db_available", lambda: False)
    monkeypatch.setattr(server_mod, "is_official_law_db_required", lambda: True)
    # degraded 分支在读取 settings.knowledge_dir 之前就 return，无需 patch settings
    assert server_mod._check_retrieval() == "degraded"


def test_check_retrieval_ok_when_official_available(monkeypatch):
    """官方库可用 → ok（无论是否 required）。"""
    import lvyan.api.server as server_mod

    monkeypatch.setattr(server_mod, "is_official_db_available", lambda: True)
    assert server_mod._check_retrieval() == "ok"


def test_check_retrieval_ok_when_not_required_and_curated_exists(monkeypatch, tmp_path):
    """未要求完整库 + 官方库缺失 + 精编库存在 → ok（降级可用）。"""
    import lvyan.api.server as server_mod

    monkeypatch.setattr(server_mod, "is_official_db_available", lambda: False)
    monkeypatch.setattr(server_mod, "is_official_law_db_required", lambda: False)

    class _FakeSettings:
        knowledge_dir = tmp_path  # 存在

    monkeypatch.setattr(server_mod, "settings", _FakeSettings())
    assert server_mod._check_retrieval() == "ok"


def test_legal_corpus_status_structure(monkeypatch):
    """_legal_corpus_status 返回必要字段。"""
    import lvyan.api.server as server_mod

    monkeypatch.setattr(server_mod, "is_official_db_available", lambda: True)
    monkeypatch.setattr(server_mod, "is_official_law_db_required", lambda: True)
    info = server_mod._legal_corpus_status()
    assert info["mode"] == "official_full"
    assert info["available"] is True
    assert info["required"] is True
    assert "lawtext_dir" in info
    # documents 在库可用时为非 None 整数（或 None 当目录扫描异常）
    assert info["documents"] is None or isinstance(info["documents"], int)


def test_legal_corpus_status_curated_only(monkeypatch):
    """官方库不可用 → mode=curated_only。"""
    import lvyan.api.server as server_mod

    monkeypatch.setattr(server_mod, "is_official_db_available", lambda: False)
    info = server_mod._legal_corpus_status()
    assert info["mode"] == "curated_only"
    assert info["available"] is False
