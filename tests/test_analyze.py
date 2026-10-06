"""Phase 8 DoD — AI 분석 (mock) + 매칭 + habit_adjusted + 실패 fallback."""
from __future__ import annotations

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.main import app
from tests.test_images import upload


class FailingAIClient:
    def __init__(self, reason: str = "ai_timeout") -> None:
        self.reason = reason

    def analyze(self, image_url, eating_habits=None):
        return failed_analyze(self.reason, latency_ms=15000)

    def recommend(self, daily_summary, preferred_category, meal_timing):
        raise AssertionError("not used")


def upload_image_id(client, headers) -> int:
    return upload(client, headers).json()["meal_image_id"]


def test_analyze_success_with_matching(client, auth_headers):
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["draft_notice"]
    # mock: 음식 0(찌개 대체 예측 3개) + 음식 1(공기밥) = 4개
    assert 1 <= len(body["candidates"]) <= 15
    top = body["candidates"][0]
    # mock 1순위 "김치찌개" → 시드 매칭 → 영양 초안 포함
    assert top["normalized_name"] == "김치찌개"
    assert top["food_index"] == 0
    assert top["nutrition"]["calories"] == 320.0
    assert top["habit_adjusted"] is None  # 식습관 미설정 시 생략
    assert body["ai_call_log_id"] > 0


def test_analyze_groups_by_food_index(client, auth_headers):
    """같은 음식의 대체 예측은 같은 food_index, 다른 음식은 다른 food_index."""
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    body = res.json()
    groups = [c["food_index"] for c in body["candidates"]]
    assert groups == [0, 0, 0, 1]  # mock: 찌개 3개 예측 + 공기밥
    # 음식당 예측 3개 초과 금지
    from collections import Counter

    assert max(Counter(groups).values()) <= 3


def test_analyze_soup_sauce_flags_passthrough(client, auth_headers):
    """AI 가 판별한 국물/소스 유무를 응답에 그대로 전달한다."""
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    body = res.json()
    by_name = {c["normalized_name"]: c for c in body["candidates"]}
    assert by_name["김치찌개"]["has_soup"] is True
    assert by_name["김치찌개"]["has_sauce"] is False
    assert by_name["공기밥"]["has_soup"] is False
    assert by_name["공기밥"]["has_sauce"] is False


def test_analyze_habit_soup_skipped_for_soupless_food(client, auth_headers):
    """국물 제외 습관이 있어도 국물 없는 음식(공기밥)엔 no_soup 을 적용하지 않는다."""
    client.patch(
        "/v1/users/eating-habits", headers=auth_headers, json={"soup_preference": "leave"}
    )
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    by_name = {c["normalized_name"]: c for c in res.json()["candidates"]}
    # 국물 있는 김치찌개에는 여전히 적용
    assert by_name["김치찌개"]["habit_adjusted"]["applied_corrections"] == ["no_soup"]
    # 국물 없는 공기밥에는 적용할 보정이 없어 habit_adjusted 자체가 생략
    assert by_name["공기밥"]["habit_adjusted"] is None


def test_analyze_habit_partial_filter_recomputes_factor(client, auth_headers):
    """소식(half)+국물 제외 습관 → 국물 없는 음식엔 half 만 남고 계수를 재계산한다."""
    client.patch(
        "/v1/users/eating-habits",
        headers=auth_headers,
        json={"default_portion": "small", "soup_preference": "leave"},
    )
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    by_name = {c["normalized_name"]: c for c in res.json()["candidates"]}
    stew = by_name["김치찌개"]["habit_adjusted"]
    assert stew["applied_corrections"] == ["half", "no_soup"]
    assert stew["applied_factor"] == 0.35  # 0.5 × 0.7
    rice = by_name["공기밥"]["habit_adjusted"]
    assert rice["applied_corrections"] == ["half"]
    assert rice["applied_factor"] == 0.5


