"""AI 서버 클라이언트 (Phase 8) — Gemini 직접 호출 아님.

구조: BE → AI 서버(/internal/analyze·/internal/recommend) → Gemini.
- AI_CLIENT_MODE=mock 이면 고정 응답(개발/테스트), real 이면 httpx 로 AI 서버 호출.
- AI 서버는 실패해도 HTTP 200 + status:"failed" 로 반환하고, 클라이언트 구현은
  통신 오류·timeout 도 같은 실패 계약으로 변환해 상위 로직을 단일 경로로 만든다.
"""
from __future__ import annotations

from app.ai_client.base import AIClient, AnalyzeResult, RecommendResult
from app.ai_client.mock import MockAIClient
from app.ai_client.real import RealAIClient
from app.core.config import settings

__all__ = [
    "AIClient",
    "AnalyzeResult",
    "RecommendResult",
    "MockAIClient",
    "RealAIClient",
    "get_ai_client",
]

_client: AIClient | None = None


def get_ai_client() -> AIClient:
    """FastAPI 의존성. 테스트는 dependency_overrides 로 교체한다."""
    global _client
    if _client is None:
        if settings.ai_client_mode == "mock":
            _client = MockAIClient()
        else:
            _client = RealAIClient(
                base_url=settings.ai_server_base_url,
                timeout=settings.ai_request_timeout_seconds,
                internal_token=settings.internal_token,
            )
    return _client
