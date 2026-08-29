"""자주 먹은 음식(GET /foods/frequent) + 즐겨찾기(/foods/favorites) 검증."""
from __future__ import annotations

from datetime import timedelta

from app.core.timeutil import KST, now_utc


def _recent_meal_time(days_ago: int, hour: int) -> str:
    """30일 집계 테스트가 실행 날짜와 무관하게 최근 기록을 만들도록 한다."""
    return (
        now_utc()
        .astimezone(KST)
        .replace(hour=hour, minute=0, second=0, microsecond=0)
        - timedelta(days=days_ago)
    ).isoformat()


def _add_meal(client, headers, food_name, nutrition_item_id, eaten_at, **extra):
    payload = {
        "meal_type": extra.get("meal_type", "lunch"),
        "eaten_at": eaten_at,
        "items": [
            {
                "nutrition_item_id": nutrition_item_id,
                "food_name": food_name,
                "calories": 300, "carbs": 30, "protein": 20, "fat": 10,
                "serving_amount": 1,
            }
        ],
    }
    res = client.post("/v1/meals", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["meal_id"]


def _first_seed_items(client, headers, query="김치"):
    res = client.post(
        "/v1/foods/search", json={"query": query}, headers=headers
    )
    assert res.status_code == 200
    return res.json()["items"]


def test_frequent_foods_counts_and_orders(client, auth_headers):
    items = _first_seed_items(client, auth_headers)
    a, b = items[0], items[1] if len(items) > 1 else items[0]

    # a 를 2회, b 를 1회 기록
    _add_meal(client, auth_headers, a["name"], a["nutrition_item_id"], _recent_meal_time(2, 12))
    _add_meal(client, auth_headers, a["name"], a["nutrition_item_id"],
              _recent_meal_time(1, 12), meal_type="dinner")
    _add_meal(client, auth_headers, b["name"], b["nutrition_item_id"], _recent_meal_time(1, 8),
              meal_type="breakfast")

    res = client.get("/v1/foods/frequent", headers=auth_headers)
    assert res.status_code == 200
    out = res.json()["items"]
    assert out[0]["name"] == a["name"]
    assert out[0]["count"] == 2
    # 영양값은 영양 DB 원본 (기록 스냅샷 아님)
    assert out[0]["calories"] == a["calories"]


def test_frequent_excludes_deleted_meals(client, auth_headers):
    items = _first_seed_items(client, auth_headers)
    a = items[0]
    meal_id = _add_meal(client, auth_headers, a["name"], a["nutrition_item_id"],
                        _recent_meal_time(1, 12))
    client.delete(f"/v1/meals/{meal_id}", headers=auth_headers)

    res = client.get("/v1/foods/frequent", headers=auth_headers)
    assert res.json()["items"] == []


def test_favorites_crud_flow(client, auth_headers):
    # 등록
    res = client.post(
        "/v1/foods/favorites",
        json={"nutrition_item_id": None, "food_name": "수제 샐러드",
              "base_serving": "1그릇(250g)", "calories": 180,
              "carbs": 12, "protein": 8, "fat": 9},
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    fav_id = res.json()["favorite_id"]

    # 같은 이름 재등록 → 기존 반환 (200)
    res = client.post(
        "/v1/foods/favorites",
        json={"food_name": "수제 샐러드", "calories": 999,
              "carbs": 1, "protein": 1, "fat": 1},
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.json()["favorite_id"] == fav_id
    assert res.json()["calories"] == 180  # 기존 값 유지

    # 목록
    res = client.get("/v1/foods/favorites", headers=auth_headers)
    assert [f["food_name"] for f in res.json()["items"]] == ["수제 샐러드"]

    # 삭제
    assert client.delete(
        f"/v1/foods/favorites/{fav_id}", headers=auth_headers
    ).status_code == 204
    assert client.get("/v1/foods/favorites", headers=auth_headers).json()["items"] == []


def test_favorite_of_other_user_not_visible_or_deletable(client, auth_headers):
    from tests.conftest import login

    res = client.post(
        "/v1/foods/favorites",
        json={"food_name": "내 것", "calories": 100, "carbs": 1, "protein": 1, "fat": 1},
        headers=auth_headers,
    )
    fav_id = res.json()["favorite_id"]

    other = login(client, social_id="other-user")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get("/v1/foods/favorites", headers=other_headers).json()["items"] == []
    assert client.delete(
        f"/v1/foods/favorites/{fav_id}", headers=other_headers
    ).status_code == 404


def test_favorites_require_auth(client):
    assert client.get("/v1/foods/favorites").status_code == 401
    assert client.post("/v1/foods/favorites", json={}).status_code in (400, 401, 422)
