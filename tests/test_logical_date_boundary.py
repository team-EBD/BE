"""식단 저장 시 요약 캐시가 게임 원장과 같은 논리 날짜를 쓰는지 검증한다."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models import DailyNutritionSummary, RewardLedger
from tests.test_meals import MEAL_PAYLOAD, create_meal


def _summary_by_date(db_factory) -> dict[date, DailyNutritionSummary]:
    with db_factory() as db:
        return {
            row.summary_date: row
            for row in db.scalars(select(DailyNutritionSummary)).all()
        }


def test_meal_cache_uses_game_logical_date_on_create_update_delete(
    client, auth_headers, db_factory
):
    meal = create_meal(
        client,
        auth_headers,
        {**MEAL_PAYLOAD, "eaten_at": "2026-09-20T03:00:00+09:00"},
    )

    with db_factory() as db:
        reward = db.scalar(
            select(RewardLedger).where(
                RewardLedger.reason == "meal",
                RewardLedger.ref_id == str(meal["meal_id"]),
            )
        )
        assert reward is not None
        assert reward.logical_date == date(2026, 9, 19)
        logical_date = reward.logical_date.isoformat()

    summaries = _summary_by_date(db_factory)
    assert set(summaries) == {date(2026, 9, 19)}
    assert summaries[date(2026, 9, 19)].total_calories == 524
    assert summaries[date(2026, 9, 19)].meal_count == 1

    daily = client.get(
        "/v1/nutrition/daily-summary",
        headers=auth_headers,
        params={"date": logical_date},
    )
    listed = client.get("/v1/meals", headers=auth_headers, params={"date": logical_date})
    calendar = client.get(
        "/v1/meals/calendar", headers=auth_headers, params={"month": "2026-09"}
    )
    assert daily.status_code == listed.status_code == calendar.status_code == 200
    assert daily.json()["total"]["calories"] == 524
    assert [row["meal_id"] for row in listed.json()["meals"]] == [meal["meal_id"]]
    assert any(
        row["date"] == logical_date and row["meal_count"] == 1
        for row in calendar.json()["days"]
    )

    updated = client.patch(
        f"/v1/meals/{meal['meal_id']}",
        headers=auth_headers,
        json={"eaten_at": "2026-09-20T07:00:00+09:00"},
    )
    assert updated.status_code == 200, updated.text
    summaries = _summary_by_date(db_factory)
    assert summaries[date(2026, 9, 19)].total_calories == 0
    assert summaries[date(2026, 9, 20)].total_calories == 524

    deleted = client.delete(f"/v1/meals/{meal['meal_id']}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    summaries = _summary_by_date(db_factory)
    assert summaries[date(2026, 9, 20)].total_calories == 0
    assert summaries[date(2026, 9, 20)].meal_count == 0
