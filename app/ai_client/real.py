"""실제 AI 서버 연동 (httpx).

- timeout → ai_timeout, 통신 오류/HTTP 에러 응답 → provider_error,
  JSON/스키마 오류 → invalid_response.
- 어떤 경우에도 예외를 밖으로 던지지 않고 실패 계약(AnalyzeResult/RecommendResult)으로
  변환한다. 분기 판단은 상위(서비스/라우터)가 status 로 한다.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.ai_client.base import (
    AnalyzeResult,
    RecommendResult,
    failed_analyze,
    failed_recommend,
    parse_analyze,
    parse_recommend,
)

logger = logging.getLogger("eatlog.ai_client")


class RealAIClient:
    def __init__(self, base_url: str, timeout: float, internal_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # AI 서버 내부 인증 (opt-in): 양쪽 서버에 같은 INTERNAL_TOKEN 을 설정하면
        # X-Internal-Token 헤더로 검증된다. 미설정 시 헤더를 보내지 않는다(기존 동작).
        self._headers = {"X-Internal-Token": internal_token} if internal_token else None

    def _post(self, path: str, payload: dict) -> tuple[dict | None, str | None, int]:
        """(json, error_reason, latency_ms)"""
        started = time.monotonic()
        try:
            res = httpx.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers,
                timeout=self.timeout,
            )
            latency = int((time.monotonic() - started) * 1000)
            if res.status_code >= 400:
                # AI 서버가 에러 응답을 줘도 본문은 JSON({"detail": ...})이라
                # 상태 코드를 안 보면 스키마 검증에서 invalid_response 로 뭉개진다.
                # 원인을 바로 알 수 있게 상태 코드와 본문 앞부분을 남긴다
                # (2026-08-01: INTERNAL_TOKEN 불일치 401 이 invalid_response 로 보였다).
                logger.warning(
                    "AI 서버 HTTP %d %s — %s",
                    res.status_code, path, res.text[:200],
                )
                return None, "provider_error", latency
            return res.json(), None, latency
        except httpx.TimeoutException:
            return None, "ai_timeout", int((time.monotonic() - started) * 1000)
        except (httpx.HTTPError, ValueError):
            return None, "provider_error", int((time.monotonic() - started) * 1000)

    def analyze(
        self,
        image_url: str,
        eating_habits: dict | None = None,
        user_text: str | None = None,
    ) -> AnalyzeResult:
        payload: dict = {"image_url": image_url}
        if eating_habits:
            payload["user_eating_habits"] = eating_habits
        if user_text:
            payload["user_text"] = user_text
        data, error, latency = self._post("/internal/analyze", payload)
        if error:
            return failed_analyze(error, latency)
        return parse_analyze(data or {}, latency)

    def parse_text(
        self, text: str, db_candidates: list[dict] | None = None
    ) -> AnalyzeResult:
        payload: dict = {"text": text}
        if db_candidates:
            payload["db_candidates"] = db_candidates
        data, error, latency = self._post("/internal/parse-meal", payload)
        if error:
            return failed_analyze(error, latency)
        return parse_analyze(data or {}, latency)

    def recommend(
        self,
        daily_summary: dict,
        preferred_category: str,
        meal_timing: str,
        user_history_context: dict | None = None,
        current_time: str | None = None,
    ) -> RecommendResult:
        payload = {
            "daily_summary": daily_summary,
            "preferred_category": preferred_category,
            "meal_timing": meal_timing,
        }
        if user_history_context:
            payload["user_history_context"] = user_history_context
        if current_time:
            payload["current_time"] = current_time  # reason 이 시간대를 고려하게 한다
        data, error, latency = self._post("/internal/recommend", payload)
        if error:
            return failed_recommend(error, latency)
        return parse_recommend(data or {}, latency)
