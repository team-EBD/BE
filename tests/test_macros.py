"""탄단지 v2 계약: 수식, 경계, 판정 순서 및 기존 래퍼 호환."""
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.macros import (
    CARB_MIN_G,
    FAT_ENERGY_RATIO,
    K_BY_ACTIVITY,
    K_PROTEIN,
    PROTEIN_MAX_PER_KG,
    MacroPlan,
    macro_plan,
    normalize_goal,
    reference_weight,
)
from app.services.summary import derive_macro_goals


@pytest.fixture(autouse=True)
def fixed_year(monkeypatch):
    monkeypatch.setattr("app.services.macros.now_utc", lambda: datetime(2026, 10, 21, tzinfo=UTC))


def test_public_constants():
    assert K_PROTEIN == {
        "lose_weight": 1.6, "gain_muscle": 1.6, "gain_weight": 1.2,
        "maintain": 1.0, "eat_healthy": 1.0,
    }
    assert K_BY_ACTIVITY == {
        "sedentary": 1.0, "light": 1.0, "moderate": 1.2, "active": 1.4, "very_active": 1.6,
    }
    assert (PROTEIN_MAX_PER_KG, FAT_ENERGY_RATIO, CARB_MIN_G) == (2.2, 0.25, 130)


def test_reference_weight_continuous_at_bmi_30():
    assert reference_weight(86, 170) == 86
    assert reference_weight(86.7, 170) == pytest.approx(86.7)
    assert reference_weight(87, 170) == pytest.approx(86.775)
    assert reference_weight(87, 170) - reference_weight(86, 170) == pytest.approx(0.775)
    assert reference_weight(86.700001, 170) - reference_weight(86.699999, 170) < 0.000002
    assert reference_weight(110, None) == 110
    assert reference_weight(Decimal("110"), Decimal("170")) == pytest.approx(92.525)


@pytest.mark.parametrize("goal,normalized", [
    ("diet", "lose_weight"), ("bulk", "gain_muscle"), ("maintain", "maintain"),
    ("lose_weight", "lose_weight"), ("gain_muscle", "gain_muscle"),
    ("gain_weight", "gain_weight"), ("eat_healthy", "eat_healthy"),
    (None, "maintain"), ("unknown", "maintain"),
])
def test_normalize_goal(goal, normalized):
    assert normalize_goal(goal) == normalized


@pytest.mark.parametrize("goal,protein", [
    ("lose_weight", 112), ("diet", 112), ("gain_muscle", 112), ("bulk", 112),
    ("gain_weight", 84), ("maintain", 70), ("eat_healthy", 70), (None, 70), ("unknown", 70),
])
def test_base_coefficients(goal, protein):
    assert macro_plan(2000, weight=70, goal=goal).protein == protein


@pytest.mark.parametrize("goal,expected", [
    ("maintain", [70, 70, 84, 98, 112, 70, 70]),
    ("eat_healthy", [70, 70, 84, 98, 112, 70, 70]),
    ("gain_weight", [84, 84, 84, 98, 112, 84, 84]),
    ("lose_weight", [112] * 7), ("gain_muscle", [112] * 7),
])
def test_activity_floors_only_for_eligible_goals(goal, expected):
    levels = ["sedentary", "light", "moderate", "active", "very_active", None, "unknown"]
    assert [macro_plan(2000, weight=70, goal=goal, activity_level=a).protein for a in levels] == expected


def test_age_65_floor_and_real_weight_minimum():
    assert macro_plan(2000, weight=70, birth_year=1962).protein == 70
    assert macro_plan(2000, weight=70, birth_year=1961).protein == 84
    assert macro_plan(2000, weight=70, birth_year=1961, goal="lose_weight").protein == 112
    assert macro_plan(2000, weight=70, birth_year=1961, activity_level="active").protein == 98
    young = macro_plan(2000, weight=200, height=170, birth_year=1962)
    older = macro_plan(2000, weight=200, height=170, birth_year=1961)
    assert young.reference_weight == older.reference_weight == 115.0
    assert young.protein == 115
    assert older.protein == 200  # 1.2 × 보정체중보다 실체중 최소가 크다


