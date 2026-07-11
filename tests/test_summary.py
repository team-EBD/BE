"""Phase 7 DoD — 일간/주간 요약 (LLM 미사용, 규칙 기반)."""
from __future__ import annotations

from tests.test_meals import MEAL_PAYLOAD, create_meal


def test_daily_summary_empty_day(client, auth_headers):
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-01"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total"]["calories"] == 0
    assert "기록이 없어요" in body["summary_text"]


def test_daily_summary_after_meal(client, auth_headers):
    create_meal(client, auth_headers)  # 524 kcal, 단백질 20.9g
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-27"}
    )
    body = res.json()
    assert body["total"]["calories"] == 524.0
    assert body["goal"]["calories"] == 2000  # 목표 미설정 → 기본값
    assert body["progress"]["calories"] == 0.26
    assert body["remaining_calories"] == 1476
    assert "단백질" in body["summary_text"]  # 20.9/120 < 0.8 → 부족 문구


def test_daily_summary_uses_profile_goal(client, auth_headers):
    client.patch(
        "/v1/users/me", headers=auth_headers, json={"daily_goal_calories": 1000}
    )
    create_meal(client, auth_headers)
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-27"}
    )
    body = res.json()
    assert body["goal"]["calories"] == 1000
    assert body["remaining_calories"] == 476


def test_daily_summary_updates_after_delete(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    client.delete(f"/v1/meals/{meal_id}", headers=auth_headers)
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-27"}
    )
    assert res.json()["total"]["calories"] == 0


def test_weekly_summary(client, auth_headers):
    create_meal(client, auth_headers)  # 6/27 (week of 6/22~6/28)
    create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "eaten_at": "2026-06-25T12:00:00+09:00"}
    )
    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-22"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["week_start"] == "2026-06-22"
    assert body["week_end"] == "2026-06-28"
    assert body["recorded_days"] == 2
    assert body["average"]["calories"] == 524
