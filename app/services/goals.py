"""사용자별 목표 칼로리 산정 (BMR/TDEE 기반).

Mifflin-St Jeor 공식 (1990, 현재 임상에서 가장 널리 쓰이는 안정시대사량 추정식):
    BMR = 10*체중(kg) + 6.25*키(cm) - 5*나이 (+5 남성 / -161 여성)
TDEE = BMR × 활동계수. 활동량은 아직 수집하지 않으므로
'가벼운 활동(주 1~3회 운동)' 1.375 를 기본 가정으로 쓴다.

목표 유형(eating_habits.meal_goal)별 조정:
    diet(감량) -500 kcal / maintain(유지) 0 / bulk(증량) +300 kcal
안전 하한 1,200 kcal (극단적 입력 방어).

키/몸무게가 없으면 None 을 반환한다 — 호출부는 기본 목표(2000)를 쓴다.
"""
from __future__ import annotations

from app.core.timeutil import KST, now_utc
from app.services.summary import derive_macro_goals

ACTIVITY_FACTOR = 1.375  # 가벼운 활동 (활동량 미수집 시 기본 가정)
GOAL_ADJUSTMENT = {"diet": -500, "maintain": 0, "bulk": 300}
MIN_GOAL_CALORIES = 1200
DEFAULT_AGE = 30  # 출생연도 미입력 시 가정

# Mifflin-St Jeor 성별 상수. 성별 미입력 시 남/여 중간값을 쓴다.
_GENDER_CONSTANT = {"male": 5.0, "female": -161.0}
_GENDER_CONSTANT_UNKNOWN = -78.0


def calculate_goal_calories(
    gender: str | None,
    birth_year: int | None,
    height: float | None,
    weight: float | None,
    meal_goal: str | None = None,
) -> int | None:
    """BMR/TDEE 기반 일일 목표 칼로리. 키/몸무게 없으면 None."""
    if height is None or weight is None:
        return None
    if birth_year:
        age = max(now_utc().astimezone(KST).year - birth_year, 1)
    else:
        age = DEFAULT_AGE
    gender_constant = _GENDER_CONSTANT.get(gender or "", _GENDER_CONSTANT_UNKNOWN)
    bmr = 10.0 * float(weight) + 6.25 * float(height) - 5.0 * age + gender_constant
    tdee = bmr * ACTIVITY_FACTOR
    goal = tdee + GOAL_ADJUSTMENT.get(meal_goal or "maintain", 0)
    # 10 kcal 단위 반올림 — 추정식에 1 kcal 정밀도는 무의미하다
    return max(MIN_GOAL_CALORIES, round(goal / 10) * 10)


def personalized_goals(
    gender: str | None,
    birth_year: int | None,
    height: float | None,
    weight: float | None,
    meal_goal: str | None = None,
) -> dict[str, int] | None:
    """목표 칼로리 + 탄단지 목표(g). 신체정보 부족 시 None."""
    calories = calculate_goal_calories(gender, birth_year, height, weight, meal_goal)
    if calories is None:
        return None
    return {"calories": calories, **derive_macro_goals(calories, weight, meal_goal)}
