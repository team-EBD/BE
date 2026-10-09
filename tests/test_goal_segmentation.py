"""목표 세분화 (2026-10-09) — 목표·활동량·속도별 목표 산정, /users/me 연동, 펫 하루 코칭."""
from __future__ import annotations

from datetime import UTC, date, datetime

from app.services.coach import build_daily_coach, coach_stage
from app.services.goals import (
    MIN_GOAL_CALORIES,
    MIN_GOAL_CALORIES_MALE,
    calculate_goal_calories,
    goal_plan,
    personalized_goals,
    resolve_primary_goal,
)
from tests.test_auth_email import signup
from tests.test_meals import MEAL_PAYLOAD, create_meal

MALE = ("male", None, 175, 70)  # BMR 1648.75, 가벼운 활동 유지 2270


def _headers(client, email="seg@example.com"):
    res = signup(client, email=email)
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


# --- 단위: 목표 산정 ---

def test_activity_level_scales_maintenance():
    sedentary = calculate_goal_calories(*MALE, activity_level="sedentary")
    light = calculate_goal_calories(*MALE, activity_level="light")
    very = calculate_goal_calories(*MALE, activity_level="very_active")
    assert sedentary == 1980  # 1648.75 × 1.2
    assert light == calculate_goal_calories(*MALE) == 2270  # 미입력은 가벼운 활동
    assert very == 3130  # × 1.9


def test_pace_changes_adjustment_per_goal():
    # 유지 3130 — 가장 큰 적자(750)도 25% 상한(783)에 걸리지 않는다
    base = calculate_goal_calories(*MALE, activity_level="very_active")
    lose = [
        calculate_goal_calories(
            *MALE, activity_level="very_active", primary_goal="lose_weight", goal_pace=p
        )
        for p in ("slow", "normal", "fast")
    ]
    assert [base - v for v in lose] == [250, 500, 750]
    muscle = calculate_goal_calories(*MALE, primary_goal="gain_muscle", goal_pace="slow")
    weight = calculate_goal_calories(*MALE, primary_goal="gain_weight", goal_pace="fast")
    assert muscle == 2270 + 150
    assert weight == 2270 + 700
    # 방향이 없는 목표는 속도를 무시한다
    assert calculate_goal_calories(*MALE, primary_goal="eat_healthy", goal_pace="fast") == 2270


def test_legacy_meal_goal_matches_normal_pace():
    # primary_goal 이 없는 기존 사용자는 예전 값 그대로
    assert calculate_goal_calories(*MALE, "diet") == calculate_goal_calories(
        *MALE, primary_goal="lose_weight"
    )
    assert calculate_goal_calories(*MALE, "bulk") == 2270 + 300
    assert resolve_primary_goal(None, None) == "maintain"
    assert resolve_primary_goal("gain_weight", "diet") == "gain_weight"
    assert resolve_primary_goal(None, "eat_healthy") == "eat_healthy"


def test_deficit_capped_at_quarter_of_maintenance():
    # 여 55kg: 유지 1751 → 빠른 감량 -750 은 25%(438)로 제한
    plan = goal_plan("female", None, 165.5, 55, primary_goal="lose_weight", goal_pace="fast")
    assert plan["calories"] == 1310
    assert plan["floor_applied"] is False


def test_floor_by_gender():
    small_f = goal_plan("female", None, 150, 40, primary_goal="lose_weight")
    assert small_f["calories"] == MIN_GOAL_CALORIES and small_f["floor_applied"] is True
    small_m = goal_plan("male", None, 160, 48, primary_goal="lose_weight", activity_level="sedentary")
    assert small_m["calories"] == MIN_GOAL_CALORIES_MALE and small_m["floor_applied"] is True


def test_plan_explains_weekly_change_and_weeks_to_target():
    plan = goal_plan(
        *MALE, primary_goal="lose_weight", activity_level="active", target_weight=65
    )
    assert plan["tdee"] == 2844 and plan["adjustment"] == -504  # 2340 − 2844 (10 kcal 반올림 포함)
    assert plan["weekly_change_kg"] == -0.46
    assert plan["weeks_to_target"] == 11  # 5kg ÷ 0.46
    # 방향이 반대인 목표 체중·방향 없는 목표는 기간을 내지 않는다
    assert goal_plan(*MALE, primary_goal="lose_weight", target_weight=80)["weeks_to_target"] is None
    assert goal_plan(*MALE, primary_goal="maintain", target_weight=65)["weeks_to_target"] is None
    assert goal_plan(*MALE, primary_goal="maintain")["weekly_change_kg"] == 0


