"""POST /meals/parse-text — 자연어 식사 서술 → 기록 초안 (analyze 계약 공유)."""
from __future__ import annotations

from sqlalchemy import select

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.main import app
from app.models import AiCallLog, FoodCandidate


def parse(client, headers, text="김밥 한 줄이랑 라면 반 개"):
    return client.post("/v1/meals/parse-text", headers=headers, json={"text": text})


def test_parse_text_success_shares_analyze_contract(client, auth_headers):
    body = parse(client, auth_headers).json()
    assert "candidates" in body and body.get("status") != "failed"
    names = [c["normalized_name"] for c in body["candidates"]]
    assert names == ["김밥", "라면"]
    # "반 개" → serving 0.5, AI 추정 영양 fallback (시드 DB에 없는 음식)
    ramen = body["candidates"][1]
    assert ramen["estimated_serving"] == 0.5
    assert ramen["nutrition"]["calories"] == 500
    assert ramen["food_candidate_id"] > 0


def test_parse_text_persists_candidates_without_image(client, auth_headers, db_factory):
    parse(client, auth_headers)
    with db_factory() as db:
        rows = list(db.scalars(select(FoodCandidate)))
        assert len(rows) == 2
        assert all(r.meal_image_id is None for r in rows)
        assert rows[0].ai_call_log_id is not None


def test_parse_text_counts_toward_analyze_quota(client, auth_headers, db_factory):
    parse(client, auth_headers)
    with db_factory() as db:
        log = db.scalars(select(AiCallLog)).first()
        assert log.task_type == "analyze"
        assert log.status == "success"
        assert log.meal_image_id is None
        assert log.total_ms is not None


def test_parse_text_failure_returns_200_fallback(client, auth_headers):
    class FailingParseClient:
        def parse_text(self, text):
            return failed_analyze("not_food", latency_ms=100)

    app.dependency_overrides[get_ai_client] = lambda: FailingParseClient()
    try:
        res = parse(client, auth_headers, "오늘 날씨 좋다")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "failed"
        assert body["reason"] == "not_food"
        assert body["fallback_action"] == "manual_food_search"
    finally:
        from app.ai_client.mock import MockAIClient

        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_parse_text_empty_rejected(client, auth_headers):
    # 검증 오류는 명세서 1.4 에러 봉투(400 VALIDATION_ERROR)로 변환된다
    assert parse(client, auth_headers, "").status_code == 400


def test_parse_text_too_long_rejected(client, auth_headers):
    assert parse(client, auth_headers, "김" * 201).status_code == 400


def test_parse_text_requires_auth(client):
    res = client.post("/v1/meals/parse-text", json={"text": "김밥"})
    assert res.status_code == 401
