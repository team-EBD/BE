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