def test_protein_coefficient_by_primary_goal():
    p = {
        g: personalized_goals(*MALE, primary_goal=g, activity_level="active")["protein"]
        for g in ("lose_weight", "maintain", "gain_muscle", "gain_weight", "eat_healthy")
    }
    assert p == {
        "lose_weight": 140, "maintain": 112, "gain_muscle": 126, "gain_weight": 112, "eat_healthy": 84,
    }


# --- API: /users/me ---

def test_goal_setup_recalculates_and_syncs_meal_goal(client):
    headers = _headers(client)
    before = client.get("/v1/users/me", headers=headers).json()
    assert before["primary_goal"] is None and before["focus_areas"] == []
    assert before["goal_plan"] is not None  # 자동 산정 사용자는 근거를 받는다

    res = client.patch(
        "/v1/users/me",
        headers=headers,
        json={
            "primary_goal": "gain_muscle",
            "activity_level": "moderate",
            "goal_pace": "normal",
            "target_weight": 58,
            "focus_areas": ["protein", "balance", "protein"],
            "goal_source": "auto",
        },
    )
    assert res.status_code == 200, res.text
    me = res.json()
    # 여 55kg/165.5cm: BMR 1273.375 × 1.55 = 1973.7 → +300 → 2270
    assert me["daily_goal_calories"] == 2270
    assert me["daily_goal_protein"] == round(55 * 1.8)
    assert me["primary_goal"] == "gain_muscle"
    assert me["activity_level"] == "moderate"
    assert me["target_weight"] == 58
    assert me["focus_areas"] == ["protein", "balance"]
    assert me["goal_plan"]["tdee"] == 1974
    assert me["goal_plan"]["weeks_to_target"] is not None
    # 예전 목표 유형도 함께 맞춰진다
    habits = client.get("/v1/users/eating-habits", headers=headers).json()
    assert habits["meal_goal"] == "bulk"


def test_goal_setup_overrides_manual_only_when_auto_requested(client):
    headers = _headers(client, email="manual@example.com")
    client.patch("/v1/users/me", headers=headers, json={"daily_goal_calories": 1900})
    # 직접 정한 칼로리는 목표를 바꿔도 유지되고, 탄단지만 새 목표를 따른다
    me = client.patch(
        "/v1/users/me", headers=headers, json={"primary_goal": "lose_weight"}
    ).json()
    assert me["daily_goal_calories"] == 1900 and me["goal_source"] == "manual"
    assert me["daily_goal_protein"] == round(55 * 2.0)
    assert me["goal_plan"] is None
    # goal_source=auto 를 함께 보내면(목표 설정 마법사) 다시 계산한다
    me = client.patch(
        "/v1/users/me", headers=headers, json={"primary_goal": "lose_weight", "goal_source": "auto"}
    ).json()
    assert me["goal_source"] == "auto" and me["daily_goal_calories"] == 1310


def test_target_weight_can_be_cleared(client):
    headers = _headers(client, email="clear@example.com")
    client.patch("/v1/users/me", headers=headers, json={"target_weight": 50})
    assert client.get("/v1/users/me", headers=headers).json()["target_weight"] == 50
    me = client.patch("/v1/users/me", headers=headers, json={"target_weight": None}).json()
    assert me["target_weight"] is None


def test_legacy_meal_goal_patch_keeps_compatible_primary_goal(client):
    headers = _headers(client, email="legacy@example.com")
    client.patch("/v1/users/me", headers=headers, json={"primary_goal": "eat_healthy"})
    # 이전 앱이 같은 유형(maintain)으로 저장해도 세분화한 목표는 지워지지 않는다
    client.patch("/v1/users/eating-habits", headers=headers, json={"meal_goal": "maintain"})
    assert client.get("/v1/users/me", headers=headers).json()["primary_goal"] == "eat_healthy"
    # 다른 유형으로 바꾸면 따라간다
    client.patch("/v1/users/eating-habits", headers=headers, json={"meal_goal": "diet"})
    me = client.get("/v1/users/me", headers=headers).json()
    assert me["primary_goal"] == "lose_weight" and me["daily_goal_calories"] == 1310


def test_invalid_goal_values_rejected(client):
    headers = _headers(client, email="invalid@example.com")
    for body in (
        {"primary_goal": "keto"},
        {"activity_level": "extreme"},
        {"focus_areas": ["sugar"]},
        {"target_weight": 5},
    ):
        # 이 서비스는 요청 검증 실패를 400 으로 돌려준다
        assert client.patch("/v1/users/me", headers=headers, json=body).status_code == 400


# --- 단위: 펫 코칭 ---

GOALS = {"calories": 2000, "carbs": 250, "protein": 120, "fat": 55}
NOON = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)  # KST 12:00
NIGHT = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)  # KST 21:30


