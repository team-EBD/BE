"""Phase 11 DoD — 명세서 14장 핵심 루프 E2E (mock AI).

1. 소셜 로그인 → 2. 이미지 업로드 → 3. AI 분석 → 4. 식단 저장(보정 반영)
→ 5. 상세 확인 → 6. 하루 요약 → 7. 다음 식사 추천
"""
from __future__ import annotations

from tests.test_images import JPEG


def test_core_loop(client):
    # 1. 소셜 로그인 (신규 → 201 + 자동 가입)
    res = client.post(
        "/v1/auth/social/login", json={"provider": "google", "token": "e2e-user"}
    )
    assert res.status_code == 201
    headers = {"Authorization": f"Bearer {res.json()['access_token']}"}

    # (식습관 설정 — 분석 초안 보정에 반영)
    client.patch(
        "/v1/users/eating-habits", headers=headers, json={"soup_preference": "leave"}
    )

    # 2. 이미지 업로드
    res = client.post(
        "/v1/meals/images",
        headers=headers,
        files={"image": JPEG},
        data={"source": "camera"},
    )
    assert res.status_code == 201
    image_id = res.json()["meal_image_id"]

    # 3. AI 분석 → 후보 + habit_adjusted 초안
    res = client.post("/v1/meals/analyze", headers=headers, json={"meal_image_id": image_id})
    assert res.status_code == 200
    top = res.json()["candidates"][0]
    assert top["normalized_name"] == "김치찌개"
    adjusted_calories = top["habit_adjusted"]["calories"]  # 320 × 0.7 = 224

    # 4. 후보를 보정 반영해 식단 저장
    res = client.post(
        "/v1/meals",
        headers=headers,
        json={
            "meal_type": "lunch",
            "eaten_at": "2026-06-27T12:40:00+09:00",
            "meal_image_id": image_id,
            "items": [
                {
                    "nutrition_item_id": 1,
                    "food_name": "김치찌개",
                    "serving_amount": 1.0,
                    "correction_type": "no_soup",
                    "calories": adjusted_calories,
                    "carbs": 13.0,
                    "protein": 15.4,
                    "fat": 11.2,
                    "before_data": top["nutrition"],
                }
            ],
        },
    )
    assert res.status_code == 201
    meal_id = res.json()["meal_id"]
    assert res.json()["total_calories"] == 224.0

    # 5. 상세 확인 (이미지 URL·보정 타입 포함)
    res = client.get(f"/v1/meals/{meal_id}", headers=headers)
    assert res.status_code == 200
    assert res.json()["image_url"]
    assert res.json()["items"][0]["correction_type"] == "no_soup"

    # 6. 하루 요약 (재계산 반영)
    res = client.get(
        "/v1/nutrition/daily-summary", headers=headers, params={"date": "2026-06-27"}
    )
    assert res.json()["total"]["calories"] == 224.0

    # 7. 다음 식사 추천 + caution_text
    res = client.post(
        "/v1/recommendations/next-meal", headers=headers, json={"date": "2026-06-27"}
    )
    assert res.status_code == 200
    assert res.json()["caution_text"]

    # (운영) 모든 AI 호출이 로그에 남는다 — analyze 1 + recommend 1
    res = client.get("/v1/ai-call-logs", headers=headers)
    assert res.json()["pagination"]["total"] == 2
