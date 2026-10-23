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
from app.services.summary import derive_macro_goals
from tests.conftest import login as social_login
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


def test_personalized_goals_macros_body_weight_based():
    # 여 55kg 유지 1750: 단백질 1.0×55=55g, 지방 25%≈49g, 탄수 나머지≈273g
    goals = personalized_goals("female", None, 165.5, 55, "maintain")
    assert goals["calories"] == 1750
    assert goals["protein"] == 55
    assert goals["fat"] == round(1750 * 0.25 / 9)  # 49
    # 탄수는 단백질·지방을 뺀 나머지 칼로리에서 유도
    assert goals["carbs"] == 273


def test_macro_protein_scales_by_goal_type():
    # 감량·근육 증가 1.6, 일반 유지 1.0 g/kg.
    w = 70
    diet = derive_macro_goals(2000, w, "diet")
    maintain = derive_macro_goals(2000, w, "maintain")
    bulk = derive_macro_goals(2000, w, "bulk")
    assert diet["protein"] == 112
    assert maintain["protein"] == 70
    assert bulk["protein"] == 112
    assert diet["protein"] == bulk["protein"] > maintain["protein"]


def test_macro_fat_default_25pct_and_carbs_remainder():
    m = derive_macro_goals(2000, 70, "maintain")
    assert m["fat"] == round(2000 * 0.25 / 9)  # 56
    # 탄수 = 전체 - 단백질kcal - 지방kcal (나머지 배분)
    assert m["carbs"] == 305


def test_macro_protein_not_capped_for_low_goal():
    # 80kg 유지 500kcal: 단백질 80g을 보존하고 지방 25%, 탄수는 반올림 전 잔여량.
    m = derive_macro_goals(500, 80, "maintain")
    assert m == {"protein": 80, "fat": 14, "carbs": 14}
    assert abs(m["protein"] * 4 + m["fat"] * 9 + m["carbs"] * 4 - 500) <= 10


def test_macro_protein_stays_weight_based_across_calorie_goals():
    p = [derive_macro_goals(g, 80, "maintain")["protein"] for g in (500, 800, 1000, 1200)]
    assert p == [80, 80, 80, 80]
    assert derive_macro_goals(2000, 80, "maintain")["protein"] == 80


def test_macro_protein_not_raised_to_10pct_for_high_goal():
    assert derive_macro_goals(6000, 80, "maintain")["protein"] == 80


def test_macro_sum_matches_goal_unless_protein_and_fat_exceed_it():
    for goal in (500, 700, 900, 1200, 2000, 3500, 6000, 10000):
        for w in (45, 70, 100):
            for mg in ("diet", "maintain", "bulk"):
                m = derive_macro_goals(goal, w, mg)
                protein = w * (1.0 if mg == "maintain" else 1.6)
                carbs = goal * 0.75 / 4 - protein
                assert m["protein"] == round(protein)
                assert m["carbs"] == round(max(carbs, 0))
                if carbs >= 0:
                    assert abs(m["protein"] * 4 + m["fat"] * 9 + m["carbs"] * 4 - goal) <= 10
                else:
                    assert m["carbs"] == 0


def test_macro_fallback_ratio_without_weight():
    # 체중 미상(신체정보 부족)이면 기존 50:30:20 비율로 폴백
    m = derive_macro_goals(2000)
    assert m["protein"] == round(2000 * 0.3 / 4)
    assert m["fat"] == round(2000 * 0.2 / 9)
    assert m["carbs"] == round(2000 * 0.5 / 4)


# --- API: 자동 재계산 vs 수동 설정 보존 ---

def _auth_header(client, email="goal@example.com"):
    res = signup(client, email=email)
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_social_user_first_patch_computes_bmr_goal(client):
    """소셜 가입자(프로필 없음)의 첫 온보딩 PATCH 에서 바로 BMR 목표가 산정돼야 한다.

    회귀 방지: 프로필이 같은 요청에서 생성될 때 goal_source 컬럼 default 가
    flush 전이라 None 이어서 자동 산정 분기를 타지 못하던 버그.
    """
    tokens = social_login(client, social_id="social-bmr-user")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    res = client.patch(
        "/v1/users/me",
        json={"gender": "male", "height": 175, "weight": 70},
        headers=headers,
    )
    assert res.status_code == 200
    # 남 70kg/175cm/기본나이 30: BMR 1648.75 × 1.375 ≈ 2270 (기본 2000 이 아니어야 함)
    assert res.json()["daily_goal_calories"] == 2270
    assert res.json()["goal_source"] == "auto"


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

    # 감량 목표로 변경 → -500 kcal. 단 적자는 유지 열량의 25% 를 넘지 않는다 (2026-10-09)
    res = client.patch(
        "/v1/users/eating-habits", json={"meal_goal": "diet"}, headers=headers
    )
    assert res.status_code == 200
    after = client.get("/v1/users/me", headers=headers).json()["daily_goal_calories"]
    assert after == max(before - 500, round(before * 0.75 / 10) * 10)
    assert after < before
