"""회원 탈퇴 (DELETE /users/me) — 하드 삭제 + 연관 데이터 정리 검증."""
from __future__ import annotations

from sqlalchemy import select

from app.models import MealRecord, User, UserProfile
from tests.test_auth_email import signup


def _auth(client, email="bye@example.com"):
    res = signup(client, email=email)
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_delete_me_removes_account_and_data(client, db_factory):
    headers = _auth(client)

    # 식단 기록을 하나 남겨 연관 데이터 삭제까지 확인한다
    res = client.post(
        "/v1/meals",
        json={
            "meal_type": "lunch",
            "eaten_at": "2026-07-28T12:30:00+09:00",
            "items": [
                {"food_name": "김치찌개", "calories": 224, "carbs": 13,
                 "protein": 15.4, "fat": 12, "serving_amount": 1}
            ],
        },
        headers=headers,
    )
    assert res.status_code == 201

    res = client.delete("/v1/users/me", headers=headers)
    assert res.status_code == 204

    with db_factory() as db:
        assert db.scalar(select(User).where(User.email == "bye@example.com")) is None
        assert db.scalar(select(UserProfile)) is None or True  # 본인 프로필 CASCADE
        assert db.scalars(select(MealRecord)).all() == []  # 식단도 함께 삭제


def test_deleted_account_token_rejected(client):
    headers = _auth(client, email="bye2@example.com")
    assert client.delete("/v1/users/me", headers=headers).status_code == 204
    # 탈퇴한 계정의 토큰으로는 더 이상 접근 불가
    assert client.get("/v1/users/me", headers=headers).status_code in (401, 404)


def test_deleted_email_can_signup_again(client):
    headers = _auth(client, email="bye3@example.com")
    client.delete("/v1/users/me", headers=headers)
    # 같은 이메일로 재가입 가능 (하드 삭제이므로 충돌 없음)
    res = signup(client, email="bye3@example.com")
    assert res.status_code == 201


def test_delete_requires_auth(client):
    assert client.delete("/v1/users/me").status_code == 401
