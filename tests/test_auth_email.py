"""이메일 가입/로그인 전 과정 (FE 계약 — /auth/signup, /auth/login)."""
from __future__ import annotations

from sqlalchemy import select

from app.models import User, UserProfile
from tests.conftest import login as social_login

SIGNUP_BODY = {
    "email": "alice@example.com",
    "password": "password1",
    "nickname": "앨리스",
    "height": 165.5,
    "weight": 55.0,
    "gender": "female",
}


def signup(client, **overrides) -> dict:
    body = {**SIGNUP_BODY, **overrides}
    res = client.post("/v1/auth/signup", json=body)
    return res


def test_signup_201_tokens_and_profile(client, db_factory):
    res = signup(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["nickname"] == "앨리스"

    # 발급된 access 토큰으로 보호 API 접근 가능
    me = client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"
    # 기본 목표 칼로리가 프로필에 저장되어 /me 에 노출된다
    assert me.json()["daily_goal_calories"] == 2000

    # user_profiles 에 성별/키/몸무게 + 탄단지 목표(50:30:20)가 저장된다
    with db_factory() as db:
        user = db.scalar(
            select(User).where(
                User.social_provider == "email", User.social_id == "alice@example.com"
            )
        )
        assert user is not None
        assert user.password_hash and user.password_hash.startswith("pbkdf2_sha256$")
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
        assert profile.gender == "female"
        assert float(profile.height) == 165.5
        assert float(profile.weight) == 55.0
        assert (profile.goal_carbs, profile.goal_protein, profile.goal_fat) == (250, 150, 44)


def test_signup_duplicate_email_409(client):
    assert signup(client).status_code == 201
    res = signup(client, nickname="다른닉네임")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CONFLICT"
    assert res.json()["error"]["message"] == "이미 가입된 이메일입니다."


def test_signup_password_without_digit_400(client):
    res = signup(client, password="passwordonly")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_success_200(client):
    signup(client)
    res = client.post(
        "/v1/auth/login",
        json={"email": "alice@example.com", "password": "password1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == "alice@example.com"

    me = client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200


def test_login_wrong_password_401(client):
    signup(client)
    res = client.post(
        "/v1/auth/login",
        json={"email": "alice@example.com", "password": "wrongpass1"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
    assert res.json()["error"]["message"] == "이메일 또는 비밀번호가 올바르지 않습니다."


def test_login_social_only_account_401(client):
    # 소셜 전용 계정(password_hash NULL)은 이메일 로그인 불가 — 동일한 401
    social_login(client, "bob")  # email: bob@example.com
    res = client.post(
        "/v1/auth/login",
        json={"email": "bob@example.com", "password": "password1"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["message"] == "이메일 또는 비밀번호가 올바르지 않습니다."


def test_signup_refresh_token_works(client):
    tokens = signup(client).json()
    res = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert res.status_code == 200
    new_tokens = res.json()
    assert new_tokens["access_token"] and new_tokens["refresh_token"]

    me = client.get(
        "/v1/users/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert me.status_code == 200


def test_duplicate_nickname_allowed_with_distinct_tags(client):
    """같은 닉네임으로 여러 명 가입 가능 — 태그(#0001~#9999)로 구분된다."""
    first = signup(client)
    second = signup(client, email="bob@example.com")
    assert first.status_code == 201 and second.status_code == 201

    tag1 = first.json()["user"]["nickname_tag"]
    tag2 = second.json()["user"]["nickname_tag"]
    assert len(tag1) == 4 and tag1.isdigit() and tag1 != "0000"
    assert tag1 != tag2  # 동일 닉네임이면 태그는 반드시 달라야 한다


def test_nickname_change_keeps_tag_when_free(client):
    """닉네임 변경 시 새 닉네임에서 기존 태그가 비어 있으면 유지한다."""
    res = signup(client)
    token = res.json()["access_token"]
    tag = res.json()["user"]["nickname_tag"]

    me = client.patch(
        "/v1/users/me",
        json={"nickname": "새닉네임"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200
    assert me.json()["nickname"] == "새닉네임"
    assert me.json()["nickname_tag"] == tag
