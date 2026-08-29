"""Phase 2 DoD — 소셜 로그인/토큰 갱신 전 과정."""
from __future__ import annotations

import time

from jose import jwt

from app.core.config import settings
from tests.conftest import login


def test_social_login_creates_user_201(client):
    res = client.post(
        "/v1/auth/social/login", json={"provider": "google", "token": "alice"}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["social_provider"] == "google"
    assert body["user"]["email"] == "alice@example.com"


def test_social_login_existing_user_200(client):
    client.post("/v1/auth/social/login", json={"provider": "google", "token": "bob"})
    res = client.post(
        "/v1/auth/social/login", json={"provider": "google", "token": "bob"}
    )
    assert res.status_code == 200


def test_unsupported_provider_400(client):
    res = client.post(
        "/v1/auth/social/login", json={"provider": "naver", "token": "x"}
    )
    # fake verifier 는 provider 를 그대로 통과시키므로 실제 검증 로직을 직접 확인
    from app.core.errors import APIError
    from app.social_client import verify_social_token

    try:
        verify_social_token("naver", "x")
        assert False, "should raise"
    except APIError as e:
        assert e.status_code == 400
        assert e.code == "VALIDATION_ERROR"


def test_protected_api_without_token_401(client):
    res = client.get("/v1/users/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_expired_access_token_401_token_expired(client):
    now = int(time.time())
    expired = jwt.encode(
        {"sub": "1", "type": "access", "iat": now - 100, "exp": now - 10},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    res = client.get("/v1/users/me", headers={"Authorization": f"Bearer {expired}"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_refresh_rotates_tokens(client):
    tokens = login(client, "carol")
    res = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 200
    new_tokens = res.json()
    assert new_tokens["access_token"] and new_tokens["refresh_token"]

    # 회전된(철회된) 기존 refresh 재사용 → 401
    res2 = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert res2.status_code == 401
    assert res2.json()["error"]["code"] == "UNAUTHORIZED"

    # 새 access 토큰으로 보호 API 접근 가능
    res3 = client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
    )
    assert res3.status_code == 200


def test_refresh_with_garbage_401(client):
    res = client.post("/v1/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert res.status_code == 401