def test_age_uses_kst_year_at_utc_year_boundary(monkeypatch):
    monkeypatch.setattr("app.services.macros.now_utc", lambda: datetime(2025, 12, 31, 15, tzinfo=UTC))
    assert macro_plan(2000, weight=70, birth_year=1961).protein == 84
    assert "under_19" not in macro_plan(2000, weight=70, birth_year=2007).reasons
    assert macro_plan(2000, weight=70, birth_year=2008).reasons == ["under_19"]


@pytest.mark.parametrize("calories,weight,status,reasons", [
    (2000, 70, "ok", []),
    (500, 100, "infeasible", ["protein_fat_exceed_calories", "protein_over_40pct"]),
    (800, 150.01, "infeasible", ["protein_fat_exceed_calories", "protein_over_40pct"]),
    (800, 150, "review", ["carbs_under_100", "protein_over_40pct"]),  # C = 0
    (1000, 87.6, "review", ["carbs_under_100", "protein_over_35pct"]),  # C=99.9 → 100g
    (1000, 87.5, "info", ["carbs_under_130"]),  # C=100, P=35%
    (1000, 57.6, "info", ["carbs_under_130"]),  # C=129.9 → 130g
    (1000, 57.5, "ok", []),  # C=130
    (2000, 200.01, "review", ["protein_over_40pct"]),
    (2000, 200, "info", ["protein_over_35pct"]),
    (2000, 175.01, "info", ["protein_over_35pct"]),
    (2000, 175, "ok", []),
    (2000, 39.99, "review", ["protein_under_8pct", "carbs_over_65pct"]),
    (2000, 40, "info", ["protein_under_10pct", "carbs_over_65pct"]),
    (2000, 49.99, "info", ["protein_under_10pct", "carbs_over_65pct"]),
    (2000, 50, "ok", []),  # P=10%, C=65%
    (10000, 70, "review", ["protein_under_8pct", "carbs_over_65pct"]),
])
def test_ratio_and_carbs_boundaries_use_unrounded_values(calories, weight, status, reasons):
    plan = macro_plan(calories, weight=weight)
    assert plan.status == status
    assert plan.reasons == reasons


def test_unsupported_has_priority_and_keeps_numbers_and_all_reasons():
    plan = macro_plan(500, weight=50, height=180, birth_year=2008, goal="diet", tdee=2000)
    assert plan == MacroPlan(
        carbs=14, protein=80, fat=14, status="unsupported",
        reasons=["under_19", "underweight_lose_weight", "carbs_under_100",
                 "protein_over_40pct", "deficit_over_25pct"],
        min_calories=1120, reference_weight=50.0,
    )
    infeasible = macro_plan(500, weight=80, birth_year=2008, goal="diet", tdee=2000)
    assert infeasible.status == "unsupported"
    assert infeasible.reasons == [
        "under_19", "protein_fat_exceed_calories", "protein_over_40pct", "deficit_over_25pct",
    ]
    assert (infeasible.carbs, infeasible.protein, infeasible.fat) == (0, 128, 14)


def test_underweight_boundary_and_goal_scope():
    assert macro_plan(2000, weight=74, height=200, goal="diet").status == "ok"  # BMI=18.5
    assert macro_plan(2000, weight=73.99, height=200, goal="diet").reasons == ["underweight_lose_weight"]
    assert macro_plan(2000, weight=73.99, height=200, goal="maintain").status == "ok"
    assert macro_plan(2000, weight=50, goal="diet").status == "ok"  # 키 미상


