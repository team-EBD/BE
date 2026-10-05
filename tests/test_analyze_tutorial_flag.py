"""분석 요청의 is_tutorial 이 ai_call_logs 에 그대로 남는지 (사진·문장 공통).

NULL 은 '보내지 않음(구버전 앱)' 이라 false 와 구분되어야 한다 — 대시보드가
NULL 구간만 근사 규칙으로 처리한다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.ai_client.mock import MockAIClient
from app.main import app
from app.models import AiCallLog
from tests.test_images import upload

FLAG_CASES = [({"is_tutorial": True}, True), ({"is_tutorial": False}, False), ({}, None)]


def _logged_flags(db_factory) -> list[bool | None]:
    with db_factory() as db:
        return [log.is_tutorial for log in db.scalars(select(AiCallLog).order_by(AiCallLog.id))]


@pytest.mark.parametrize("extra, expected", FLAG_CASES)
def test_photo_analyze_stores_flag(client, auth_headers, db_factory, extra, expected):
    image_id = upload(client, auth_headers).json()["meal_image_id"]
    res = client.post(
        "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id, **extra}
    )
    assert res.status_code == 200
    assert _logged_flags(db_factory) == [expected]


@pytest.mark.parametrize("extra, expected", FLAG_CASES)
def test_parse_text_stores_flag(client, auth_headers, db_factory, extra, expected):
    res = client.post(
        "/v1/meals/parse-text", headers=auth_headers, json={"text": "김밥 한 줄", **extra}
    )
    assert res.status_code == 200
    assert _logged_flags(db_factory) == [expected]


def test_failed_analysis_keeps_flag(client, auth_headers, db_factory):
    """분석 실패 호출도 표시가 남아야 튜토리얼 중 실패를 따로 볼 수 있다."""

    class FailingAIClient(MockAIClient):
        def parse_text(self, text, db_candidates=None):
            return failed_analyze("no_candidates", latency_ms=1)

    app.dependency_overrides[get_ai_client] = lambda: FailingAIClient()
    try:
        res = client.post(
            "/v1/meals/parse-text",
            headers=auth_headers,
            json={"text": "김밥 한 줄", "is_tutorial": True},
        )
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
    assert res.json()["status"] == "failed"
    assert _logged_flags(db_factory) == [True]