def _total(calories, protein, carbs=200, fat=50):
    return {"calories": calories, "protein": protein, "carbs": carbs, "fat": fat}


def _coach(total, goal, stage="wrap_up", now=NIGHT, meal_count=3, focus=()):
    return build_daily_coach(
        total, GOALS, meal_count, primary_goal=goal, focus_areas=focus, stage=stage, now=now
    )


def test_coach_none_without_meals():
    assert build_daily_coach(_total(0, 0, 0, 0), GOALS, 0) is None


def test_coach_stage_by_time_and_date():
    today = date(2026, 10, 9)
    assert coach_stage(today, NOON, 6) == "in_progress"
    assert coach_stage(today, NIGHT, 6) == "wrap_up"
    assert coach_stage(date(2026, 10, 8), NOON, 6) == "wrap_up"
    # 자정을 넘긴 새벽 2시는 아직 어제(10/9)의 하루 정리
    assert coach_stage(today, datetime(2026, 10, 9, 17, 0, tzinfo=UTC), 6) == "wrap_up"


def test_coach_same_intake_differs_by_goal():
    # 열량은 넘겼고 단백질은 모자란 하루
    total = _total(2400, 70)
    assert _coach(total, "lose_weight")["code"] == "calories_over"
    assert _coach(total, "gain_muscle")["code"] == "protein_low"
    # 단백질까지 채운 증량 사용자에게 초과는 칭찬이다
    met = _coach(_total(2400, 125), "gain_weight")
    assert met["code"] == "calories_met" and met["tone"] == "praise"


def test_coach_low_calories_only_flagged_when_too_low_for_diet():
    assert _coach(_total(1500, 110), "lose_weight")["code"] == "on_track"  # 75% 는 감량 중 정상
    low = _coach(_total(1000, 110), "lose_weight")
    assert low["code"] == "calories_low" and low["tone"] == "warn"
    assert _coach(_total(1500, 110), "gain_weight")["code"] == "calories_low"


def test_coach_in_progress_uses_time_of_day():
    # 점심 무렵 600kcal·단백질 40g 은 무난하다 — 아침부터 부족하다고 하지 않는다
    midday = _coach(_total(600, 40, 80, 15), "maintain", stage="in_progress", now=NOON, meal_count=1)
    assert midday["code"] == "on_track" and midday["stage"] == "in_progress"
    assert all(item["status"] == "ok" for item in midday["items"])
    # 같은 양이라도 하루를 마친 뒤라면 부족하다
    assert _coach(_total(600, 40, 80, 15), "maintain", meal_count=1)["code"] in (
        "protein_low", "calories_low",
    )
    lacking = _coach(_total(600, 15, 80, 15), "maintain", stage="in_progress", now=NOON, meal_count=1)
    assert lacking["code"] == "protein_low" and "아직" in lacking["message"]


def test_coach_focus_area_moves_priority():
    total = _total(2400, 70)
    assert _coach(total, "maintain")["code"] == "calories_over"
    assert _coach(total, "maintain", focus=("protein",))["code"] == "protein_low"
    skipped = _coach(_total(900, 30), "maintain", meal_count=1, focus=("skipping",))
    assert skipped["code"] == "meals_skipped"


def test_coach_items_and_message_shape():
    coach = _coach(_total(2400, 70, 320, 50), "maintain")
    by = {item["nutrient"]: item for item in coach["items"]}
    assert by["calories"] == {"nutrient": "calories", "status": "high", "percent": 120}
    assert by["protein"]["status"] == "low" and by["carbs"]["status"] == "high"
    assert by["fat"]["status"] == "ok"
    assert coach["nutrient"] == "calories"
    assert "400kcal" in coach["detail"]
    # 말풍선 두 줄 안에 들어가는 길이
    assert len(coach["message"]) <= 40


def test_daily_summary_includes_coach(client, auth_headers):
    params = {"date": "2026-06-27"}
    empty = client.get("/v1/nutrition/daily-summary", headers=auth_headers, params=params).json()
    assert empty["coach"] is None

    create_meal(client, auth_headers)  # 524 kcal, 단백질 20.9g — 지난 날짜라 하루 정리
    client.patch("/v1/users/me", headers=auth_headers, json={"focus_areas": ["protein"]})
    body = client.get("/v1/nutrition/daily-summary", headers=auth_headers, params=params).json()
    assert body["coach"]["stage"] == "wrap_up"
    assert body["coach"]["code"] == "protein_low"
    assert {item["nutrient"] for item in body["coach"]["items"]} == {
        "calories", "protein", "carbs", "fat",
    }
    assert MEAL_PAYLOAD["meal_type"] == "lunch"  # 기존 요약 필드는 그대로
    assert "summary_text" in body
