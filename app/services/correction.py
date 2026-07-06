"""보정 계수 정책 (명세서 8.1, 서버·클라 공유).

| correction_type | 계수 |
| half            | ×0.5 |
| large           | ×1.5 |
| no_soup         | ×0.7 |
| no_sauce        | ×0.85 |
| custom          | 기준값 × serving_amount |

식습관(eating_habits) → AI 분석 기본 보정(habit_adjusted, FR-HABIT-002):
- default_portion small → half / large → large
- soup_preference leave → no_soup
- sauce_preference leave → no_sauce
계수는 곱으로 합성한다.
"""
from __future__ import annotations

from app.models import EatingHabit

FACTORS: dict[str, float] = {
    "half": 0.5,
    "large": 1.5,
    "no_soup": 0.7,
    "no_sauce": 0.85,
}

NUTRIENT_KEYS = ("calories", "carbs", "protein", "fat")


def apply_correction(
    base: dict[str, float], correction_type: str | None, serving_amount: float = 1.0
) -> dict[str, float]:
    """기준 영양값에 보정 계수를 적용한 값을 반환한다."""
    if correction_type == "custom" or correction_type is None:
        factor = serving_amount
    else:
        factor = FACTORS.get(correction_type, 1.0) * serving_amount
    return {k: round(float(base[k]) * factor, 2) for k in NUTRIENT_KEYS if k in base}


def habit_factor(habit: EatingHabit | None) -> tuple[float, list[str]]:
    """식습관 설정 → (합성 계수, 적용된 보정 목록). 설정 없으면 (1.0, [])."""
    if habit is None:
        return 1.0, []
    applied: list[str] = []
    if habit.default_portion == "small":
        applied.append("half")
    elif habit.default_portion == "large":
        applied.append("large")
    if habit.soup_preference == "leave":
        applied.append("no_soup")
    if habit.sauce_preference == "leave":
        applied.append("no_sauce")

    factor = 1.0
    for c in applied:
        factor *= FACTORS[c]
    return round(factor, 4), applied
