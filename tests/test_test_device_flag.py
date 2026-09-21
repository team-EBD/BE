"""테스트 기기(로봇) 표시 — 로그인·가입 요청의 is_test_device → users.is_test_device.

분석 대시보드가 이 값이 True 인 계정을 지표에서 제외한다 (app/services/test_device.py).
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import User
from app.services.test_device import record_test_device
from tests.test_auth_email import SIGNUP_BODY


def _flag(db_factory, social_id_or_email: str) -> bool | None:
    with db_factory() as db:
        user = db.scalar(
            select(User).where((User.email == social_id_or_email) | (User.social_id == social_id_or_email))
        )
        assert user is not None
        return user.is_test_device


def _social(client, token: str, **extra):
    return client.post("/v1/auth/social/login", json={"provider": "google", "token": token, **extra})


def test_social_signup_from_test_device_is_flagged(client, db_factory):
    assert _social(client, "robot", is_test_device=True).status_code == 201
    assert _flag(db_factory, "robot@example.com") is True


def test_social_signup_from_normal_device_is_false(client, db_factory):
    assert _social(client, "human", is_test_device=False).status_code == 201
    assert _flag(db_factory, "human@example.com") is False


def test_old_app_without_field_stays_unknown(client, db_factory):
    """필드를 모르는 구버전 앱 — 요청이 그대로 통과하고 값은 NULL(모름)로 남는다."""
    assert _social(client, "legacy").status_code == 201
    assert _flag(db_factory, "legacy@example.com") is None


def test_existing_account_gets_flagged_on_later_login(client, db_factory):
    _social(client, "late")
    assert _flag(db_factory, "late@example.com") is None
    assert _social(client, "late", is_test_device=True).status_code == 200
    assert _flag(db_factory, "late@example.com") is True


def test_flag_is_sticky_once_true(client, db_factory):
    _social(client, "sticky", is_test_device=True)
    _social(client, "sticky", is_test_device=False)  # 같은 계정이 일반 기기에서 로그인
    _social(client, "sticky")  # 구버전 앱에서 로그인
    assert _flag(db_factory, "sticky@example.com") is True


def test_unknown_then_false_is_recorded(client, db_factory):
    _social(client, "upgrade")  # 구버전 앱으로 가입
    _social(client, "upgrade", is_test_device=False)  # 새 앱으로 업데이트 후 로그인
    assert _flag(db_factory, "upgrade@example.com") is False


def test_email_signup_and_login_carry_flag(client, db_factory):
    res = client.post("/v1/auth/signup", json={**SIGNUP_BODY, "is_test_device": False})
    assert res.status_code == 201, res.text
    assert _flag(db_factory, SIGNUP_BODY["email"]) is False

    res = client.post(
        "/v1/auth/login",
        json={"email": SIGNUP_BODY["email"], "password": SIGNUP_BODY["password"], "is_test_device": True},
    )
    assert res.status_code == 200, res.text
    assert _flag(db_factory, SIGNUP_BODY["email"]) is True


def test_flag_not_exposed_in_responses(client):
    """분석용 내부 값 — 클라이언트 응답에 실리지 않는다."""
    body = _social(client, "hidden", is_test_device=True).json()
    assert "is_test_device" not in body["user"]
    me = client.get("/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert "is_test_device" not in me.json()


def test_invalid_flag_type_is_rejected(client):
    res = _social(client, "bad", is_test_device="definitely")
    assert res.status_code in (400, 422)


def test_record_rule_directly():
    user = User(social_provider="google", social_id="x", nickname="n")
    record_test_device(user, None)
    assert user.is_test_device is None
    record_test_device(user, False)
    assert user.is_test_device is False
    record_test_device(user, True)
    assert user.is_test_device is True
    record_test_device(user, False)
    assert user.is_test_device is True
