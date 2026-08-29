"""Phase 9·10 DoD — 추천 3종 + 실패 5xx + 위치 동의 가드."""
from __future__ import annotations

from app.ai_client import get_ai_client
from app.ai_client.base import failed_recommend
from app.ai_client.mock import MockAIClient
from app.main import app
from tests.test_meals import create_meal


class FailingAIClient:
    def __init__(self, reason: str = "ai_timeout") -> None:
        self.reason = reason

    def analyze(self, image_url, eating_habits=None):
        raise AssertionError("not used")

    def recommend(
        self, daily_summary, preferred_category, meal_timing,
        user_history_context=None, current_time=None,
    ):
        return failed_recommend(self.reason)


class RecordingAIClient(MockAIClient):
    """recommend 로 전달된 user_history_context/current_time 을 기록하는 성공 클라이언트."""

    def __init__(self) -> None:
        self.history_calls: list = []
        self.time_calls: list = []

    def recommend(
        self, daily_summary, preferred_category, meal_timing,
        user_history_context=None, current_time=None,
    ):
        self.history_calls.append(user_history_context)
        self.time_calls.append(current_time)
        return super().recommend(
            daily_summary, preferred_category, meal_timing,
            user_history_context, current_time,
        )


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
    error = res.json()["error"]
    assert error["code"] == "AI_TIMEOUT"
    assert error["details"] == [{"field": "ai", "reason": "ai_timeout"}]


def test_next_meal_provider_error_502(client, auth_headers):
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("provider_error")
    res = client.post(
        "/v1/recommendations/next-meal", headers=auth_headers, json={"date": "2026-06-27"}
    )
    assert res.status_code == 502
    error = res.json()["error"]
    assert error["code"] == "AI_PROVIDER_ERROR"
    assert error["details"] == [{"field": "ai", "reason": "provider_error"}]


def test_menu_no_candidates_502_with_reason(client, auth_headers):
    """AI 서버가 200 + status=failed(no_candidates) 를 줘도 502 에 사유가 남는다."""
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("no_candidates")
    res = client.post(
        "/v1/recommendations/menu",
        headers=auth_headers,
        json={"meal_type": "lunch"},
    )
    assert res.status_code == 502
    error = res.json()["error"]
    assert error["code"] == "AI_PROVIDER_ERROR"
    assert error["details"] == [{"field": "ai", "reason": "no_candidates"}]


def test_failed_reason_visible_in_ai_call_logs(client, auth_headers):
    """실패 사유(error_message)가 운영 조회 API 에 노출된다 (명세서 6.2)."""
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("no_candidates")
    client.post(
        "/v1/recommendations/next-meal", headers=auth_headers, json={"date": "2026-06-27"}
    )
    res = client.get(
        "/v1/ai-call-logs", headers=auth_headers, params={"status": "failed"}
    )
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["error_message"] == "no_candidates"


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


def test_next_meal_passes_today_food_history_to_ai(client, auth_headers):
    """오늘 먹은 음식 이름·직전 식사가 user_history_context 로 AI에 전달된다."""
    create_meal(client, auth_headers)  # lunch: 김치찌개, 공기밥 (2026-06-27)
    recording = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recording
    res = client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27", "preferred_category": "convenience_store"},
    )
    assert res.status_code == 200
    history = recording.history_calls[-1]
    assert history["today_foods"] == ["김치찌개", "공기밥"]
    assert history["last_meal_type"] == "lunch"
    assert history["last_meal_foods"] == ["김치찌개", "공기밥"]


def test_next_meal_without_meals_sends_no_history(client, auth_headers):
    """기록이 없는 날은 user_history_context 를 보내지 않는다(None)."""
    recording = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recording
    res = client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27"},
    )
    assert res.status_code == 200
    assert recording.history_calls[-1] is None


def test_recommend_passes_current_time_to_ai(client, auth_headers):
    """추천 호출 시 현재 KST 시각(HH:MM)이 AI 에 전달된다."""
    import re

    recording = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recording
    res = client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27"},
    )
    assert res.status_code == 200
    assert re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", recording.time_calls[-1])


def test_default_meal_timing_by_kst_hour(monkeypatch):
    """meal_timing 미지정 시 KST 시각 기준: <10 breakfast, <15 lunch, 이후 dinner."""
    from datetime import datetime, timezone

    import app.api.v1.recommendations as rec

    def at_kst_hour(hour):
        # KST(h) = UTC(h-9)
        return datetime(2026, 7, 15, (hour - 9) % 24, 30, tzinfo=timezone.utc)

    for hour, expected in [(7, "breakfast"), (9, "breakfast"), (10, "lunch"),
                           (14, "lunch"), (15, "dinner"), (22, "dinner")]:
        monkeypatch.setattr(rec, "now_utc", lambda h=hour: at_kst_hour(h))
        assert rec._default_meal_timing() == expected, hour
