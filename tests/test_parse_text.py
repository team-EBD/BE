"""POST /meals/parse-text — 자연어 식사 서술 → 기록 초안 (analyze 계약 공유)."""
from __future__ import annotations

from sqlalchemy import select

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.ai_client.mock import MockAIClient
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


# ------------------------------- 선(先)-매칭: 문장 → DB 후보 → AI 전달


def test_db_candidates_found_in_sentence(client, auth_headers, db_factory):
    """조사가 붙어도("김치찌개랑") 이름 포함 검사로 후보를 찾는다."""
    from app.services.matching import db_candidates_for_text

    with db_factory() as db:
        rows = db_candidates_for_text(db, "김치찌개랑 공기밥 먹었어")
        names = [r.name for r in rows]
        assert "김치찌개" in names
        assert "공기밥" in names
        assert all(r.is_representative for r in rows)


def test_db_candidates_specific_name_first(client, auth_headers, db_factory):
    """더 구체적인(긴) 이름이 목록 앞에 온다 — AI 가 구체명을 우선 보게."""
    from app.services.matching import db_candidates_for_text

    with db_factory() as db:
        rows = db_candidates_for_text(db, "김치찌개")
        assert rows, "김치찌개 시드 항목을 찾아야 함"
        lengths = [len(r.name.replace(" ", "")) for r in rows]
        assert lengths == sorted(lengths, reverse=True)


def test_db_candidates_empty_for_non_food(client, auth_headers, db_factory):
    from app.services.matching import db_candidates_for_text

    with db_factory() as db:
        assert db_candidates_for_text(db, "오늘 날씨 참 좋다") == []


def test_parse_passes_db_candidates_to_ai(client, auth_headers):
    """문장에서 찾은 후보가 이름+기준량 형태로 AI 클라이언트에 전달된다."""

    class RecordingParseClient(MockAIClient):
        def __init__(self):
            self.seen = "NOT_CALLED"

        def parse_text(self, text, db_candidates=None):
            self.seen = db_candidates
            return super().parse_text(text, db_candidates)

    recorder = RecordingParseClient()
    app.dependency_overrides[get_ai_client] = lambda: recorder
    try:
        parse(client, auth_headers, "김치찌개 한 그릇")
        assert isinstance(recorder.seen, list) and recorder.seen
        names = [c["name"] for c in recorder.seen]
        assert "김치찌개" in names
        assert all("base_serving" in c and c["base_serving"] for c in recorder.seen)
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_parse_without_candidates_uses_legacy_signature(client, auth_headers):
    """후보가 없으면 구 시그니처(위치 인자만)로 호출 — 테스트 더블 호환 유지."""

    class LegacyParseClient(MockAIClient):
        def __init__(self):
            self.called = False

        def parse_text(self, text):  # db_candidates 인자 없는 구버전
            self.called = True
            return MockAIClient.parse_text(self, text)

    legacy = LegacyParseClient()
    app.dependency_overrides[get_ai_client] = lambda: legacy
    try:
        res = parse(client, auth_headers, "오늘 뭔가 특별한 우주음식")
        assert res.status_code == 200
        assert legacy.called
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