@pytest.mark.parametrize("goal,calories,code", [
    ("lose_weight", 1492, None), ("lose_weight", 1488, "deficit_over_25pct"),
    ("gain_muscle", 2408, None), ("gain_muscle", 2412, "surplus_over_20pct"),
    ("gain_weight", 2408, None), ("gain_weight", 2412, "surplus_over_20pct"),
    ("maintain", 2408, None), ("maintain", 2412, "far_from_tdee"),
    ("eat_healthy", 1592, None), ("eat_healthy", 1588, "far_from_tdee"),
    ("lose_weight", 2500, None), ("gain_muscle", 1400, None),
])
def test_tdee_thresholds_are_rounded_to_two_places(goal, calories, code):
    plan = macro_plan(calories, weight=70, goal=goal, tdee=2000)
    assert plan.reasons == ([code] if code else [])
    assert plan.status == ("info" if code == "far_from_tdee" else "review" if code else "ok")
    assert macro_plan(calories, weight=70, goal=goal).reasons == []


def test_reason_order_is_table_order_not_severity_order():
    plan = macro_plan(1000, weight=85, goal="maintain", tdee=2000)
    assert plan.reasons == ["carbs_under_130", "far_from_tdee"]
    plan = macro_plan(1000, weight=55, goal="diet", tdee=2000)
    assert plan.reasons == ["carbs_under_100", "protein_over_35pct", "deficit_over_25pct"]
    assert plan.status == "review"
    plan = macro_plan(1000, weight=52, goal="diet", tdee=2000)
    assert plan.reasons == ["carbs_under_130", "deficit_over_25pct"]
    assert plan.status == "review"


def test_min_calories_and_reference_weight_round_only_at_output():
    plan = macro_plan(2000, weight=87, height=170, goal="diet")
    assert plan.reference_weight == 86.8
    assert plan.protein == 139  # 실제 계산 기준체중은 86.775
    assert plan.min_calories == 1440
    plan = macro_plan(2000, weight=86.56, height=170, goal="diet")
    assert plan.reference_weight == 86.6
    assert plan.protein == 138  # 표시 기준체중 86.6으로 계산하면 139g이 된다
    for weight in (20, 55, 87, 110, 300):
        ref = reference_weight(weight, 170)
        plan = macro_plan(2000, weight=weight, height=170, goal="diet")
        assert plan.min_calories % 10 == 0
        assert plan.min_calories * 0.75 / 4 - 1.6 * ref >= 130
        assert (plan.min_calories - 10) * 0.75 / 4 - 1.6 * ref < 130


def test_energy_sum_and_extremes_across_body_sizes_and_goals():
    for calories in (500, 800, 1200, 1750, 2000, 3500, 10000):
        for height in (None, 100, 170, 250):
            for weight in (20, 55, 86, 87, 110, 300):
                for goal, k in K_PROTEIN.items():
                    plan = macro_plan(calories, weight=weight, height=height, goal=goal)
                    raw_protein = k * reference_weight(weight, height)
                    raw_carbs = calories * 0.75 / 4 - raw_protein
                    assert plan.protein == round(raw_protein)
                    assert plan.fat == round(calories * 0.25 / 9)
                    assert plan.carbs == round(max(raw_carbs, 0))
                    if raw_carbs >= 0:
                        assert abs(4 * plan.carbs + 4 * plan.protein + 9 * plan.fat - calories) <= 10
                    else:
                        assert plan.carbs == 0
                        assert "protein_fat_exceed_calories" in plan.reasons
                        assert not any(r.startswith("carbs_") for r in plan.reasons)


def test_fallback_wins_over_other_missing_body_diagnostics_and_is_frozen():
    plan = macro_plan(2000, weight=None, height=170, birth_year=2008, goal="diet", tdee=3000)
    assert plan == MacroPlan(250, 150, 44, "fallback", ["no_body_info"], None, None)
    with pytest.raises(FrozenInstanceError):
        plan.protein = 1


def test_legacy_wrapper_accepts_new_keywords_and_returns_only_macros():
    assert derive_macro_goals(
        2000, 70, "maintain", height=175, birth_year=1961, activity_level="active", tdee=2000,
    ) == {"carbs": 277, "protein": 98, "fat": 56}
