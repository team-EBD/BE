"""Sign in with Apple — identityToken 검증(단위) + 소셜 로그인 연동(API)."""
from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app.core.config import settings
from app.core.errors import APIError
from app.social_client import SocialIdentity, apple, verify_social_token

KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    public_jwk = jwk.construct(public_pem, algorithm="RS256").to_dict()
    public_jwk["kid"] = KID
    return private_pem, {"keys": [public_jwk]}


@pytest.fixture(autouse=True)
def stub_jwks(rsa_keys, monkeypatch):
    """Apple JWKS 네트워크 조회를 테스트 키로 대체한다."""
    _, jwks = rsa_keys
    apple.reset_jwks_cache()
    monkeypatch.setattr(apple, "_fetch_jwks", lambda: jwks)
    yield
    apple.reset_jwks_cache()


def _make_token(private_pem: str, *, kid: str = KID, **overrides) -> str:
    claims = {
        "iss": apple.APPLE_ISSUER,
        "sub": "apple-user-001",
        "aud": "com.eatlog.app",
        "email": "user@privaterelay.appleid.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def test_valid_token_returns_claims(rsa_keys):
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    claims = apple.verify_identity_token(_make_token(private_pem))
    assert claims["sub"] == "apple-user-001"
    assert claims["email"] == "user@privaterelay.appleid.com"


def test_wrong_audience_rejected(rsa_keys):
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    with pytest.raises(APIError) as exc:
        apple.verify_identity_token(_make_token(private_pem, aud="com.other.app"))
    assert exc.value.status_code == 401


def test_expired_token_rejected(rsa_keys):
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    with pytest.raises(APIError) as exc:
        apple.verify_identity_token(
            _make_token(private_pem, exp=int(time.time()) - 60)
        )
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected(rsa_keys):
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    with pytest.raises(APIError) as exc:
        apple.verify_identity_token(
            _make_token(private_pem, iss="https://evil.example.com")
        )
    assert exc.value.status_code == 401


def test_unknown_kid_rejected(rsa_keys):
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    with pytest.raises(APIError) as exc:
        apple.verify_identity_token(_make_token(private_pem, kid="unknown-kid"))
    assert exc.value.status_code == 401


def test_verify_social_token_apple_mapping(rsa_keys):
    """apple 분기: sub→social_id, 이메일 앞부분→닉네임 폴백."""
    private_pem, _ = rsa_keys
    settings.apple_bundle_id = "com.eatlog.app"
    identity = verify_social_token("apple", _make_token(private_pem))
    assert identity == SocialIdentity(
        provider="apple",
        social_id="apple-user-001",
        email="user@privaterelay.appleid.com",
        nickname="user",
        profile_image_url=None,
    )


def test_unsupported_provider_still_400():
    with pytest.raises(APIError) as exc:
        verify_social_token("kakao", "token")
    assert exc.value.status_code == 400


# --- API: 최초 인증 시 이름 전달(body.name) → 신규 가입 닉네임 ---

def test_social_login_uses_body_name_for_new_user(client):
    res = client.post(
        "/v1/auth/social/login",
        json={"provider": "google", "token": "apple-like-user", "name": "사과유저"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["user"]["nickname"] == "사과유저"

    # 재로그인(기존 유저)에서는 name 을 보내도 닉네임이 바뀌지 않는다
    res = client.post(
        "/v1/auth/social/login",
        json={"provider": "google", "token": "apple-like-user", "name": "다른이름"},
    )
    assert res.status_code == 200
    assert res.json()["user"]["nickname"] == "사과유저"
