"""RealAIClient — X-Internal-Token 헤더 전송(opt-in)과 HTTP 에러 응답 처리."""
from __future__ import annotations

import httpx

from app.ai_client.real import RealAIClient


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "status": "failed", "reason": "provider_error", "ai_call_log": {}
        }
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


def _capture_post(calls: list, response: _FakeResponse | None = None):
    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return response or _FakeResponse()

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


def test_http_401_maps_to_provider_error(monkeypatch, caplog):
    """토큰 불일치(401)는 provider_error 다 — invalid_response 로 뭉개지면 안 된다.

    2026-08-01: AI 서버가 401 을 줘도 본문이 JSON({"detail": ...})이라 스키마
    검증 실패로 처리돼, 로그만 보고는 인증 문제인 줄 알 수 없었다.
    """
    unauthorized = _FakeResponse(401, {"detail": "유효하지 않거나 누락된 X-Internal-Token 입니다."})
    monkeypatch.setattr(httpx, "post", _capture_post([], unauthorized))

    result = RealAIClient("http://ai", timeout=1.0).recommend(
        {"total_calories": 0}, "convenience_store", "lunch"
    )

    assert result.status == "failed"
    assert result.reason == "provider_error"
    assert "401" in caplog.text  # 상태 코드가 로그에 드러나야 한다


def test_http_500_on_analyze_maps_to_provider_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", _capture_post([], _FakeResponse(500, {"detail": "boom"})))

    result = RealAIClient("http://ai", timeout=1.0).analyze("http://img/1.jpg")

    assert (result.status, result.reason) == ("failed", "provider_error")
    assert result.fallback_action == "manual_food_search"


def test_invalid_response_keeps_measured_latency(monkeypatch):
    """계약 불일치라도 응답은 왔으므로 측정한 소요 시간을 남긴다."""
    monkeypatch.setattr(httpx, "post", _capture_post([], _FakeResponse(200, {"nope": 1})))

    result = RealAIClient("http://ai", timeout=1.0).recommend(
        {"total_calories": 0}, "convenience_store", "lunch"
    )

    assert result.reason == "invalid_response"
    assert result.ai_call_log.latency_ms >= 0