def test_analyze_habit_adjusted(client, auth_headers):
    client.patch(
        "/v1/users/eating-habits", headers=auth_headers, json={"soup_preference": "leave"}
    )
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    top = res.json()["candidates"][0]
    assert top["habit_adjusted"]["applied_corrections"] == ["no_soup"]
    assert top["habit_adjusted"]["applied_factor"] == 0.7
    assert top["habit_adjusted"]["calories"] == 224.0  # 320 × 0.7


def test_analyze_image_not_found_404(client, auth_headers):
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": 999999}
    )
    assert res.status_code == 404


def test_analyze_failure_returns_200_fallback(client, auth_headers):
    image_id = upload_image_id(client, auth_headers)
    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient("ai_timeout")
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 200  # 실패도 200 + fallback (FR-AI-004, NFR-005)
    body = res.json()
    assert body["status"] == "failed"
    assert body["reason"] == "ai_timeout"
    assert body["fallback_action"] == "manual_food_search"
    assert body["ai_call_log_id"] > 0


def test_analyze_logs_every_call(client, auth_headers):
    image_id = upload_image_id(client, auth_headers)
    client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})

    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient()
    client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})

    res = client.get("/v1/ai-call-logs", headers=auth_headers)
    body = res.json()
    assert body["pagination"]["total"] == 2
    statuses = {item["status"] for item in body["items"]}
    assert statuses == {"success", "failed"}


class UnmatchedWithEstimateAIClient:
    """영양 DB에 없는 음식명 + LLM 영양 추정치를 반환하는 대역."""

    def analyze(self, image_url, eating_habits=None):
        from app.ai_client.base import (
            AICallLogPayload,
            AICandidate,
            AINutritionEstimate,
            AnalyzeResult,
        )

        return AnalyzeResult(
            status="success",
            draft_notice="AI가 분석한 기록 초안입니다.",
            candidates=[
                AICandidate(
                    food_name="크림새우",
                    confidence=0.95,
                    estimated_serving=1.0,
                    nutrition=AINutritionEstimate(
                        base_serving="1인분(250g)",
                        calories=520.0,
                        carbs=32.0,
                        protein=24.0,
                        fat=33.0,
                    ),
                ),
                AICandidate(food_name="양꼬치", confidence=0.85, estimated_serving=1.0),
            ],
            ai_call_log=AICallLogPayload(
                task_type="analyze", status="success", latency_ms=100
            ),
        )

    def recommend(self, daily_summary, preferred_category, meal_timing):
        raise AssertionError("not used")


def test_analyze_unmatched_food_uses_ai_nutrition_estimate(client, auth_headers):
    """DB 매칭 실패 시 LLM 추정치가 초안 영양값으로 내려간다 (미매칭+추정치도 없으면 null)."""
    image_id = upload_image_id(client, auth_headers)
    app.dependency_overrides[get_ai_client] = lambda: UnmatchedWithEstimateAIClient()
    try:
        res = client.post(
            "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
        )
    finally:
        from app.ai_client.mock import MockAIClient

        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()

    assert res.status_code == 200
    body = res.json()
    first, second = body["candidates"]

    # 시드 DB에 없는 음식 → nutrition_item_id 는 없지만 AI 추정치로 초안 생성
    assert first["normalized_name"] == "크림새우"
    assert first["nutrition_item_id"] is None
    assert first["nutrition"]["calories"] == 520.0
    assert first["nutrition"]["base_serving"] == "1인분(250g)"

    # 추정치조차 없으면 기존과 동일하게 nutrition null
    assert second["nutrition_item_id"] is None
    assert second["nutrition"] is None


def test_analyze_bbox_passthrough(client, auth_headers):
    """AI 가 준 음식 위치(bbox)를 응답에 그대로 전달한다 (같은 음식은 같은 좌표)."""
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    by_name = {c["normalized_name"]: c for c in res.json()["candidates"]}
    assert by_name["김치찌개"]["bbox"] == {
        "x": 0.04, "y": 0.12, "width": 0.44, "height": 0.5,
    }
    assert by_name["된장찌개"]["bbox"] == by_name["김치찌개"]["bbox"]
    assert by_name["공기밥"]["bbox"]["x"] == 0.52


