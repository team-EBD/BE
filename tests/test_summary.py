"""Phase 7 DoD — 일간/주간/월간 요약 (LLM 미사용, 규칙 기반)."""
from __future__ import annotations

from tests.test_meals import MEAL_PAYLOAD, create_meal


def meal_on(day: str) -> dict:
    """해당 KST 날짜 정오에 먹은 MEAL_PAYLOAD (524 kcal, 탄81/단20.9/지11.8)."""
    return {**MEAL_PAYLOAD, "eaten_at": f"{day}T12:00:00+09:00"}


# MEAL_PAYLOAD 의 칼로리 기여 비율: 탄 324 / 단 83.6 / 지 106.2 (총 513.8 kcal)
MEAL_MACRO_RATIO = {"carbs": 63, "protein": 16, "fat": 21}


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


# ---------------------------------------------------------------- daily 확장


def test_daily_summary_streak_and_macro_ratio(client, auth_headers):
    for day in ("2026-06-25", "2026-06-26", "2026-06-27"):
        create_meal(client, auth_headers, meal_on(day))
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-27"}
    )
    body = res.json()
    assert body["streak_days"] == 3
    assert body["macro_ratio"] == MEAL_MACRO_RATIO
    # 해당 date 에 기록이 없으면 date-1 부터 거꾸로 센다
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-28"}
    )
    assert res.json()["streak_days"] == 3


def test_daily_summary_streak_broken_by_gap(client, auth_headers):
    create_meal(client, auth_headers, meal_on("2026-06-25"))
    create_meal(client, auth_headers, meal_on("2026-06-27"))
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-27"}
    )
    assert res.json()["streak_days"] == 1


def test_daily_summary_empty_streak_and_macro(client, auth_headers):
    res = client.get(
        "/v1/nutrition/daily-summary", headers=auth_headers, params={"date": "2026-06-01"}
    )
    body = res.json()
    assert body["streak_days"] == 0
    assert body["macro_ratio"] == {"carbs": 0, "protein": 0, "fat": 0}


# --------------------------------------------------------------- weekly 확장


def test_weekly_summary_extended_fields(client, auth_headers):
    create_meal(client, auth_headers, meal_on("2026-06-27"))
    create_meal(client, auth_headers, meal_on("2026-06-25"))
    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-22"},
    )
    body = res.json()
    assert body["goal_calories"] == 2000
    assert body["achieved_days"] == 2  # 두 날 모두 기록 있고 524 <= 2000
    assert len(body["days"]) == 7
    by_date = {d["date"]: d for d in body["days"]}
    assert by_date["2026-06-27"] == {
        "date": "2026-06-27", "calories": 524, "meal_count": 1, "achieved": True
    }
    assert by_date["2026-06-23"]["meal_count"] == 0
    assert by_date["2026-06-23"]["achieved"] is False
    assert body["macro_ratio"] == MEAL_MACRO_RATIO
    assert body["top_food"]["count"] == 2  # 김치찌개/공기밥 각 2회 (동률)
    assert body["prev_week"] == {"achieved_days": 0, "average_calories": 0}
    assert "달성일" in body["summary_text"]  # 지난주(0일) 대비 증가 → 칭찬


def test_weekly_top_food_has_representative_image(client, auth_headers):
    """top_food 에는 그 음식이 담긴 최근 기록의 사진이 대표 이미지로 실린다."""
    from tests.test_images import upload

    image = upload(client, auth_headers).json()
    with_photo = {**meal_on("2026-06-27"), "meal_image_id": image["meal_image_id"]}
    create_meal(client, auth_headers, with_photo)
    create_meal(client, auth_headers, meal_on("2026-06-25"))  # 사진 없는 기록

    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-22"},
    )
    top = res.json()["top_food"]
    assert top["image_url"] == image["image_url"]


def test_top_food_image_none_without_photo(client, auth_headers):
    """사진 없는 기록만 있으면 image_url 은 null (아이콘 폴백은 FE 담당)."""
    create_meal(client, auth_headers, meal_on("2026-06-27"))
    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-22"},
    )
    assert res.json()["top_food"]["image_url"] is None


def test_weekly_summary_empty_week(client, auth_headers):
    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-01"},
    )
    body = res.json()
    assert body["recorded_days"] == 0
    assert body["achieved_days"] == 0
    assert body["top_food"] is None
    assert body["macro_ratio"] == {"carbs": 0, "protein": 0, "fat": 0}
    assert all(d["meal_count"] == 0 and d["achieved"] is False for d in body["days"])
    assert "기록이 없어요" in body["summary_text"]


def test_weekly_summary_prev_week_comparison(client, auth_headers):
    create_meal(client, auth_headers, meal_on("2026-06-17"))  # 직전 주 (6/15~6/21)
    create_meal(client, auth_headers, meal_on("2026-06-25"))
    res = client.get(
        "/v1/nutrition/weekly-summary",
        headers=auth_headers,
        params={"week_start": "2026-06-22"},
    )
    body = res.json()
    assert body["prev_week"] == {"achieved_days": 1, "average_calories": 524}


