"""Phase 9·10 DoD — 추천 3종 + 실패 5xx + 위치 동의 가드."""
from __future__ import annotations

from app.ai_client import get_ai_client
from app.ai_client.base import failed_recommend
from app.main import app
from tests.test_meals import create_meal


class FailingAIClient:
    def __init__(self, reason: str = "ai_timeout") -> None:
        self.reason = reason

    def analyze(self, image_url, eating_habits=None):
        raise AssertionError("not used")

    def recommend(self, daily_summary, preferred_category, meal_timing):
        return failed_recommend(self.reason)


def test_next_meal_success(client, auth_headers):
    create_meal(client, auth_headers)
    res = client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27", "preferred_category": "convenience_store"},
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["recommendations"]) == 3
    assert body["caution_text"]
    assert body["ai_call_log_id"] > 0
    first = body["recommendations"][0]
    assert {"name", "category", "estimated_calories", "reason"} <= set(first)


def test_next_meal_timeout_504(client, auth_headers):
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("ai_timeout")
    res = client.post(
        "/v1/recommendations/next-meal", headers=auth_headers, json={"date": "2026-06-27"}
    )
    assert res.status_code == 504
    assert res.json()["error"]["code"] == "AI_TIMEOUT"


def test_next_meal_provider_error_502(client, auth_headers):
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("provider_error")
    res = client.post(
        "/v1/recommendations/next-meal", headers=auth_headers, json={"date": "2026-06-27"}
    )
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "AI_PROVIDER_ERROR"


def test_menu_exceed_flag(client, auth_headers):
    res = client.post(
        "/v1/recommendations/menu",
        headers=auth_headers,
        json={"meal_type": "lunch", "remaining_calories": 400},
    )
    assert res.status_code == 200
    body = res.json()
    # mock 추천: 320/450/380 kcal → 400 초과분(450)은 alternative 로
    recommended_cals = [m["estimated_calories"] for m in body["recommended_menus"]]
    alternative_cals = [m["estimated_calories"] for m in body["alternative_menus"]]
    assert all(c <= 400 for c in recommended_cals)
    assert all(c > 400 for c in alternative_cals)
    assert all(m["exceed_flag"] for m in body["alternative_menus"])


def test_location_menu_requires_consent_403(client, auth_headers):
    res = client.post(
        "/v1/recommendations/location-based-menu",
        headers=auth_headers,
        json={"latitude": 37.5665, "longitude": 126.9780},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"


def test_location_menu_with_consent(client, auth_headers):
    client.post(
        "/v1/users/location-consent",
        headers=auth_headers,
        json={"consent_status": True, "consent_version": "1.0"},
    )
    res = client.post(
        "/v1/recommendations/location-based-menu",
        headers=auth_headers,
        json={"latitude": 37.5665, "longitude": 126.9780, "category": "convenience_store"},
    )
    assert res.status_code == 200
    assert len(res.json()["nearby_recommendations"]) == 3


def test_location_menu_after_revoke_403(client, auth_headers):
    client.post(
        "/v1/users/location-consent",
        headers=auth_headers,
        json={"consent_status": True, "consent_version": "1.0"},
    )
    client.patch(
        "/v1/users/location-consent", headers=auth_headers, json={"consent_status": False}
    )
    res = client.post(
        "/v1/recommendations/location-based-menu",
        headers=auth_headers,
        json={"latitude": 37.5665, "longitude": 126.9780},
    )
    assert res.status_code == 403


def test_recommendation_logged(client, auth_headers):
    client.post(
        "/v1/recommendations/next-meal", headers=auth_headers, json={"date": "2026-06-27"}
    )
    res = client.get(
        "/v1/ai-call-logs", headers=auth_headers, params={"task_type": "recommend"}
    )
    assert res.json()["pagination"]["total"] == 1
