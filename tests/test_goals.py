"""BMR/TDEE 기반 목표 칼로리 산정 (services/goals) + API 연동 검증.

Mifflin-St Jeor: BMR = 10*체중 + 6.25*키 - 5*나이 (+5 남 / -161 여)
TDEE = BMR × 1.375 (가벼운 활동), meal_goal 조정: diet -500 / bulk +300.
"""
from __future__ import annotations

from app.services.goals import (
    MIN_GOAL_CALORIES,
    calculate_goal_calories,
    personalized_goals,
)
from tests.test_auth_email import signup


# --- 단위: calculate_goal_calories ---

def test_male_maintain():
    # 남 70kg/175cm/30세: BMR 1648.75 × 1.375 = 2267.0 → 2270
    assert calculate_goal_calories("male", None, 175, 70) == 2270


def test_female_maintain():
    # 여 55kg/165.5cm/30세: BMR 1273.375 × 1.375 = 1750.9 → 1750
    assert calculate_goal_calories("female", None, 165.5, 55) == 1750


def test_meal_goal_adjustments():
    base = calculate_goal_calories("male", None, 175, 70, "maintain")
    assert calculate_goal_calories("male", None, 175, 70, "diet") == base - 500
    assert calculate_goal_calories("male", None, 175, 70, "bulk") == base + 300


def test_birth_year_reflected():
    # 나이가 많을수록 BMR 감소 → 목표 칼로리 감소
    young = calculate_goal_calories("male", 2006, 175, 70)
    old = calculate_goal_calories("male", 1966, 175, 70)
    assert young > old


def test_missing_body_info_returns_none():
    assert calculate_goal_calories("male", None, None, 70) is None
    assert calculate_goal_calories("male", None, 175, None) is None
    assert personalized_goals("male", None, None, None) is None


def test_min_floor():
    # 극단적 입력(감량 + 저체중)도 1200 kcal 아래로 내려가지 않는다
    assert calculate_goal_calories("female", None, 140, 35, "diet") >= MIN_GOAL_CALORIES


def test_unknown_gender_between_male_and_female():
    male = calculate_goal_calories("male", None, 170, 60)
    female = calculate_goal_calories("female", None, 170, 60)
    unknown = calculate_goal_calories(None, None, 170, 60)
    assert female < unknown < male


def test_personalized_goals_macros_derived():
    goals = personalized_goals("female", None, 165.5, 55)
    assert goals["calories"] == 1750
    # 50:30:20 유도 (탄단 4kcal/g, 지 9kcal/g)
    assert goals["carbs"] == round(1750 * 0.5 / 4)
    assert goals["protein"] == round(1750 * 0.3 / 4)
    assert goals["fat"] == round(1750 * 0.2 / 9)


# --- API: 자동 재계산 vs 수동 설정 보존 ---

def _auth_header(client, email="goal@example.com"):
    res = signup(client, email=email)
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_weight_change_recomputes_auto_goal(client):
    headers = _auth_header(client)
    before = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]

    # 몸무게 증가 → 자동 목표 상향
    res = client.patch("/v1/users/me", json={"weight": 70.0}, headers=headers)
    assert res.status_code == 200
    assert res.json()["daily_goal_calories"] > before


def test_manual_goal_not_overwritten_by_body_change(client):
    headers = _auth_header(client, email="manual@example.com")

    # 사용자가 목표를 직접 설정 → manual
    res = client.patch(
        "/v1/users/me", json={"daily_goal_calories": 1800}, headers=headers
    )
    assert res.json()["daily_goal_calories"] == 1800

    # 이후 신체정보가 바뀌어도 직접 설정한 목표는 유지된다
    res = client.patch("/v1/users/me", json={"weight": 90.0}, headers=headers)
    assert res.json()["daily_goal_calories"] == 1800


def test_birth_year_editable_and_recomputes_auto_goal(client):
    headers = _auth_header(client, email="age@example.com")
    before = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]

    # 출생연도 입력(나이 반영) — 기본 가정(30세)보다 나이가 많으면 목표 하향
    res = client.patch("/v1/users/me", json={"birth_year": 1966}, headers=headers)
    assert res.status_code == 200
    assert res.json()["birth_year"] == 1966
    assert res.json()["daily_goal_calories"] < before


def test_revert_manual_goal_to_auto(client):
    headers = _auth_header(client, email="revert@example.com")
    auto_goal = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]

    # 직접 설정 → manual
    res = client.patch(
        "/v1/users/me", json={"daily_goal_calories": 1600}, headers=headers
    )
    assert res.json()["goal_source"] == "manual"
    assert res.json()["daily_goal_calories"] == 1600

    # 자동 계산으로 되돌리기 → BMR 기반 값으로 재계산
    res = client.patch("/v1/users/me", json={"goal_source": "auto"}, headers=headers)
    assert res.json()["goal_source"] == "auto"
    assert res.json()["daily_goal_calories"] == auto_goal

    # 되돌린 뒤에는 신체정보 변경 시 다시 자동 재계산된다
    res = client.patch("/v1/users/me", json={"weight": 80.0}, headers=headers)
    assert res.json()["daily_goal_calories"] > auto_goal


def test_meal_goal_change_adjusts_auto_goal(client):
    headers = _auth_header(client, email="diet@example.com")
    before = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]

    # 감량 목표로 변경 → -500 kcal
    res = client.patch(
        "/v1/users/eating-habits", json={"meal_goal": "diet"}, headers=headers
    )
    assert res.status_code == 200
    after = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]
    assert after == before - 500
