"""사용자별 목표 칼로리 산정 (BMR/TDEE 기반).

Mifflin-St Jeor 공식 (1990, 현재 임상에서 가장 널리 쓰이는 안정시대사량 추정식):
    BMR = 10*체중(kg) + 6.25*키(cm) - 5*나이 (+5 남성 / -161 여성)
TDEE = BMR × 활동계수. 활동량을 받지 않은 사용자는
'가벼운 활동(주 1~3회 운동)' 1.375 를 기본 가정으로 쓴다.

목표(user_profiles.primary_goal)와 속도(goal_pace)별 하루 열량 조정 (2026-10-09 목표 세분화):
    lose_weight(감량)   -250 / -500 / -750   (주 약 0.25 / 0.5 / 0.75 kg)
    gain_muscle(근육)   +150 / +300 / +450   (지방 증가를 줄인 작은 잉여)
    gain_weight(증량)   +300 / +500 / +700
    maintain(유지) · eat_healthy(건강한 식습관)  0
primary_goal 이 없는 사용자는 예전 목표 유형(eating_habits.meal_goal)을 같은 표에 대응시킨다
(diet→lose_weight, bulk→gain_muscle, maintain→maintain) — 속도 '보통' 이 예전 값과 같다.

안전장치: 적자는 TDEE 의 25% 를 넘지 않고, 하한은 남성 1,500 / 그 외 1,200 kcal.
(하한 없이 1,000 kcal 대 목표를 내는 앱이 공개적으로 비판받는다 —
 근거: 워크스페이스 루트 COMPETITOR-SEGMENTATION-RESEARCH-20261009.md)

키/몸무게가 없으면 None 을 반환한다 — 호출부는 기본 목표(2000)를 쓴다.
"""
from __future__ import annotations

import math

from app.core.timeutil import KST, now_utc
from app.services.summary import derive_macro_goals

# 활동 수준 → 활동계수 (관행값). 미입력 시 light.
ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
DEFAULT_ACTIVITY_LEVEL = "light"
ACTIVITY_FACTOR = ACTIVITY_FACTORS[DEFAULT_ACTIVITY_LEVEL]

PRIMARY_GOALS = ("lose_weight", "maintain", "gain_muscle", "gain_weight", "eat_healthy")
GOAL_PACES = ("slow", "normal", "fast")
DEFAULT_GOAL_PACE = "normal"
# 목표·속도별 하루 열량 조정 (kcal). 표에 없는 목표는 0.
PACE_ADJUSTMENT = {
    "lose_weight": {"slow": -250, "normal": -500, "fast": -750},
    "gain_muscle": {"slow": 150, "normal": 300, "fast": 450},
    "gain_weight": {"slow": 300, "normal": 500, "fast": 700},
}
# 예전 목표 유형(meal_goal)과의 대응. 추천·요약 등 meal_goal 을 읽는 코드와 스토어에 나가 있는
# 이전 앱이 계속 동작하도록 두 값을 함께 맞춰 둔다 (api/v1/users.py).
MEAL_GOAL_BY_PRIMARY = {
    "lose_weight": "diet",
    "maintain": "maintain",
    "gain_muscle": "bulk",
    "gain_weight": "bulk",
    "eat_healthy": "maintain",
}
PRIMARY_BY_MEAL_GOAL = {"diet": "lose_weight", "maintain": "maintain", "bulk": "gain_muscle"}
# 예전 호출부·테스트가 읽는 '보통 속도' 조정값
GOAL_ADJUSTMENT = {"diet": -500, "maintain": 0, "bulk": 300}

MAX_DEFICIT_RATIO = 0.25  # 적자는 TDEE 의 25% 까지
MIN_GOAL_CALORIES = 1200
MIN_GOAL_CALORIES_MALE = 1500
DEFAULT_AGE = 30  # 출생연도 미입력 시 가정
KCAL_PER_KG = 7700  # 체중 1kg 변화에 해당하는 열량 (관행 환산값)

# Mifflin-St Jeor 성별 상수. 성별 미입력 시 남/여 중간값을 쓴다.
_GENDER_CONSTANT = {"male": 5.0, "female": -161.0}
_GENDER_CONSTANT_UNKNOWN = -78.0