def test_analyze_bbox_none_for_legacy_ai_server(client, auth_headers, monkeypatch):
    """bbox 를 주지 않는(구버전) AI 서버 응답도 분석은 정상 동작한다."""
    from app.ai_client.mock import MockAIClient

    original = MockAIClient.analyze

    def without_bbox(self, image_url, eating_habits=None):
        result = original(self, image_url, eating_habits)
        for cand in result.candidates:
            cand.bbox = None
        return result

    monkeypatch.setattr(MockAIClient, "analyze", without_bbox)
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 200
    assert all(c["bbox"] is None for c in res.json()["candidates"])


# --------------------------------------------------------- 절대량(g) 기준 환산
# AI 는 우리 DB 의 1인분이 몇 g 인지 모른다. 절대량이 오면 그것을 우리 기준량으로
# 나눠 배수를 다시 계산해야 한다 (피자 1판 vs 1조각처럼 몇 배씩 어긋나는 것 방지).

class _Cand:
    def __init__(self, serving=1.0, grams=None, count=None, unit=None, per_100g=None, nutrition=None, label=None, package=None):
        self.estimated_serving = serving
        self.estimated_serving_g = grams
        self.count, self.count_unit = count, unit
        self.nutrition_per_100g = per_100g
        self.nutrition = nutrition
        self.label, self.package = label, package
        self.has_soup = self.has_sauce = False


class _Item:
    def __init__(self, base_amount, calories, source="public", external_id="D1", name="행"):
        self.base_amount, self.calories = base_amount, calories
        self.carbs = self.protein = self.fat = 1.0
        self.source, self.external_id, self.name, self.id = source, external_id, name, 1


class _Per100:
    def __init__(self, calories):
        self.calories, self.carbs, self.protein, self.fat = calories, 1.0, 1.0, 1.0

    def model_dump(self):
        return {"calories": self.calories, "carbs": self.carbs, "protein": self.protein, "fat": self.fat}


class _Label:
    def __init__(self, calories_per_100, package_size_g=None, sources=()):
        self.per_100g, self.package_size_g, self.sources = _Per100(calories_per_100), package_size_g, list(sources)


class _Package:
    def __init__(self, size_g=None, printed_kcal=None):
        self.size_g, self.printed_kcal = size_g, printed_kcal


def test_resolve_printed_kcal_on_package_wins():
    """참쌀설병 낱개 '9g(45 kcal)' 이 읽히면 검색이 찾은 봉지(128g) 값 대신 인쇄 열량 그대로. 2개면 90."""
    from app.services.analyze import resolve_nutrition

    r = resolve_nutrition(_Cand(grams=9, count=1, unit="개", per_100g=_Per100(475), label=_Label(475, 128), package=_Package(9, 45)), None)
    assert r.source == "printed" and r.nutrition.calories == 45.0 and r.grams == 9 and r.estimated_serving == 1.0
    two = resolve_nutrition(_Cand(grams=18, count=2, unit="개", per_100g=_Per100(475), package=_Package(9, 45)), None)
    assert two.nutrition.calories == 45.0 and two.estimated_serving == 2.0  # 1개 값 × 개수는 FE 가 곱한다
    cup = resolve_nutrition(_Cand(grams=300, count=1, unit="잔", per_100g=_Per100(60), package=_Package(300, 180)), None)
    assert cup.source == "ai" and cup.nutrition.calories == 180.0  # 잔은 포장 단위가 아니라 밀도 경로 (300 × 0.6)


