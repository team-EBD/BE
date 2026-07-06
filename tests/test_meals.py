"""Phase 6 DoD — 식단 저장/조회/수정/삭제 + 보정 + 소유권."""
from __future__ import annotations

from tests.conftest import login

MEAL_PAYLOAD = {
    "meal_type": "lunch",
    "eaten_at": "2026-06-27T12:40:00+09:00",
    "memo": "회사 근처 식당",
    "items": [
        {
            "nutrition_item_id": 1,
            "food_name": "김치찌개",
            "serving_amount": 1.0,
            "correction_type": "no_soup",
            "calories": 224,
            "carbs": 13.0,
            "protein": 15.4,
            "fat": 11.2,
            "before_data": {"calories": 320, "carbs": 18.5, "protein": 22.0, "fat": 16.0},
        },
        {
            "nutrition_item_id": 8,
            "food_name": "공기밥",
            "serving_amount": 1.0,
            "calories": 300,
            "carbs": 68.0,
            "protein": 5.5,
            "fat": 0.6,
        },
    ],
}


def create_meal(client, headers, payload=None):
    res = client.post("/v1/meals", headers=headers, json=payload or MEAL_PAYLOAD)
    assert res.status_code == 201, res.text
    return res.json()


def test_create_meal_totals_and_items(client, auth_headers):
    body = create_meal(client, auth_headers)
    assert body["total_calories"] == 524.0
    assert body["total_carbs"] == 81.0
    assert len(body["items"]) == 2
    assert body["eaten_at"] == "2026-06-27T12:40:00+09:00"


def test_detail_includes_correction_type(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    res = client.get(f"/v1/meals/{meal_id}", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    by_name = {i["food_name"]: i for i in body["items"]}
    assert by_name["김치찌개"]["correction_type"] == "no_soup"
    assert by_name["공기밥"]["correction_type"] is None
    assert body["memo"] == "회사 근처 식당"


def test_create_meal_without_items_400(client, auth_headers):
    payload = {**MEAL_PAYLOAD, "items": []}
    res = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert res.status_code == 400


def test_update_meal_replaces_items_and_recomputes(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}",
        headers=auth_headers,
        json={
            "memo": "수정된 메모",
            "items": [
                {
                    "nutrition_item_id": 8,
                    "food_name": "공기밥",
                    "serving_amount": 0.5,
                    "correction_type": "half",
                    "calories": 150,
                    "carbs": 34.0,
                    "protein": 2.75,
                    "fat": 0.3,
                }
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["memo"] == "수정된 메모"
    assert body["total_calories"] == 150.0
    assert len(body["items"]) == 1


def test_delete_meal_soft(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    res = client.delete(f"/v1/meals/{meal_id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "meal_id": meal_id}

    res2 = client.get(f"/v1/meals/{meal_id}", headers=auth_headers)
    assert res2.status_code == 404

    # 목록에서도 제외
    res3 = client.get("/v1/meals", headers=auth_headers, params={"date": "2026-06-27"})
    assert all(m["meal_id"] != meal_id for m in res3.json()["meals"])


def test_other_users_meal_403(client, auth_headers):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    other = login(client, "intruder")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/v1/meals/{meal_id}", headers=other_headers).status_code == 403
    assert client.delete(f"/v1/meals/{meal_id}", headers=other_headers).status_code == 403


def test_meal_not_found_404(client, auth_headers):
    assert client.get("/v1/meals/999999", headers=auth_headers).status_code == 404


def test_list_by_date(client, auth_headers):
    create_meal(client, auth_headers)
    res = client.get("/v1/meals", headers=auth_headers, params={"date": "2026-06-27"})
    assert res.status_code == 200
    body = res.json()
    assert body["date"] == "2026-06-27"
    assert len(body["meals"]) == 1

    empty = client.get("/v1/meals", headers=auth_headers, params={"date": "2026-06-01"})
    assert empty.json()["meals"] == []


def test_kst_day_boundary(client, auth_headers):
    # KST 자정 직후(=UTC 전날 15:10)는 해당 KST 날짜에 잡혀야 한다
    payload = {**MEAL_PAYLOAD, "eaten_at": "2026-06-28T00:10:00+09:00"}
    create_meal(client, auth_headers, payload)
    res = client.get("/v1/meals", headers=auth_headers, params={"date": "2026-06-28"})
    assert len(res.json()["meals"]) == 1
    res2 = client.get("/v1/meals", headers=auth_headers, params={"date": "2026-06-27"})
    assert len(res2.json()["meals"]) == 0


def test_calendar(client, auth_headers):
    create_meal(client, auth_headers)
    create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "eaten_at": "2026-06-26T08:00:00+09:00"}
    )
    res = client.get("/v1/meals/calendar", headers=auth_headers, params={"month": "2026-06"})
    assert res.status_code == 200
    body = res.json()
    assert body["month"] == "2026-06"
    days = {d["date"]: d for d in body["days"]}
    assert days["2026-06-27"]["meal_count"] == 1
    assert days["2026-06-26"]["total_calories"] == 524.0


def test_calendar_bad_month_400(client, auth_headers):
    res = client.get("/v1/meals/calendar", headers=auth_headers, params={"month": "2026-13"})
    assert res.status_code == 400
