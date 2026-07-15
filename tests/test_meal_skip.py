"""식사 생략(is_skipped) 기록 — 저장/조회/수정/요약 반영."""
from __future__ import annotations

from tests.test_meals import MEAL_PAYLOAD, create_meal

SKIP_PAYLOAD = {
    "meal_type": "breakfast",
    "eaten_at": "2026-06-27T08:00:00+09:00",
    "is_skipped": True,
    "memo": "아침 거름",
    "items": [],
}


def test_create_skipped_meal(client, auth_headers):
    res = client.post("/v1/meals", headers=auth_headers, json=SKIP_PAYLOAD)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["is_skipped"] is True
    assert body["total_calories"] == 0.0
    assert body["items"] == []


def test_create_skipped_meal_items_omitted(client, auth_headers):
    payload = {k: v for k, v in SKIP_PAYLOAD.items() if k != "items"}
    res = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert res.status_code == 201, res.text


def test_create_skipped_meal_with_items_400(client, auth_headers):
    payload = {**SKIP_PAYLOAD, "items": MEAL_PAYLOAD["items"]}
    res = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_and_detail_expose_is_skipped(client, auth_headers):
    meal_id = client.post(
        "/v1/meals", headers=auth_headers, json=SKIP_PAYLOAD
    ).json()["meal_id"]
    create_meal(client, auth_headers)  # 일반 기록 (is_skipped=False)

    detail = client.get(f"/v1/meals/{meal_id}", headers=auth_headers).json()
    assert detail["is_skipped"] is True

    listing = client.get("/v1/meals?date=2026-06-27", headers=auth_headers).json()
    flags = {m["meal_id"]: m["is_skipped"] for m in listing["meals"]}
    assert flags[meal_id] is True
    assert False in flags.values()


def test_update_to_skipped_clears_items(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}", headers=auth_headers, json={"is_skipped": True}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_skipped"] is True
    assert body["items"] == []
    assert body["total_calories"] == 0.0


def test_unskip_requires_items(client, auth_headers):
    meal_id = client.post(
        "/v1/meals", headers=auth_headers, json=SKIP_PAYLOAD
    ).json()["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}", headers=auth_headers, json={"is_skipped": False}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unskip_with_items_succeeds(client, auth_headers):
    meal_id = client.post(
        "/v1/meals", headers=auth_headers, json=SKIP_PAYLOAD
    ).json()["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}",
        headers=auth_headers,
        json={"is_skipped": False, "items": MEAL_PAYLOAD["items"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_skipped"] is False
    assert body["total_calories"] == 524.0


def test_skipped_meal_excluded_from_meal_count(client, auth_headers):
    client.post("/v1/meals", headers=auth_headers, json=SKIP_PAYLOAD)
    create_meal(client, auth_headers)
    res = client.get(
        "/v1/nutrition/daily-summary?date=2026-06-27", headers=auth_headers
    )
    assert res.status_code == 200
    body = res.json()
    # 생략 기록은 합계 0 이고, 요약 문구도 '기록 없음' 이 아니어야 한다
    assert body["total"]["calories"] == 524.0
    assert "기록이 없어요" not in body["summary_text"]