def test_resolve_bowl_dish_uses_db_density_times_grams_and_is_one_visible_serving():
    """라면 560g × (DB 500g 당 500kcal → 100kcal/100g) = 560kcal. 1인분 = 보이는 한 그릇, 스냅 없음."""
    from app.services.analyze import resolve_nutrition

    r = resolve_nutrition(_Cand(grams=560), _Item(500, 500))
    assert r.source == "db" and r.grams == 560 and r.estimated_serving == 1.0
    assert r.nutrition.calories == 560.0 and r.nutrition.base_serving == "보이는 양(560g)"
    assert r.count is None and r.grams_per_unit is None


def test_resolve_counted_food_reports_per_unit_values_and_count_as_serving():
    """피자 8조각 800g → 1조각 100g 값, estimated_serving 8 (0.5 단위 개수도 그대로)."""
    from app.services.analyze import resolve_nutrition

    r = resolve_nutrition(_Cand(grams=800, count=8, unit="조각"), _Item(200, 530))
    assert (r.count, r.count_unit, r.estimated_serving, r.grams_per_unit) == (8.0, "조각", 8.0, 100.0)
    assert r.nutrition.calories == 265.0 and r.nutrition.base_serving == "1조각(100g)"
    half = resolve_nutrition(_Cand(grams=45, count=0.5, unit="개"), _Item(90, 270))
    assert half.estimated_serving == 0.5 and half.nutrition.calories == 270.0  # 1개 90g 값, 개수 0.5


def test_resolve_unmatched_uses_ai_per_100g_and_tiny_base_rows_no_longer_inflate():
    """미매칭이면 AI 100g 당 값. 땅콩버터 5g 기준 행이어도 kcal 은 g × 밀도라 '10인분' 같은 왜곡이 없다."""
    from app.services.analyze import resolve_nutrition

    r = resolve_nutrition(_Cand(grams=120, per_100g=_Per100(150)), None)
    assert r.source == "ai" and r.nutrition.calories == 180.0 and r.estimated_serving == 1.0
    pb = resolve_nutrition(_Cand(grams=15, per_100g=_Per100(600)), _Item(5, 30.5))
    assert pb.source == "db" and pb.nutrition.calories == 91.5 and pb.estimated_serving == 1.0


def test_resolve_label_wins_and_package_size_replaces_ai_grams():
    """표시 성분 검색 결과가 있으면 그 밀도, 용량 355ml × 1캔. 오징어땅콩 1봉 98g 도 봉지 용량으로."""
    from app.services.analyze import resolve_nutrition

    r = resolve_nutrition(_Cand(grams=200, count=1, unit="캔", label=_Label(1.4, 355, ["https://a"])), _Item(100, 40))
    assert r.source == "label" and r.grams == 355 and round(r.nutrition.calories, 1) == 5.0 and r.sources == ["https://a"]
    bag = resolve_nutrition(_Cand(grams=40, count=1, unit="개", per_100g=_Per100(500), package=_Package(98)), None)
    assert bag.grams == 98 and bag.nutrition.calories == 490.0
    # 봉지 속 낱개 4개는 '봉지 4개'가 아니다 — 포장 용량은 1개(한 포장)일 때만, 아니면 AI 가 본 전체 g
    minis = resolve_nutrition(_Cand(grams=120, count=4, unit="개", label=_Label(390, 100)), None)
    assert minis.grams == 120 and round(minis.nutrition.calories * 4) == 468
    cans = resolve_nutrition(_Cand(grams=500, count=2, unit="캔", label=_Label(42, 355)), None)
    assert cans.grams == 710  # 캔·병은 개수 × 용량
    # 사진 속 포장(40g 파우치)이 검색이 찾은 묶음 포장(280g)보다 우선
    pouch = resolve_nutrition(_Cand(grams=40, count=1, unit="개", label=_Label(385, 280), package=_Package(40)), None)
    assert pouch.grams == 40 and pouch.nutrition.calories == 154.0