# --------------------------------------------------------------- monthly


def test_monthly_summary_with_meals(client, auth_headers):
    for day in ("2026-06-25", "2026-06-26", "2026-06-27"):
        create_meal(client, auth_headers, meal_on(day))
    res = client.get(
        "/v1/nutrition/monthly-summary", headers=auth_headers, params={"month": "2026-06"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["month"] == "2026-06"
    assert body["goal_calories"] == 2000
    assert body["days_counted"] == 30
    assert body["recorded_days"] == 3
    assert body["achieved_days"] == 3
    assert body["achievement_rate"] == 0.1  # 3/30
    assert body["average_calories"] == 524
    assert body["longest_streak"] == 3
    # weeks: 6/1~7, 8~14, 15~21, 22~28, 29~30 (마지막은 잔여 2일)
    assert len(body["weeks"]) == 5
    assert body["weeks"][0]["start"] == "2026-06-01"
    assert body["weeks"][0]["end"] == "2026-06-07"
    assert body["weeks"][4]["start"] == "2026-06-29"
    assert body["weeks"][4]["end"] == "2026-06-30"
    week4 = body["weeks"][3]
    assert week4["recorded_days"] == 3
    assert week4["achieved_days"] == 3
    assert week4["average_calories"] == 524
    assert week4["macro_ratio"] == MEAL_MACRO_RATIO
    assert body["weeks"][0]["recorded_days"] == 0
    # top_foods: 김치찌개/공기밥 각 3회
    assert {f["name"] for f in body["top_foods"]} == {"김치찌개", "공기밥"}
    assert all(f["count"] == 3 for f in body["top_foods"])
    assert body["insights"] == []  # 기록 있는 주가 1개 → 인사이트 없음
    assert isinstance(body["summary_text"], str) and body["summary_text"]


def test_monthly_summary_prev_month(client, auth_headers):
    for day in ("2026-06-25", "2026-06-26", "2026-06-27"):
        create_meal(client, auth_headers, meal_on(day))
    res = client.get(
        "/v1/nutrition/monthly-summary", headers=auth_headers, params={"month": "2026-07"}
    )
    body = res.json()
    assert body["recorded_days"] == 0
    assert body["prev_month"] == {
        "longest_streak": 3,
        "average_calories": 524,
        "achievement_rate": 0.1,
    }
    assert "기록이 없어요" in body["summary_text"]


def test_monthly_summary_empty_month(client, auth_headers):
    res = client.get(
        "/v1/nutrition/monthly-summary", headers=auth_headers, params={"month": "2026-05"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["days_counted"] == 31
    assert body["recorded_days"] == 0
    assert body["achieved_days"] == 0
    assert body["achievement_rate"] == 0.0
    assert body["average_calories"] == 0
    assert body["longest_streak"] == 0
    assert body["top_foods"] == []
    assert body["insights"] == []
    assert len(body["weeks"]) == 5
    assert all(w["recorded_days"] == 0 for w in body["weeks"])


def test_monthly_summary_invalid_month_422(client, auth_headers):
    for bad in ("2026-13", "2026/06", "202606", "2026-6", "abcd-ef"):
        res = client.get(
            "/v1/nutrition/monthly-summary", headers=auth_headers, params={"month": bad}
        )
        assert res.status_code == 422, bad


def test_daily_summary_day_start_hour_moves_late_night_meal(client, auth_headers):
    """day_start_hour=6 이면 새벽 야식(6/28 01:30)이 전날(6/27) 섭취로 잡힌다."""
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "eaten_at": "2026-06-28T01:30:00+09:00"})

    def total(day: str, **params):
        res = client.get(
            "/v1/nutrition/daily-summary",
            headers=auth_headers,
            params={"date": day, **params},
        )
        assert res.status_code == 200, res.text
        return res.json()["total"]["calories"]

    # 기본(자정 경계) — 먹은 날짜 그대로 6/28
    assert total("2026-06-27") == 0
    assert total("2026-06-28") == 524.0
    # 6시 경계 — 전날 기록으로 이동
    assert total("2026-06-27", day_start_hour=6) == 524.0
    assert total("2026-06-28", day_start_hour=6) == 0


def test_daily_summary_day_start_hour_streak(client, auth_headers):
    """연속 기록(streak)도 같은 하루 경계 규칙을 따른다."""
    create_meal(client, auth_headers, meal_on("2026-06-26"))
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "eaten_at": "2026-06-28T02:00:00+09:00"})
    res = client.get(
        "/v1/nutrition/daily-summary",
        headers=auth_headers,
        params={"date": "2026-06-27", "day_start_hour": 6},
    )
    # 6시 경계에서 6/28 새벽 기록은 6/27 → 6/26~6/27 연속 2일
    assert res.json()["streak_days"] == 2


def test_daily_summary_rejects_out_of_range_day_start_hour(client, auth_headers):
    res = client.get(
        "/v1/nutrition/daily-summary",
        headers=auth_headers,
        params={"date": "2026-06-27", "day_start_hour": 20},
    )
    assert res.status_code in (400, 422)