def resolve_primary_goal(primary_goal: str | None, meal_goal: str | None = None) -> str:
    """계산에 쓸 목표. primary_goal 이 없으면 예전 meal_goal 을 대응시키고, 둘 다 없으면 유지.

    meal_goal 자리에 세분화한 목표 값이 와도 그대로 받는다 (호출부가 둘 중 있는 값을 넘긴다).
    """
    if primary_goal in PRIMARY_GOALS:
        return primary_goal
    if meal_goal in PRIMARY_GOALS:
        return meal_goal
    return PRIMARY_BY_MEAL_GOAL.get(meal_goal or "", "maintain")


def goal_plan(
    gender: str | None,
    birth_year: int | None,
    height: float | None,
    weight: float | None,
    meal_goal: str | None = None,
    *,
    primary_goal: str | None = None,
    activity_level: str | None = None,
    goal_pace: str | None = None,
    target_weight: float | None = None,
) -> dict | None:
    """목표 칼로리와 그 근거(BMR·유지 열량·조정·예상 주간 변화). 키/몸무게 없으면 None."""
    if height is None or weight is None:
        return None
    if birth_year:
        age = max(now_utc().astimezone(KST).year - birth_year, 1)
    else:
        age = DEFAULT_AGE
    gender_constant = _GENDER_CONSTANT.get(gender or "", _GENDER_CONSTANT_UNKNOWN)
    bmr = 10.0 * float(weight) + 6.25 * float(height) - 5.0 * age + gender_constant
    tdee = bmr * ACTIVITY_FACTORS.get(activity_level or "", ACTIVITY_FACTOR)

    goal = resolve_primary_goal(primary_goal, meal_goal)
    paces = PACE_ADJUSTMENT.get(goal)
    adjustment = paces.get(goal_pace or "", paces[DEFAULT_GOAL_PACE]) if paces else 0
    if adjustment < 0:
        adjustment = max(adjustment, -tdee * MAX_DEFICIT_RATIO)

    floor = MIN_GOAL_CALORIES_MALE if gender == "male" else MIN_GOAL_CALORIES
    # 10 kcal 단위 반올림 — 추정식에 1 kcal 정밀도는 무의미하다
    computed = round((tdee + adjustment) / 10) * 10
    calories = max(floor, computed)

    # 예상 변화는 실제로 적용된 목표(하한·상한 반영) 기준으로 알려 준다
    applied = calories - tdee if paces else 0
    weekly_change = round(applied * 7 / KCAL_PER_KG, 2)
    weeks_to_target = None
    if target_weight is not None and weekly_change:
        diff = float(target_weight) - float(weight)
        if diff and (diff > 0) == (weekly_change > 0):
            weeks_to_target = math.ceil(abs(diff) / abs(weekly_change))
    return {
        "calories": calories,
        "bmr": round(bmr),
        "tdee": round(tdee),
        "adjustment": round(applied),
        "weekly_change_kg": weekly_change,
        "weeks_to_target": weeks_to_target,
        "floor_applied": computed < floor,
    }


def calculate_goal_calories(
    gender: str | None,
    birth_year: int | None,
    height: float | None,
    weight: float | None,
    meal_goal: str | None = None,
    *,
    primary_goal: str | None = None,
    activity_level: str | None = None,
    goal_pace: str | None = None,
) -> int | None:
    """BMR/TDEE 기반 일일 목표 칼로리. 키/몸무게 없으면 None."""
    plan = goal_plan(
        gender,
        birth_year,
        height,
        weight,
        meal_goal,
        primary_goal=primary_goal,
        activity_level=activity_level,
        goal_pace=goal_pace,
    )
    return plan["calories"] if plan else None


def personalized_goals(
    gender: str | None,
    birth_year: int | None,
    height: float | None,
    weight: float | None,
    meal_goal: str | None = None,
    *,
    primary_goal: str | None = None,
    activity_level: str | None = None,
    goal_pace: str | None = None,
) -> dict[str, int] | None:
    """목표 칼로리 + 탄단지 목표(g). 신체정보 부족 시 None."""
    calories = calculate_goal_calories(
        gender,
        birth_year,
        height,
        weight,
        meal_goal,
        primary_goal=primary_goal,
        activity_level=activity_level,
        goal_pace=goal_pace,
    )
    if calories is None:
        return None
    return {"calories": calories, **derive_macro_goals(calories, weight, primary_goal or meal_goal)}