def test_resolve_legacy_ai_without_grams_or_per_100g_keeps_old_behavior():
    """구 AI 서버(배수 + 1인분형 추정치): 매칭 행이면 기준량 × 배수, 아니면 추정치와 배수를 그대로."""
    from app.services.analyze import resolve_nutrition

    class _N:
        base_serving, calories, carbs, protein, fat = "1인분(250g)", 520.0, 32.0, 24.0, 33.0

    r = resolve_nutrition(_Cand(serving=0.5), _Item(400, 320))
    assert r.source == "db" and r.grams == 200 and r.nutrition.calories == 160.0 and r.estimated_serving == 1.0
    legacy = resolve_nutrition(_Cand(serving=0.5, nutrition=_N()), None)
    assert legacy.source == "ai_serving" and legacy.nutrition.calories == 520.0 and legacy.estimated_serving == 0.5
    nothing = resolve_nutrition(_Cand(serving=1.0), None)
    assert nothing.source == "none" and nothing.nutrition is None


class CountedPizzaAIClient:
    """낱개 음식(피자 8조각 800g) + 그릇 음식(라면 560g) 을 돌려주는 대역."""

    def analyze(self, image_url, eating_habits=None):
        from app.ai_client.base import AICallLogPayload, AICandidate, AnalyzeResult

        return AnalyzeResult(
            status="success",
            draft_notice="AI가 분석한 기록 초안입니다.",
            candidates=[
                AICandidate(food_index=0, food_name="피자", confidence=0.9, estimated_serving=1.0,
                            estimated_serving_g=800, count=8, count_unit="조각"),
                AICandidate(food_index=1, food_name="라면", confidence=0.9, estimated_serving=1.0,
                            estimated_serving_g=560),
            ],
            ai_call_log=AICallLogPayload(task_type="analyze", status="success", latency_ms=100),
        )

    def recommend(self, *args, **kwargs):
        raise AssertionError("not used")


def test_analyze_counted_food_returns_quantity_and_unit(client, auth_headers, db_factory):
    """피자 8조각 800g: 1조각(100g) 값 × 개수 8. 라면 560g: 보이는 한 그릇 = 1인분, kcal 은 560g 치."""
    image_id = upload_image_id(client, auth_headers)
    app.dependency_overrides[get_ai_client] = lambda: CountedPizzaAIClient()
    try:
        res = client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})
    finally:
        from app.ai_client.mock import MockAIClient

        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
    assert res.status_code == 200, res.text
    by_name = {c["normalized_name"]: c for c in res.json()["candidates"]}
    pizza, ramen = by_name["피자"], by_name["라면"]

    from sqlalchemy import select

    from app.models import FoodCandidate, NutritionItem

    db = db_factory()
    pizza_row = db.scalar(select(NutritionItem).where(NutritionItem.normalized_name == "피자"))
    ramen_row = db.scalar(select(NutritionItem).where(NutritionItem.normalized_name == "라면"))
    assert pizza["estimated_serving"] == 8.0 and pizza["grams"] == 800 and pizza["nutrition_source"] == "db"
    assert (pizza["quantity"], pizza["quantity_unit"], pizza["serving_per_unit"]) == (8.0, "조각", 1.0)
    assert pizza["nutrition"]["calories"] == round(float(pizza_row.calories) / float(pizza_row.base_amount) * 100, 1)
    assert pizza["matched_name"] == pizza_row.name and pizza["per_100g"]["calories"] > 0
    assert ramen["estimated_serving"] == 1.0 and ramen["grams"] == 560 and ramen["quantity"] is None
    assert ramen["nutrition"]["calories"] == round(float(ramen_row.calories) / float(ramen_row.base_amount) * 560, 1)
    row = db.scalar(select(FoodCandidate).where(FoodCandidate.id == pizza["food_candidate_id"]))
    assert (float(row.quantity), row.quantity_unit, float(row.grams_per_unit), float(row.estimated_grams), row.nutrition_source, row.food_index) == (
        8.0, "조각", 100.0, 800.0, "db", 0)
