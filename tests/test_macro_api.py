"""탄단지 v2 저장 경로·읽기 판정·수동 칼로리 보존 회귀 테스트."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import UserProfile
from tests.test_auth_email import signup


@pytest.fixture(autouse=True)
def fixed_year(monkeypatch):
    now = lambda: datetime(2026, 10, 21, tzinfo=UTC)
    monkeypatch.setattr("app.services.macros.now_utc", now)
    monkeypatch.setattr("app.services.goals.now_utc", now)


def patch_me(client, headers, body):
    res = client.patch("/v1/users/me", headers=headers, json=body)
    assert res.status_code == 200, res.text
    me = res.json()
    assert client.get("/v1/users/me", headers=headers).json() == me
    return me


def test_macro_plan_null_without_profile_or_weight(client, auth_headers):
    assert client.get("/v1/users/me", headers=auth_headers).json()["macro_plan"] is None
    me = patch_me(client, auth_headers, {"daily_goal_calories": 2000})
    assert me["macro_plan"] is None
    assert (me["daily_goal_carbs"], me["daily_goal_protein"], me["daily_goal_fat"]) == (250, 150, 44)


@pytest.mark.parametrize("body,status,reasons,macros,minimum", [
    ({"weight": 70, "daily_goal_calories": 2000}, "ok", [], (305, 70, 56), 1070),
    ({"weight": 70, "daily_goal_calories": 3000}, "info",
     ["protein_under_10pct", "carbs_over_65pct"], (492, 70, 83), 1070),
    ({"weight": 80, "daily_goal_calories": 500}, "review",
     ["carbs_under_100", "protein_over_40pct"], (14, 80, 14), 1120),
    ({"weight": 80, "daily_goal_calories": 500, "primary_goal": "lose_weight"}, "infeasible",
     ["protein_fat_exceed_calories", "protein_over_40pct"], (0, 128, 14), 1380),
    ({"weight": 80, "daily_goal_calories": 500, "primary_goal": "lose_weight", "birth_year": 2008},
     "unsupported", ["under_19", "protein_fat_exceed_calories", "protein_over_40pct"], (0, 128, 14), 1380),
])
def test_me_returns_status_and_stores_numbers_even_if_unsupported(
    client, auth_headers, db_factory, body, status, reasons, macros, minimum,
):
    me = patch_me(client, auth_headers, body)
    assert me["goal_source"] == "manual"
    assert me["goal_plan"] is None
    assert me["macro_plan"] == {
        "status": status, "reasons": reasons, "min_calories": minimum,
        "reference_weight": float(body["weight"]),
    }
    assert tuple(me[f"daily_goal_{key}"] for key in ("carbs", "protein", "fat")) == macros
    with db_factory() as db:
        profile = db.scalar(select(UserProfile))
        assert profile.goal_calories == body["daily_goal_calories"]
        assert (profile.goal_carbs, profile.goal_protein, profile.goal_fat) == macros


@pytest.mark.parametrize("changed,protein,ref_weight", [
    ({"weight": 80}, 80, 80.0),
    ({"height": 120}, 50, 49.9),  # 43.2 + (70−43.2)×0.25
    ({"birth_year": 1961}, 84, 70.0),
    ({"activity_level": "active"}, 98, 70.0),
    ({"primary_goal": "gain_muscle"}, 112, 70.0),
])
def test_manual_recalculates_each_macro_input_without_changing_calories(
    client, auth_headers, changed, protein, ref_weight,
):
    before = patch_me(client, auth_headers, {
        "daily_goal_calories": 2000, "weight": 70, "height": 175,
        "birth_year": 1996, "primary_goal": "maintain", "activity_level": "light",
    })
    assert before["daily_goal_protein"] == 70
    after = patch_me(client, auth_headers, changed)
    assert after["daily_goal_calories"] == 2000
    assert after["goal_source"] == "manual" and after["goal_plan"] is None
    assert after["daily_goal_protein"] == protein
    assert after["daily_goal_carbs"] != before["daily_goal_carbs"]
    assert after["daily_goal_fat"] == 56
    assert after["macro_plan"]["reference_weight"] == ref_weight


def test_manual_legacy_meal_goal_change_recalculates(client, auth_headers):
    patch_me(client, auth_headers, {"weight": 70, "daily_goal_calories": 2000})
    res = client.patch("/v1/users/eating-habits", headers=auth_headers, json={"meal_goal": "diet"})
    assert res.status_code == 200
    me = client.get("/v1/users/me", headers=auth_headers).json()
    assert me["daily_goal_calories"] == 2000 and me["goal_source"] == "manual"
    assert (me["daily_goal_carbs"], me["daily_goal_protein"], me["daily_goal_fat"]) == (263, 112, 56)


def test_first_weight_only_profile_and_later_height_recalculate(client, auth_headers):
    first = patch_me(client, auth_headers, {"weight": 100})
    assert first["goal_source"] == "auto" and first["daily_goal_calories"] == 2000
    assert first["goal_plan"] is None
    assert first["daily_goal_protein"] == 100
    assert first["macro_plan"]["reference_weight"] == 100.0
    second = patch_me(client, auth_headers, {"height": 170})
    assert second["daily_goal_calories"] != 2000 and second["goal_plan"] is not None
    assert second["daily_goal_protein"] == 90
    assert second["macro_plan"]["reference_weight"] == 90.0


def test_first_profile_uses_existing_legacy_goal(client, auth_headers):
    client.patch("/v1/users/eating-habits", headers=auth_headers, json={"meal_goal": "bulk"})
    me = patch_me(client, auth_headers, {"weight": 70})
    assert me["daily_goal_protein"] == 112


def test_signup_uses_height_adjusted_v2_formula(client):
    res = signup(client, weight=100, height=170)
    assert res.status_code == 201
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}
    me = client.get("/v1/users/me", headers=headers).json()
    assert me["daily_goal_calories"] == 2410
    assert (me["daily_goal_carbs"], me["daily_goal_protein"], me["daily_goal_fat"]) == (362, 90, 67)
    assert me["macro_plan"] == {
        "status": "ok", "reasons": [], "min_calories": 1180, "reference_weight": 90.0,
    }


@pytest.mark.parametrize("goal,calories,code", [
    ("lose_weight", 1400, "deficit_over_25pct"),
    ("gain_muscle", 3000, "surplus_over_20pct"),
    ("gain_weight", 3000, "surplus_over_20pct"),
    ("maintain", 3000, "far_from_tdee"),
    ("eat_healthy", 1500, "far_from_tdee"),
])
def test_manual_plan_uses_tdee_even_when_goal_plan_is_null(client, auth_headers, goal, calories, code):
    me = patch_me(client, auth_headers, {
        "gender": "male", "height": 175, "weight": 70,
        "primary_goal": goal, "daily_goal_calories": calories,
    })
    assert me["goal_plan"] is None
    assert code in me["macro_plan"]["reasons"]


@pytest.mark.parametrize("goal", ["gain_muscle", "gain_weight"])
def test_auto_surplus_cap_and_actual_adjustment(client, auth_headers, goal):
    me = patch_me(client, auth_headers, {
        "gender": "female", "height": 165.5, "weight": 55,
        "primary_goal": goal, "goal_pace": "fast",
    })
    assert me["daily_goal_calories"] == 2100
    assert me["goal_plan"] == {
        "bmr": 1273, "tdee": 1751, "adjustment": 349, "weekly_change_kg": 0.32,
        "weeks_to_target": None, "floor_applied": False,
    }
    assert me["daily_goal_protein"] == (88 if goal == "gain_muscle" else 66)
    assert "surplus_over_20pct" not in me["macro_plan"]["reasons"]


def test_get_recomputes_diagnostics_without_rewriting_old_stored_macros(client, auth_headers, db_factory):
    me = patch_me(client, auth_headers, {"weight": 80, "daily_goal_calories": 2000})
    with db_factory() as db:
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == me["id"]))
        profile.goal_calories = 500
        profile.goal_carbs, profile.goal_protein, profile.goal_fat = 50, 44, 14  # 예전 클램프 결과
        db.commit()
    me = client.get("/v1/users/me", headers=auth_headers).json()
    assert (me["daily_goal_carbs"], me["daily_goal_protein"], me["daily_goal_fat"]) == (50, 44, 14)
    assert me["macro_plan"]["reasons"] == ["carbs_under_100", "protein_over_40pct"]
    assert me["macro_plan"]["min_calories"] == 1120
    with db_factory() as db:
        assert db.scalar(select(UserProfile)).goal_protein == 44
