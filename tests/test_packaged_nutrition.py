"""Package calories use the package mass basis, preserving the public meal contract."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai_client import get_ai_client
from app.ai_client.base import AICallLogPayload, AICandidate, AnalyzeResult
from app.core.config import get_settings
from app.main import app
from app.models import FoodCandidate
from app.services.analyze import resolve_nutrition


CASES = json.loads((Path(__file__).resolve().parents[1]
                   / "experiments/packaged_nutrition_20261025/cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_package_contract_cases(case, monkeypatch):
    monkeypatch.setattr(get_settings(), "count_half_steps", case.get("half_steps", False))
    candidate = AICandidate(food_name="합성 검증 음식", confidence=0.9, **case["candidate"])
    matched = SimpleNamespace(**case["matched"]) if "matched" in case else None
    result = resolve_nutrition(candidate, matched)

    assert result.nutrition is not None
    assert result.nutrition.calories * result.estimated_serving == pytest.approx(case["expected_total_kcal"])
    assert result.grams == case["expected_grams"]
    assert result.source == case["expected_source"]


@pytest.mark.parametrize("count", [0.5, 1.5, 2.5])
@pytest.mark.parametrize("unit", ["개", "캔"])
def test_half_step_compatibility_preserves_mass_and_total_nutrients(count, unit, monkeypatch):
    candidate = AICandidate(
        food_name="합성 검증 음식", confidence=0.9, count=count, count_unit=unit,
        estimated_serving_g=count * 100,
        package={"size_g": 100, "printed_kcal": 400},
        nutrition_per_100g={"calories": 400, "carbs": 50, "protein": 10, "fat": 15},
    )
    monkeypatch.setattr(get_settings(), "count_half_steps", False)
    legacy = resolve_nutrition(candidate, None)
    monkeypatch.setattr(get_settings(), "count_half_steps", True)
    native = resolve_nutrition(candidate, None)

    assert legacy.count == max(1, round(count))
    assert native.count == count
    assert legacy.grams == native.grams == count * 100
    # 각 응답의 1단위 값은 소수점 한 자리로 반올림된다.
    rounding_bound = 0.05 * (legacy.estimated_serving + native.estimated_serving) + 1e-9
    for nutrient in ("calories", "carbs", "protein", "fat"):
        assert getattr(legacy.nutrition, nutrient) * legacy.estimated_serving == pytest.approx(
            getattr(native.nutrition, nutrient) * native.estimated_serving, abs=rounding_bound
        )


@pytest.mark.parametrize("printed_kcal", [-1, float("inf"), float("nan")])
def test_invalid_printed_energy_uses_density_fallback(printed_kcal):
    candidate = AICandidate(
        food_name="합성 검증 음식", confidence=0.9, count=1, count_unit="개",
        estimated_serving_g=50, package={"size_g": 50, "printed_kcal": printed_kcal},
        nutrition_per_100g={"calories": 200, "carbs": 20, "protein": 10, "fat": 8},
    )
    result = resolve_nutrition(candidate, None)
    assert result.source == "ai"
    assert result.nutrition.calories == 100


def test_packaged_calories_flow_through_analysis_and_meal_save(client, auth_headers, db_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "count_half_steps", False)
    selected = [CASES[index] for index in (0, 3, 7)]

    class PackageAI:
        def analyze(self, *args, **kwargs):
            return AnalyzeResult(
                status="success",
                candidates=[AICandidate(
                    food_name=f"합성검증음식{index}", confidence=0.9, food_index=index,
                    **case["candidate"],
                ) for index, case in enumerate(selected)],
                ai_call_log=AICallLogPayload(task_type="analyze", status="success"),
            )

    monkeypatch.setitem(app.dependency_overrides, get_ai_client, lambda: PackageAI())
    image = client.post(
        "/v1/meals/images", headers=auth_headers,
        files={"image": ("synthetic.jpg", b"\xff\xd8\xff", "image/jpeg")},
        data={"source": "camera"},
    ).json()
    response = client.post("/v1/meals/analyze", headers=auth_headers,
                           json={"meal_image_id": image["meal_image_id"]})
    assert response.status_code == 200, response.text
    body = response.json()
    items = []
    with db_factory() as db:
        for candidate, expected in zip(body["candidates"], selected, strict=True):
            assert candidate["nutrition"]["calories"] * candidate["estimated_serving"] == expected["expected_total_kcal"]
            assert candidate["nutrition_source"] == "printed"
            row = db.get(FoodCandidate, candidate["food_candidate_id"])
            assert float(row.estimated_grams) == expected["expected_grams"]
            items.append({
                "food_name": candidate["normalized_name"],
                "food_candidate_id": candidate["food_candidate_id"],
                "serving_amount": candidate["estimated_serving"],
                "estimated_serving": candidate["estimated_serving"],
                "quantity": candidate["quantity"], "quantity_unit": candidate["quantity_unit"],
                # 식단 저장 API 는 FE 가 수량을 적용한 영양 합계를 받는다.
                **{key: candidate["nutrition"][key] * candidate["estimated_serving"]
                   for key in ("calories", "carbs", "protein", "fat")},
            })
    saved = client.post("/v1/meals", headers=auth_headers, json={
        "meal_type": "lunch", "eaten_at": "2026-10-25T12:00:00+09:00",
        "meal_image_id": image["meal_image_id"], "ai_call_log_id": body["ai_call_log_id"],
        "entry_method": "photo", "items": items,
    })
    assert saved.status_code == 201, saved.text
    assert saved.json()["total_calories"] == sum(case["expected_total_kcal"] for case in selected)
