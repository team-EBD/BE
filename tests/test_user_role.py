"""계정 역할(users.role: user/tester/admin) — AI 한도 면제와 '서버에서만 지정' 원칙.

tester·admin 은 AI 사용 한도(무료 10회 게이트·일일 한도)를 받지 않는다. 구독 상태는 그대로다.
역할은 어떤 API 로도 바뀌지 않고 응답에도 실리지 않는다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models import AI_LIMIT_EXEMPT_ROLES, User, UserRole
from tests.test_ai_premium_gate import _post, seed_ai_logs  # noqa: F401 (fixture 재사용)

AI_PATHS = [
    "/v1/meals/analyze",
    "/v1/meals/parse-text",
    "/v1/recommendations/next-meal",
    "/v1/recommendations/menu",
    "/v1/recommendations/location-based-menu",
]


def _call(client, headers, path: str):
    """위치 기반 추천은 위치 동의가 없으면 한도 확인 전에 403 — 한도 동작만 보려고 동의를 먼저 넣는다."""
    if path.endswith("location-based-menu"):
        consent = client.post(
            "/v1/users/location-consent",
            headers=headers,
            json={"consent_status": True, "consent_version": "1.0"},
        )
        assert consent.status_code == 201
    return _post(client, headers, path)


@pytest.fixture
def set_role(db_factory, auth_headers):
    def _set(role: str) -> None:
        with db_factory() as db:
            db.execute(update(User).values(role=role))
            db.commit()

    return _set


@pytest.fixture(autouse=True)
def _legacy_recommend_engine(monkeypatch):
    """무료 크레딧 게이트는 AI 를 실제로 부르는 legacy 추천에만 걸린다 — 기본 엔진(v2)은 AI·한도를 쓰지 않는다."""
    monkeypatch.setattr(settings, "recommend_engine", "legacy")


def test_new_accounts_default_to_user(client, auth_headers, db_factory):
    with db_factory() as db:
        assert db.scalar(select(User.role)) == UserRole.USER


def test_exempt_roles_are_tester_and_admin_only():
    assert AI_LIMIT_EXEMPT_ROLES == {UserRole.TESTER, UserRole.ADMIN}
    assert UserRole.USER not in AI_LIMIT_EXEMPT_ROLES


@pytest.mark.parametrize("path", AI_PATHS)
def test_user_is_blocked_after_free_credits(client, auth_headers, seed_ai_logs, monkeypatch, path):
    """기준선 — 일반 사용자는 게이트가 켜지면 10회 뒤 429."""
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    seed_ai_logs("analyze", 10)
    res = _call(client, auth_headers, path)
    assert res.status_code == 429, res.text
    assert res.json()["error"]["details"][0]["reason"] == "free_credit_exhausted"


@pytest.mark.parametrize("role", [UserRole.TESTER, UserRole.ADMIN])
@pytest.mark.parametrize("path", AI_PATHS)
def test_exempt_roles_pass_the_gate_on_every_ai_path(
    client, auth_headers, seed_ai_logs, set_role, monkeypatch, role, path
):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    seed_ai_logs("analyze", 25)
    set_role(role)
    res = _call(client, auth_headers, path)
    assert res.status_code != 429, res.text


def test_exempt_role_also_skips_daily_limit_when_gate_is_off(
    client, auth_headers, seed_ai_logs, set_role, monkeypatch
):
    monkeypatch.setattr(settings, "ai_premium_gate", False)
    monkeypatch.setattr(settings, "analyze_daily_limit", 3)
    seed_ai_logs("analyze", 3)
    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code == 429
    set_role(UserRole.TESTER)
    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code != 429


def test_usage_api_reports_unlimited_for_tester_but_keeps_is_premium_truthful(
    client, auth_headers, seed_ai_logs, set_role, monkeypatch
):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    seed_ai_logs("analyze", 12)

    before = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert before["free_credits"] == {"limit": 10, "used": 12, "remaining": 0}

    set_role(UserRole.TESTER)
    after = client.get("/v1/usage/daily", headers=auth_headers).json()
    # 앱의 '무료 크레딧 소진' 배지는 remaining 이 null 이면 뜨지 않는다
    assert after["free_credits"] == {"limit": None, "used": 12, "remaining": None}
    assert after["analyze"]["limit"] is None and after["recommend"]["limit"] is None
    # 구독자가 아니다 — 구독 유도·결제 화면은 일반 사용자처럼 시험할 수 있어야 한다
    assert after["is_premium"] is False


def test_role_cannot_be_set_through_the_api(client, auth_headers, db_factory):
    """프로필 수정에 role 을 끼워 넣어도 바뀌지 않는다."""
    client.patch("/v1/users/me", headers=auth_headers, json={"nickname": "새이름", "role": "admin"})
    client.put("/v1/users/me", headers=auth_headers, json={"role": "tester"})
    with db_factory() as db:
        assert db.scalar(select(User.role)) == UserRole.USER


def test_role_is_not_exposed_in_responses(client, auth_headers, set_role):
    set_role(UserRole.ADMIN)
    me = client.get("/v1/users/me", headers=auth_headers).json()
    assert "role" not in me
    login = client.post("/v1/auth/social/login", json={"provider": "google", "token": "roleprobe"}).json()
    assert "role" not in login["user"]


def test_unknown_role_is_rejected_by_the_database(db_factory, auth_headers):
    with db_factory() as db, pytest.raises(IntegrityError):
        db.execute(update(User).values(role="superuser"))
        db.commit()
