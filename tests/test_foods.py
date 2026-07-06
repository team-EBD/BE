"""Phase 5 DoD — 음식 검색."""
from __future__ import annotations


def test_search_kimchi(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "김치"}
    )
    assert res.status_code == 200
    body = res.json()
    names = [item["name"] for item in body["items"]]
    assert "김치찌개" in names
    assert body["pagination"]["page"] == 1
    first = body["items"][0]
    assert {"nutrition_item_id", "name", "base_serving", "calories"} <= set(first)


def test_search_with_space_normalization(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "김치 찌개"}
    )
    assert res.status_code == 200
    assert any(i["name"] == "김치찌개" for i in res.json()["items"])


def test_search_no_result_empty_200(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "존재하지않는음식xyz"}
    )
    assert res.status_code == 200
    assert res.json()["items"] == []
    assert res.json()["pagination"]["total"] == 0


def test_search_missing_query_400(client, auth_headers):
    res = client.post("/v1/foods/search", headers=auth_headers, json={})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_pagination(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "찌개", "size": 2}
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) <= 2
    assert body["pagination"]["size"] == 2
