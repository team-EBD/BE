"""Mock AI 클라이언트 (개발/테스트용 고정 응답).

AI 서버 없이도 분석/추천 경로 전체(E2E)가 돌게 한다. 응답 형태는
실제 AI 서버 계약과 동일하다.
"""
from __future__ import annotations

from app.ai_client.base import (
    AIBoundingBox,
    AICallLogPayload,
    AICandidate,
    AINutritionEstimate,
    AIRecommendation,
    AnalyzeResult,
    RecommendResult,
)

MOCK_MODEL = "mock-model"

# 음식별 위치 (같은 음식의 대체 예측끼리는 같은 좌표)
_STEW_BOX = AIBoundingBox(x=0.04, y=0.12, width=0.44, height=0.5)
_RICE_BOX = AIBoundingBox(x=0.52, y=0.43, width=0.38, height=0.35)


class MockAIClient:
    def analyze(
        self,
        image_url: str,
        eating_habits: dict | None = None,
        user_text: str | None = None,
    ) -> AnalyzeResult:
        return AnalyzeResult(
            status="success",
            draft_notice="AI가 분석한 기록 초안입니다.",
            candidates=[
                # 음식 0: 찌개류 대체 예측 3개, 음식 1: 공기밥 (여러 음식 그룹핑 검증용)
                AICandidate(food_index=0, food_name="김치찌개", confidence=0.87, estimated_serving=1.0, has_soup=True, has_sauce=False, bbox=_STEW_BOX),
                AICandidate(food_index=0, food_name="된장찌개", confidence=0.08, estimated_serving=1.0, has_soup=True, has_sauce=False, bbox=_STEW_BOX),
                AICandidate(food_index=0, food_name="순두부찌개", confidence=0.05, estimated_serving=1.0, has_soup=True, has_sauce=False, bbox=_STEW_BOX),
                AICandidate(food_index=1, food_name="공기밥", confidence=0.95, estimated_serving=1.0, has_soup=False, has_sauce=False, bbox=_RICE_BOX),
            ],
            ai_call_log=AICallLogPayload(
                provider="google",
                model_name=MOCK_MODEL,
                task_type="analyze",
                status="success",
                latency_ms=42,
            ),
        )

    def parse_text(self, text: str) -> AnalyzeResult:
        # 문장 파싱은 음식당 예측 1개, bbox 없음 (실 AI 서버 계약과 동일 형태)
        return AnalyzeResult(
            status="success",
            draft_notice="문장에서 추출한 기록 초안입니다.",
            candidates=[
                AICandidate(
                    food_index=0, food_name="김밥", confidence=0.95,
                    estimated_serving=1.0, has_soup=False, has_sauce=False,
                    nutrition=AINutritionEstimate(base_serving="1줄(230g)", calories=320, carbs=55, protein=9, fat=7),
                ),
                AICandidate(
                    food_index=1, food_name="라면", confidence=0.95,
                    estimated_serving=0.5, has_soup=True, has_sauce=False,
                    nutrition=AINutritionEstimate(base_serving="1개(120g, 조리)", calories=500, carbs=78, protein=10, fat=16),
                ),
            ],
            ai_call_log=AICallLogPayload(
                provider="google",
                model_name=MOCK_MODEL,
                task_type="analyze",
                status="success",
                latency_ms=42,
            ),
        )

    def recommend(
        self,
        daily_summary: dict,
        preferred_category: str,
        meal_timing: str,
        user_history_context: dict | None = None,
        current_time: str | None = None,
    ) -> RecommendResult:
        return RecommendResult(
            status="success",
            recommendations=[
                AIRecommendation(
                    name="닭가슴살 샐러드",
                    category=preferred_category,
                    estimated_calories=320,
                    reason="오늘 부족한 단백질을 보충하기 좋아요.",
                ),
                AIRecommendation(
                    name="연어 포케",
                    category=preferred_category,
                    estimated_calories=450,
                    reason="가볍지만 포만감 있는 한 끼예요.",
                ),
                AIRecommendation(
                    name="두부 유부초밥",
                    category=preferred_category,
                    estimated_calories=380,
                    reason="남은 칼로리 안에서 즐길 수 있어요.",
                ),
            ],
            caution_text="추천은 생활 식단 참고용이며 의학적 조언이 아닙니다.",
            ai_call_log=AICallLogPayload(
                provider="google",
                model_name=MOCK_MODEL,
                task_type="recommend",
                status="success",
                latency_ms=42,
            ),
        )
