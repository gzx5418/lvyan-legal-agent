"""M3：JWT 认证测试。

覆盖：
- AUTH_ENABLED=false 保持 anonymous；
- AUTH_ENABLED=true + X-User-ID 网关注入；
- AUTH_ENABLED=true + Bearer JWT 但未开启进程内验签 → 401（不信任未验签 JWT）；
- 伪造签名 / 过期 / issuer 不匹配 / audience 不匹配 / 缺少 sub 的 JWT 均失败；
- 合法签名且字段匹配的 JWT 可正常提取 sub。

为避免依赖外部 JWKS endpoint，本测试在本地生成 RS256 密钥对 + 模拟 JWKS，
并通过 monkeypatch 把 ``PyJWKClient`` 替换为返回本地公钥的桩。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 辅助：构造本地 RS256 密钥对、签发 JWT、模拟 JWKS
# ---------------------------------------------------------------------------
def _ensure_pyjwt_and_crypto():
    """确保 PyJWT 与 cryptography 可用，否则跳过本组测试。"""
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")


def _generate_rsa_key():
    """生成测试用 RS256 私钥（含公钥）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_kid_from_pub_pem(public_pem: bytes) -> tuple[str, dict[str, str]]:
    """从 PEM 公钥构造 JWK + kid。"""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    pub = serialization.load_pem_public_key(public_pem, backend=default_backend())
    numbers = pub.public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    kid = "test-kid-001"
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url(n),
        "e": _b64url(e),
    }
    return kid, jwk


def _make_rs256_jwt(
    private_pem: bytes,
    *,
    claims: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> str:
    """用本地私钥签发 RS256 JWT。"""
    import jwt  # type: ignore[import-untyped]

    return jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers=headers or {},
    )


def _make_unsigned_jwt(claims: dict[str, Any]) -> str:
    """手工构造 alg=none 的无签名 JWT（攻击载荷）。"""
    header = {"alg": "none", "typ": "JWT"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{h}.{p}."


def _make_tampered_jwt(
    private_pem: bytes,
    public_pem_other: bytes,
    claims: dict[str, Any],
) -> str:
    """用「另一个」密钥签名，但 header 声称 RS256 → 验签必失败。"""
    import jwt  # type: ignore[import-untyped]

    return jwt.encode(claims, public_pem_other, algorithm="HS256")


def _patch_jwks(monkeypatch, public_pem: bytes, kid: str, jwk: dict[str, str]):
    """把 auth._fetch_signing_key 替换为返回本地公钥的桩。"""

    def _fake_fetch_signing_key(token: str, jwks_url: str, algorithms: list[str]):
        # 直接返回 PEM 公钥；PyJWT RS256 验签可直接接受 PEM 字符串
        return public_pem

    monkeypatch.setattr(
        "lvyan.api.auth._fetch_signing_key",
        _fake_fetch_signing_key,
    )


class _Req:
    """模拟 FastAPI Request。"""


# ---------------------------------------------------------------------------
# 1. AUTH_ENABLED=false → anonymous
# ---------------------------------------------------------------------------
def test_auth_disabled_keeps_anonymous(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    from lvyan.api.auth import ANONYMOUS_USER, get_current_user_id

    uid = get_current_user_id(_Req(), x_user_id=None, authorization=None)
    assert uid == ANONYMOUS_USER


# ---------------------------------------------------------------------------
# 2. AUTH_ENABLED=true + X-User-ID 网关注入
# ---------------------------------------------------------------------------
def test_auth_enabled_with_x_user_id(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from lvyan.api.auth import get_current_user_id

    uid = get_current_user_id(_Req(), x_user_id="user-from-gateway", authorization=None)
    assert uid == "user-from-gateway"


def test_auth_enabled_no_credentials_returns_401(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=None)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 3. 未开启进程内验签 → 任何 Bearer JWT 都返回 401（不信任未验签 payload）
# ---------------------------------------------------------------------------
def test_bearer_jwt_rejected_when_verify_disabled(monkeypatch):
    """JWT_VERIFY_IN_PROCESS=false 时，Bearer JWT 必须被拒绝，不能解码 payload 信任 sub。"""
    _ensure_pyjwt_and_crypto()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("JWT_VERIFY_IN_PROCESS", raising=False)
    # 同时确保 settings 默认为 false
    from lvyan.config import settings

    monkeypatch.setattr(settings, "jwt_verify_in_process", False)

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    # 构造一个 sub=attacker 的「漂亮」JWT；未验签路径若被启用就会读到 attacker
    fake_token = _make_unsigned_jwt({"sub": "attacker", "exp": int(time.time()) + 3600})
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {fake_token}")
    assert exc.value.status_code == 401
    assert "attacker" not in (exc.value.detail or "")


# ---------------------------------------------------------------------------
# 4. 伪造签名（alg=none / 错误密钥）不能通过
# ---------------------------------------------------------------------------
def test_forged_alg_none_jwt_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    fake_token = _make_unsigned_jwt({"sub": "attacker"})
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {fake_token}")
    assert exc.value.status_code in (401, 500)


# ---------------------------------------------------------------------------
# 5. 过期 JWT → 401
# ---------------------------------------------------------------------------
def test_expired_jwt_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "test-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "test-aud")

    expired = _make_rs256_jwt(
        private_pem,
        claims={
            "sub": "user-expired",
            "exp": int(time.time()) - 3600,
            "nbf": int(time.time()) - 7200,
            "iss": "test-iss",
            "aud": "test-aud",
        },
        headers={"kid": kid},
    )

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {expired}")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 6. issuer 不匹配 → 401
# ---------------------------------------------------------------------------
def test_wrong_issuer_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "expected-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "test-aud")

    token = _make_rs256_jwt(
        private_pem,
        claims={
            "sub": "user-wrong-iss",
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) - 10,
            "iss": "wrong-iss",
            "aud": "test-aud",
        },
        headers={"kid": kid},
    )

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 7. audience 不匹配 → 401
# ---------------------------------------------------------------------------
def test_wrong_audience_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "test-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "expected-aud")

    token = _make_rs256_jwt(
        private_pem,
        claims={
            "sub": "user-wrong-aud",
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) - 10,
            "iss": "test-iss",
            "aud": "wrong-aud",
        },
        headers={"kid": kid},
    )

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 8. 缺少 sub → 401
# ---------------------------------------------------------------------------
def test_jwt_missing_sub_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "test-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "test-aud")

    token = _make_rs256_jwt(
        private_pem,
        claims={
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) - 10,
        },
        headers={"kid": kid},
    )

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 9. 时效与绑定 claim 均为必填，不能仅在「存在时」校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing_claim", ["exp", "nbf", "iss", "aud"])
def test_jwt_missing_required_claim_rejected(monkeypatch, missing_claim):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "test-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "test-aud")

    claims: dict[str, Any] = {
        "sub": "user-missing-claim",
        "exp": int(time.time()) + 3600,
        "nbf": int(time.time()) - 10,
        "iss": "test-iss",
        "aud": "test-aud",
    }
    claims.pop(missing_claim)
    token = _make_rs256_jwt(private_pem, claims=claims, headers={"kid": kid})

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {token}")
    assert exc.value.status_code == 401


