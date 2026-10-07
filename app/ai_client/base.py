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


class AIBoundingBox(BaseModel):
    """사진 속 음식 위치 (이미지 좌상단 기준 정규화 좌표 0.0~1.0).

    FE 가 사진 확대 보기에서 음식 이름을 그 위치에 오버레이하는 데 쓴다.
    """

    x: float
    y: float
    width: float
    height: float


class AINutritionPer100g(BaseModel):
    """100g(ml) 당 영양값 — AI 추정 또는 표시 성분 검색 결과."""

    calories: float
    carbs: float
    protein: float
    fat: float


class AIPackageInfo(BaseModel):
    """포장 글자에서 읽은 제품 정보 (포장 제품일 때만)."""

    brand: str | None = None
    product_name: str | None = None
    variant: str | None = None
    size_text: str | None = None
    label_text: str | None = None
    size_g: float | None = None
    printed_kcal: float | None = None  # 포장에 인쇄된 총 열량 ("9g(45 kcal)" → 45)


class AILabelInfo(BaseModel):
    """검색 그라운딩으로 찾은 표시 영양성분."""

    product_name: str
    per_100g: AINutritionPer100g
    package_size_g: float | None = None
    sources: list[str] = []
    confidence: float = 0.0


class AICandidate(BaseModel):
    # 사진 속 몇 번째 음식에 대한 예측인지 (0부터). 같은 food_index 후보들은
    # "같은 음식에 대한 대체 예측"이다. 구버전 AI 서버 응답에는 없으므로 기본 0.
    food_index: int = 0
    food_name: str
    confidence: float
    estimated_serving: float = 1.0
    # 사진에 담긴 **절대량**(g/ml). AI 가 생각하는 1인분과 영양DB 의 1인분이 다르면
    # 배수(estimated_serving)만으로는 계산이 어긋나므로(피자 1판 vs 1조각) 절대량을
    # 받아 우리 기준으로 다시 나눈다. 구버전 AI 서버·추정 실패 시 None.
    estimated_serving_g: float | None = None
    # 낱개로 셀 수 있는 음식의 사진 속 개수와 단위(개·조각·장·줄). 그릇·접시 음식과 구버전 AI 응답은 None.
    # 개수 음식은 g ÷ 영양DB 1인분 g 으로 배수를 내고 화면엔 개수를 보여 준다 (AI 의 1인분 개념에 기대지 않는다).
    count: float | None = None  # 0.5 단위 (반 개)
    count_unit: str | None = None
    # 국물/소스가 실제로 있는 음식인지 — FE 보정 버튼 노출 판단용.
    # 구버전 AI 서버 응답에는 없으므로 True(버튼 노출 유지) 기본값.
    has_soup: bool = True
    has_sauce: bool = True
    # 사진 속 위치. 구버전 AI 서버·좌표 판별 실패 시 None.
    bbox: AIBoundingBox | None = None
    nutrition: AINutritionEstimate | None = None
    # 100g 당 영양(AI 추정), 포장 글자, 표시 성분 검색 결과 — 2026-10 이후 AI 서버. 구버전은 None
    nutrition_per_100g: AINutritionPer100g | None = None
    package: AIPackageInfo | None = None
    label: AILabelInfo | None = None


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
    def analyze(
        self,
        image_url: str,
        eating_habits: dict | None = None,
        user_text: str | None = None,
        candidate_depth: str | None = None,
    ) -> AnalyzeResult:
        """`/internal/analyze` 의 candidate_depth 계약: "standard" | "clarifier".

        "clarifier" 면 AI 서버가 음식당 대체 후보를 1개 더 주고
        `ai_call_log.task_type` 을 "analyze_clarifier" 로 구분해 준다.
        **"clarifier" 일 때만** 넘어온다 — 구버전 AI 서버가 모르는 필드를
        기본 요청에 섞지 않기 위한 규약이다.
        """
        ...

    def parse_text(
        self, text: str, db_candidates: list[dict] | None = None
    ) -> AnalyzeResult:
        """자연어 식사 서술("김밥 한 줄이랑 라면 반 개") → 후보. 계약은 analyze 와 동일.

        db_candidates: 문장에서 선(先)-매칭한 영양 DB 후보 [{name, base_serving}] —
        AI 가 음식명·수량 기준을 DB 에 정렬하는 데 쓴다. 없으면 자유 추출.
        """
        ...

    def recommend(
        self,
        daily_summary: dict,
        preferred_category: str,
        meal_timing: str,
        user_history_context: dict | None = None,
        current_time: str | None = None,
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


def parse_analyze(data: dict, latency_ms: int = 0) -> AnalyzeResult:
    """AI 서버 응답 JSON 스키마 검증. 실패 시 invalid_response 계약으로 변환.

    latency_ms 는 검증 실패 시에만 쓴다 (성공 응답은 AI 서버가 준 값을 그대로 둔다).
    실패에도 실제 소요 시간을 남겨야 '응답이 왔는데 계약 불일치'인지 구분된다.
    """
    try:
        return AnalyzeResult.model_validate(data)
    except ValidationError:
        return failed_analyze("invalid_response", latency_ms)


def parse_recommend(data: dict, latency_ms: int = 0) -> RecommendResult:
    try:
        return RecommendResult.model_validate(data)
    except ValidationError:
        return failed_recommend("invalid_response", latency_ms)
