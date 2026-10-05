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


@pytest.fixture
def credit_limit_10(monkeypatch):
    """아래 시나리오들은 '10회 소진'을 기준으로 쓰였다 — 한도를 설정으로 10에 고정한다."""
    monkeypatch.setattr(settings, "free_credit_limit", 10)


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


def test_gate_off_keeps_separate_daily_limits(
    client, auth_headers, seed_ai_logs, monkeypatch, credit_limit_10
):
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


def test_gate_on_combines_calls_across_days(
    client, auth_headers, seed_ai_logs, monkeypatch, credit_limit_10
):
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
def test_gate_on_blocks_every_ai_endpoint(
    client, auth_headers, seed_ai_logs, monkeypatch, credit_limit_10, path
):
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


# --- 무료 사용권 한도는 설정값 (출시 기념 10 → 100) ---


def test_free_credit_limit_defaults_to_100():
    from app.core.config import Settings

    assert Settings.model_fields["free_credit_limit"].default == 100


def test_legacy_constant_name_follows_the_setting(monkeypatch):
    """FREE_CREDIT_LIMIT 이름은 남아 있고, 읽을 때마다 설정값을 돌려준다."""
    from app.services import usage_limit

    monkeypatch.setattr(settings, "free_credit_limit", 100)
    assert usage_limit.FREE_CREDIT_LIMIT == 100
    assert usage_limit.free_credit_limit() == 100
    monkeypatch.setattr(settings, "free_credit_limit", 7)
    assert usage_limit.FREE_CREDIT_LIMIT == 7
    from app.services.usage_limit import FREE_CREDIT_LIMIT  # 기존 import 형태

    assert FREE_CREDIT_LIMIT == 7


@pytest.mark.parametrize("used", (10, 50, 99))
def test_gate_on_default_limit_does_not_block_below_100(
    client, auth_headers, seed_ai_logs, monkeypatch, used
):
    """예전 한도(10)를 넘긴 사용자도 100회 전까지는 막히지 않는다."""
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "free_credit_limit", 100)
    seed_ai_logs("analyze", used - 5, days_ago=3)
    seed_ai_logs("recommend", 5)

    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 100, "used": used, "remaining": 100 - used}
    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code == 200
    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 100, "used": used + 1, "remaining": 99 - used}


def test_gate_on_default_limit_blocks_at_100(client, auth_headers, seed_ai_logs, monkeypatch):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "free_credit_limit", 100)
    seed_ai_logs("analyze", 60, days_ago=3)
    seed_ai_logs("recommend", 39)

    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code == 200  # 100번째
    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 100, "used": 100, "remaining": 0}

    blocked = _post(client, auth_headers, "/v1/meals/parse-text")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == {
        "code": "TOO_MANY_REQUESTS",
        "message": "무료 AI 사용권 100회를 모두 사용했어요. 프리미엄으로 업그레이드하면 무제한으로 이용할 수 있어요.",
        "details": [
            {
                "field": "analyze",
                "reason": "free_credit_exhausted",
                "limit": 100,
                "used": 100,
                "upgradable": True,
            }
        ],
    }


def test_free_credit_limit_follows_the_setting(client, auth_headers, seed_ai_logs, monkeypatch):
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "free_credit_limit", 3)
    seed_ai_logs("analyze", 3)

    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 3, "used": 3, "remaining": 0}
    blocked = _post(client, auth_headers, "/v1/recommendations/next-meal")
    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["message"].startswith("무료 AI 사용권 3회를 모두 사용했어요.")
    assert error["details"][0]["limit"] == 3

    # 한도를 올리면 코드 변경 없이 바로 풀린다
    monkeypatch.setattr(settings, "free_credit_limit", 4)
    assert _post(client, auth_headers, "/v1/recommendations/next-meal").status_code == 200


def test_gate_off_free_credits_are_display_only(client, auth_headers, seed_ai_logs, monkeypatch):
    """게이트가 꺼져 있으면 무료 사용권 한도는 막지 않는다 — /usage/daily 표시에만 쓰인다."""
    monkeypatch.setattr(settings, "ai_premium_gate", False)
    monkeypatch.setattr(settings, "free_credit_limit", 100)
    monkeypatch.setattr(settings, "analyze_daily_limit", 10)
    monkeypatch.setattr(settings, "recommend_daily_limit", 10)
    seed_ai_logs("analyze", 150, days_ago=3)  # 평생 150회 — 무료 사용권은 이미 넘겼다

    usage = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert usage["free_credits"] == {"limit": 100, "used": 150, "remaining": 0}
    assert usage["analyze"] == {"limit": 10, "used": 0, "remaining": 10}
    # 그래도 막히지 않는다 — 적용되는 것은 일일 한도뿐
    assert _post(client, auth_headers, "/v1/meals/parse-text").status_code == 200

    seed_ai_logs("analyze", 9)
    blocked = _post(client, auth_headers, "/v1/meals/parse-text")
    assert blocked.status_code == 429
    details = blocked.json()["error"]["details"][0]
    assert details["reason"] == "daily_limit_exceeded"
    assert details["limit"] == 10