def test_jwt_verify_requires_issuer_and_audience_configuration(monkeypatch):
    _ensure_pyjwt_and_crypto()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    from lvyan.config import settings

    monkeypatch.setattr(settings, "jwt_issuer", "")
    monkeypatch.setattr(settings, "jwt_audience", "")

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(_Req(), x_user_id=None, authorization="Bearer x.y.z")
    assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# 9. 合法 JWT → 正常提取 sub
# ---------------------------------------------------------------------------
def test_valid_jwt_extracts_sub(monkeypatch):
    _ensure_pyjwt_and_crypto()
    private_pem, public_pem = _generate_rsa_key()
    kid, jwk = _jwk_kid_from_pub_pem(public_pem)
    _patch_jwks(monkeypatch, public_pem, kid, jwk)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ISSUER", "test-iss")
    monkeypatch.setenv("JWT_AUDIENCE", "test-aud")

    token = _make_rs256_jwt(
        private_pem,
        claims={
            "sub": "user-valid-123",
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) - 10,
            "iss": "test-iss",
            "aud": "test-aud",
        },
        headers={"kid": kid},
    )

    from lvyan.api.auth import get_current_user_id

    uid = get_current_user_id(_Req(), x_user_id=None, authorization=f"Bearer {token}")
    assert uid == "user-valid-123"


# ---------------------------------------------------------------------------
# 10. JWT_ALGORITHMS 禁止 none
# ---------------------------------------------------------------------------
def test_jwt_algorithms_none_rejected(monkeypatch):
    _ensure_pyjwt_and_crypto()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.setenv("JWT_JWKS_URL", "https://fake.example/.well-known/jwks.json")
    monkeypatch.setenv("JWT_ALGORITHMS", "none")

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(
            _Req(),
            x_user_id=None,
            authorization="Bearer x.y.z",
        )
    assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# 11. JWT_VERIFY_IN_PROCESS=true 但 JWKS 未配置 → 500
# ---------------------------------------------------------------------------
def test_jwks_missing_returns_500(monkeypatch):
    _ensure_pyjwt_and_crypto()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_VERIFY_IN_PROCESS", "true")
    monkeypatch.delenv("JWT_JWKS_URL", raising=False)
    from lvyan.config import settings

    monkeypatch.setattr(settings, "jwt_jwks_url", "")

    from fastapi import HTTPException

    from lvyan.api.auth import get_current_user_id

    with pytest.raises(HTTPException) as exc:
        get_current_user_id(
            _Req(),
            x_user_id=None,
            authorization="Bearer x.y.z",
        )
    assert exc.value.status_code == 500
