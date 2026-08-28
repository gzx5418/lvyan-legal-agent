"""验证 case_vault 加密降级阻断逻辑。

退出条件验证：
1. 生产环境 + 无 CASE_VAULT_KEY → 启动失败
2. 开发环境 + CASE_VAULT_ALLOW_INSECURE=false + 无 key → 启动失败
3. 开发环境 + CASE_VAULT_ALLOW_INSECURE=true + 无 key → base64 降级
4. 有合法 key → AES-256-GCM 加解密正常
"""

from __future__ import annotations

import secrets

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """确保每个测试从干净的环境变量状态开始。"""
    monkeypatch.delenv("CASE_VAULT_KEY", raising=False)
    monkeypatch.delenv("CASE_VAULT_ALLOW_INSECURE", raising=False)
    monkeypatch.delenv("RUNTIME_MODE", raising=False)


def test_production_no_key_startup_fails(monkeypatch):
    """生产环境 + 无 CASE_VAULT_KEY → validate_encryption_config 抛异常。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")

    from lvyan.memory.case_vault import CaseVault

    with pytest.raises(RuntimeError, match="生产环境必须配置.*CASE_VAULT_KEY"):
        CaseVault.validate_encryption_config()


def test_production_invalid_key_startup_fails(monkeypatch):
    """生产环境 + 不合法 key (长度不对) → 启动失败。"""
    monkeypatch.setenv("RUNTIME_MODE", "production")
    monkeypatch.setenv("CASE_VAULT_KEY", "aabbccdd")  # 仅4字节

    from lvyan.memory.case_vault import CaseVault

    with pytest.raises(RuntimeError, match="生产环境必须配置.*CASE_VAULT_KEY"):
        CaseVault.validate_encryption_config()


def test_development_no_key_no_insecure_fails(monkeypatch):
    """开发环境 + 无 key + CASE_VAULT_ALLOW_INSECURE 未设置 → 失败。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    # 「未设置」需要同时清环境变量与 settings 默认值
    # （conftest 为整个测试套件启用了 base64 降级）
    monkeypatch.delenv("CASE_VAULT_ALLOW_INSECURE", raising=False)
    from lvyan.config import settings

    monkeypatch.setattr(settings, "case_vault_allow_insecure", False)

    from lvyan.memory.case_vault import CaseVault

    with pytest.raises(RuntimeError, match="CASE_VAULT_ALLOW_INSECURE=true"):
        CaseVault.validate_encryption_config()


def test_development_insecure_allowed_passes(monkeypatch):
    """开发环境 + CASE_VAULT_ALLOW_INSECURE=true → 允许降级（不抛异常）。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("CASE_VAULT_ALLOW_INSECURE", "true")

    from lvyan.memory.case_vault import CaseVault

    # 不应抛异常
    CaseVault.validate_encryption_config()


def test_valid_key_passes(monkeypatch):
    """有合法 32 字节密钥 → 通过校验。"""
    key = secrets.token_hex(32)  # 64 hex 字符 = 32 字节
    monkeypatch.setenv("CASE_VAULT_KEY", key)
    monkeypatch.setenv("RUNTIME_MODE", "production")

    from lvyan.memory.case_vault import CaseVault

    CaseVault.validate_encryption_config()


def test_aes_encrypt_decrypt_roundtrip(monkeypatch):
    """有合法 key 时，加密后可正确解密。"""
    key = secrets.token_hex(32)
    monkeypatch.setenv("CASE_VAULT_KEY", key)

    from lvyan.memory.case_vault import CaseVault

    plaintext = "测试敏感法律材料：合同原文".encode("utf-8")
    ciphertext = CaseVault._encrypt(plaintext)

    # 密文应以 AES header 开头
    assert ciphertext.startswith(CaseVault._AES_HEADER)

    # 解密后应与原文一致
    decrypted = CaseVault._decrypt(ciphertext)
    assert decrypted == plaintext


def test_encrypt_fails_without_key_in_strict_mode(monkeypatch):
    """无 key + 不允许不安全模式 → _encrypt 抛异常。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    # 「不允许」需要同时清环境变量与 settings 默认值（conftest 全局启用了降级）
    monkeypatch.delenv("CASE_VAULT_ALLOW_INSECURE", raising=False)
    from lvyan.config import settings

    monkeypatch.setattr(settings, "case_vault_allow_insecure", False)

    from lvyan.memory.case_vault import CaseVault

    with pytest.raises(RuntimeError, match="加密操作失败"):
        CaseVault._encrypt(b"secret data")


def test_encrypt_base64_fallback_insecure_mode(monkeypatch):
    """开发环境 + CASE_VAULT_ALLOW_INSECURE=true → base64 降级。"""
    monkeypatch.setenv("RUNTIME_MODE", "development")
    monkeypatch.setenv("CASE_VAULT_ALLOW_INSECURE", "true")

    from lvyan.memory.case_vault import CaseVault
    import base64

    plaintext = b"test data"
    result = CaseVault._encrypt(plaintext)

    # 应该是 base64 编码（不是 AES 头）
    assert not result.startswith(CaseVault._AES_HEADER)
    assert base64.b64decode(result) == plaintext
