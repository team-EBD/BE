"""GET /v1/usage/daily — 일일 사용량/한도/잔여 조회."""
from __future__ import annotations

import pytest

from app.core.config import settings
from tests.test_images import upload


def upload_image_id(client, headers) -> int:
    return upload(client, headers).json()["meal_image_id"]


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(settings, "analyze_daily_limit", 3)
    monkeypatch.setattr(settings, "recommend_daily_limit", 5)


def test_daily_usage_initial(client, auth_headers, small_limits):
    res = client.get("/v1/usage/daily", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["analyze"] == {"limit": 3, "used": 0, "remaining": 3}
    assert body["recommend"] == {"limit": 5, "used": 0, "remaining": 5}


def test_daily_usage_counts_success_calls(client, auth_headers, small_limits):
    image_id = upload_image_id(client, auth_headers)
    client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27"},
    )
    body = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert body["analyze"]["used"] == 1
    assert body["analyze"]["remaining"] == 2
    assert body["recommend"]["used"] == 1
    assert body["recommend"]["remaining"] == 4


def test_daily_usage_unlimited_when_limit_zero(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "analyze_daily_limit", 0)
    body = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert body["analyze"]["limit"] is None
    assert body["analyze"]["remaining"] is None


def test_daily_usage_requires_auth(client):
    res = client.get("/v1/usage/daily")
    assert res.status_code == 401
