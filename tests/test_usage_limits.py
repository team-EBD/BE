"""일일 AI 사용량 제한 — 분석/추천 각각 한도 초과 시 429."""
from __future__ import annotations

import pytest

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.core.config import settings
from app.main import app
from tests.test_images import upload


def upload_image_id(client, headers) -> int:
    return upload(client, headers).json()["meal_image_id"]


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(settings, "analyze_daily_limit", 2)
    monkeypatch.setattr(settings, "recommend_daily_limit", 2)


def test_analyze_daily_limit_429(client, auth_headers, small_limits):
    for _ in range(2):
        image_id = upload_image_id(client, auth_headers)
        res = client.post(
            "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
        )
        assert res.status_code == 200

    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 429
    body = res.json()["error"]
    assert body["code"] == "TOO_MANY_REQUESTS"
    assert body["details"][0]["reason"] == "daily_limit_exceeded"
    assert body["details"][0]["limit"] == 2


def test_failed_analyze_does_not_consume_quota(client, auth_headers, small_limits):
    class FailingAIClient:
        def analyze(self, image_url, eating_habits=None):
            return failed_analyze("ai_timeout", latency_ms=15000)

        def recommend(self, *args, **kwargs):
            raise AssertionError("not used")

    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient()
    try:
        for _ in range(3):  # 한도(2) 를 넘는 실패 시도 — 차감되지 않아야 한다
            image_id = upload_image_id(client, auth_headers)
            res = client.post(
                "/v1/meals/analyze",
                headers=auth_headers,
                json={"meal_image_id": image_id},
            )
            assert res.status_code == 200
            assert res.json()["status"] == "failed"
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    # mock(성공) 클라이언트로 복귀해도 여전히 호출 가능해야 한다
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 200


def test_recommend_daily_limit_429(client, auth_headers, small_limits):
    for _ in range(2):
        res = client.post(
            "/v1/recommendations/next-meal",
            headers=auth_headers,
            json={"date": "2026-06-27"},
        )
        assert res.status_code == 200

    res = client.post(
        "/v1/recommendations/next-meal",
        headers=auth_headers,
        json={"date": "2026-06-27"},
    )
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "TOO_MANY_REQUESTS"


def test_analyze_and_recommend_limits_are_independent(client, auth_headers, small_limits):
    # 추천 한도를 소진해도 분석은 가능
    for _ in range(2):
        client.post(
            "/v1/recommendations/next-meal",
            headers=auth_headers,
            json={"date": "2026-06-27"},
        )
    image_id = upload_image_id(client, auth_headers)
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
    )
    assert res.status_code == 200


def test_limit_zero_means_unlimited(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_daily_limit", 0)
    for _ in range(3):
        res = client.post(
            "/v1/recommendations/next-meal",
            headers=auth_headers,
            json={"date": "2026-06-27"},
        )
        assert res.status_code == 200
