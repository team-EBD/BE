"""AI 서버 응답 계약 (아키텍처 §8, ai-server 구현과 1:1).

성공/실패 모두 ai_call_log 를 포함하며, BE 는 이를 그대로 ai_call_logs 에 기록한다.
"""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError


class AICallLogPayload(BaseModel):
    provider: str = "google"
    model_name: str = "unknown"
    task_type: str
    status: str  # success/failed
    latency_ms: int = 0


class AINutritionEstimate(BaseModel):
    """AI 서버(LLM)가 추정한 1인분 기준 영양값.

    영양 DB 매칭 실패 시 기록 초안의 fallback 으로 사용한다.
    """

    base_serving: str = "1인분"
    calories: float
    carbs: float
    protein: float
    fat: float


class AICandidate(BaseModel):
    food_name: str
    confidence: float
    estimated_serving: float = 1.0
    nutrition: AINutritionEstimate | None = None


class AnalyzeResult(BaseModel):
    status: Literal["success", "failed"]
    draft_notice: str | None = None
    candidates: list[AICandidate] = []
    reason: str | None = None  # ai_timeout/invalid_response/provider_error/not_food
    fallback_action: str | None = None
    ai_call_log: AICallLogPayload


class AIRecommendation(BaseModel):
    name: str
    category: str
    estimated_calories: float
    reason: str


class RecommendResult(BaseModel):
    status: Literal["success", "failed"]
    recommendations: list[AIRecommendation] = []
    caution_text: str | None = None
    reason: str | None = None
    ai_call_log: AICallLogPayload


class AIClient(Protocol):
    def analyze(self, image_url: str, eating_habits: dict | None = None) -> AnalyzeResult:
        ...

    def recommend(
        self, daily_summary: dict, preferred_category: str, meal_timing: str
    ) -> RecommendResult:
        ...


def failed_analyze(reason: str, latency_ms: int = 0) -> AnalyzeResult:
    """통신 오류/timeout/검증 실패를 실패 계약으로 변환."""
    return AnalyzeResult(
        status="failed",
        reason=reason,
        fallback_action="manual_food_search",
        ai_call_log=AICallLogPayload(
            task_type="analyze", status="failed", latency_ms=latency_ms
        ),
    )


def failed_recommend(reason: str, latency_ms: int = 0) -> RecommendResult:
    return RecommendResult(
        status="failed",
        reason=reason,
        ai_call_log=AICallLogPayload(
            task_type="recommend", status="failed", latency_ms=latency_ms
        ),
    )


def parse_analyze(data: dict) -> AnalyzeResult:
    """AI 서버 응답 JSON 스키마 검증. 실패 시 invalid_response 계약으로 변환."""
    try:
        return AnalyzeResult.model_validate(data)
    except ValidationError:
        return failed_analyze("invalid_response")


def parse_recommend(data: dict) -> RecommendResult:
    try:
        return RecommendResult.model_validate(data)
    except ValidationError:
        return failed_recommend("invalid_response")
