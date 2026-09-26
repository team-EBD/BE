"""AI 프리미엄 게이트의 off/on 정책과 다섯 호출 경로."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.timeutil import now_utc
from app.models import AiCallLog, User
from app.models.billing import Subscription
from tests.test_images import upload


@pytest.fixture
def seed_ai_logs(db_factory, auth_headers):
    def seed(task_type: str, count: int, *, days_ago: int = 0, status: str = "success") -> None:
        with db_factory() as db:
            user_id = db.scalar(select(User.id))
            for _ in range(count):
                db.add(
                    AiCallLog(
                        user_id=user_id,
                        task_type=task_type,
                        provider="mock",
                        model_name="mock",
                        status=status,
                        latency_ms=0,
                        created_at=now_utc() - timedelta(days=days_ago),
                    )
                )
            db.commit()

    return seed


@pytest.fixture(autouse=True)
def _legacy_recommend_engine(monkeypatch):
    """AI 게이트는 AI 를 실제로 부르는 legacy 추천에만 걸린다 — 기본 엔진(v2)은 AI·한도를 쓰지 않는다."""
    monkeypatch.setattr(settings, "recommend_engine", "legacy")


def _post(client, headers, path: str):
    payloads = {
        "/v1/meals/parse-text": {"text": "김밥 한 줄"},
        "/v1/recommendations/next-meal": {"date": "2026-06-27"},
        "/v1/recommendations/menu": {"meal_type": "lunch"},
        "/v1/recommendations/location-based-menu": {
            "latitude": 37.5665,
            "longitude": 126.9780,
        },
    }
    if path == "/v1/meals/analyze":
        image_id = upload(client, headers).json()["meal_image_id"]
        payload = {"meal_image_id": image_id}
    else:
        payload = payloads[path]
    return client.post(path, headers=headers, json=payload)


def test_gate_off_keeps_separate_daily_limits(client, auth_headers, seed_ai_logs, monkeypatch):
    monkeypatch.setattr(settings, "ai_premium_gate", False)
    seed_ai_logs("analyze", 1, days_ago=2)
    seed_ai_logs("recommend", 1, days_ago=2)
    seed_ai_logs("analyze", 9)
    seed_ai_logs("recommend", 9)

    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["analyze"] == {"limit": 10, "used": 9, "remaining": 1}
    assert usage["recommend"] == {"limit": 10, "used": 9, "remaining": 1}
    assert usage["free_credits"] == {"limit": 10, "used": 20, "remaining": 0}

    for path in ("/v1/meals/parse-text", "/v1/recommendations/next-meal"):
        assert _post(client, auth_headers, path).status_code == 200
        blocked = _post(client, auth_headers, path)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["details"][0]["reason"] == "daily_limit_exceeded"


def test_gate_on_combines_calls_across_days(client, auth_headers, seed_ai_logs, monkeypatch):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "analyze_daily_limit", 1)
    monkeypatch.setattr(settings, "recommend_daily_limit", 1)
    seed_ai_logs("analyze", 4, days_ago=2)
    seed_ai_logs("recommend", 4, days_ago=2)
    seed_ai_logs("analyze", 1, status="error")

    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code == 200
    assert _post(client, auth_headers, "/v1/recommendations/menu").status_code == 200
    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 10, "used": 10, "remaining": 0}
    assert usage["analyze"] == {"limit": None, "used": 1, "remaining": None}
    assert usage["recommend"] == {"limit": None, "used": 1, "remaining": None}
    assert usage["is_premium"] is False

    blocked = _post(client, auth_headers, "/v1/recommendations/next-meal")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == {
        "code": "TOO_MANY_REQUESTS",
        "message": "무료 AI 사용권 10회를 모두 사용했어요. 프리미엄으로 업그레이드하면 무제한으로 이용할 수 있어요.",
        "details": [
            {
                "field": "recommend",
                "reason": "free_credit_exhausted",
                "limit": 10,
                "used": 10,
                "upgradable": True,
            }
        ],
    }


@pytest.mark.parametrize(
    "path",
    (
        "/v1/meals/analyze",
        "/v1/meals/parse-text",
        "/v1/recommendations/next-meal",
        "/v1/recommendations/menu",
        "/v1/recommendations/location-based-menu",
    ),
)
def test_gate_on_blocks_every_ai_endpoint(client, auth_headers, seed_ai_logs, monkeypatch, path):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    seed_ai_logs("analyze", 5)
    seed_ai_logs("recommend", 5)
    if path.endswith("location-based-menu"):
        consent = client.post(
            "/v1/users/location-consent",
            headers=auth_headers,
            json={"consent_status": True, "consent_version": "1.0"},
        )
        assert consent.status_code == 201

    blocked = _post(client, auth_headers, path)
    assert blocked.status_code == 429
    details = blocked.json()["error"]["details"][0]
    assert details["reason"] == "free_credit_exhausted"
    assert details["upgradable"] is True


def test_gate_on_premium_is_unlimited(client, auth_headers, seed_ai_logs, db_factory, monkeypatch):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "analyze_daily_limit_premium", 1)
    monkeypatch.setattr(settings, "recommend_daily_limit_premium", 1)
    seed_ai_logs("analyze", 10)
    with db_factory() as db:
        user_id = db.scalar(select(User.id))
        db.add(
            Subscription(
                user_id=user_id,
                platform="android",
                product_id="eatlog_premium_monthly",
                purchase_key="premium-gate-test",
                status="active",
                is_auto_renewing=True,
                verified_at=now_utc(),
                expires_at=now_utc() + timedelta(days=30),
            )
        )
        db.commit()

    for path in (
        "/v1/meals/parse-text",
        "/v1/meals/parse-text",
        "/v1/recommendations/next-meal",
        "/v1/recommendations/next-meal",
    ):
        assert _post(client, auth_headers, path).status_code == 200
    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["is_premium"] is True
    assert usage["analyze"]["limit"] is None
    assert usage["recommend"]["limit"] is None
