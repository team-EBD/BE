"""RealAIClient — X-Internal-Token 헤더 전송 (opt-in) 검증."""
from __future__ import annotations

import httpx

from app.ai_client.real import RealAIClient


class _FakeResponse:
    @staticmethod
    def json() -> dict:
        return {"status": "failed", "reason": "provider_error", "ai_call_log": {}}


def _capture_post(calls: list):
    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return _FakeResponse()

    return fake_post


def test_internal_token_header_sent_when_configured(monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", _capture_post(calls))

    client = RealAIClient("http://ai", timeout=1.0, internal_token="secret-token")
    client.analyze("http://img/1.jpg")

    assert calls[0]["headers"] == {"X-Internal-Token": "secret-token"}


def test_no_header_when_token_empty(monkeypatch):
    """토큰 미설정이면 헤더를 보내지 않는다 — 기존 동작과 동일해야 한다."""
    calls: list = []
    monkeypatch.setattr(httpx, "post", _capture_post(calls))

    client = RealAIClient("http://ai", timeout=1.0)
    client.parse_text("김치찌개 먹었어")
    client.recommend({"total_calories": 0}, "convenience_store", "lunch")

    assert calls[0]["headers"] is None
    assert calls[1]["headers"] is None
