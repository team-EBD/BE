"""추천 스키마 (명세서 10장)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.food import FoodSearchItem

Category = Literal["convenience_store", "delivery", "home_meal", "eating_out"]


class NextMealRequest(BaseModel):
    date: date
    preferred_category: Category | None = None
    meal_timing: Literal["lunch", "dinner"] | None = None


class RecommendationItem(BaseModel):
    name: str
    category: str
    estimated_calories: float
    reason: str


class NextMealResponse(BaseModel):
    recommendations: list[RecommendationItem]
    caution_text: str
    ai_call_log_id: int


MealType = Literal["breakfast", "lunch", "dinner", "snack"]
Mood = Literal["any", "light", "hearty"]


class MenuRequest(BaseModel):
    surface: Literal["recommendation", "home"] = "recommendation"
    # 생략하면 서버가 KST 시각으로 끼니를 정한다 (legacy 는 breakfast/lunch/dinner 로만 AI 에 전달)
    meal_type: MealType | None = None
    preferred_category: Category | None = None
    remaining_calories: int | None = Field(default=None, ge=0)
    household_type: Literal["single", "multi", "none"] | None = None
    location_enabled: bool | None = None
    # v2 전용 — 예산을 가볍게(×0.8)/든든하게(×1.2) 조정. legacy 는 무시한다
    mood: Mood = "any"


class MenuItem(BaseModel):
    name: str
    category: str
    estimated_calories: float  # 메인 1인분
    exceed_flag: bool  # 오늘 남은 칼로리를 넘는가 (v2 는 동반 합산 기준)
    reason: str
    # --- v2(엔진) 전용. legacy 응답에서는 전부 None ---
    source: str | None = None  # personal | popular | similar | collaborative | catalog
    budget_label: str | None = None  # fit | light | heavy — 끼니 예산 대비
    total_calories: float | None = None  # 메인 + 동반(밥) 합산
    companion_name: str | None = None  # "쌀밥" — 함께 먹는 것으로 보고 예산을 계산했다
    companion_calories: float | None = None
    group_id: int | None = None
    group_name: str | None = None
    family: str | None = None
    recommendation_item_id: int | None = None  # 화면 노출·피드백·실제 식사 연결용 카드 id
    # 기록 초안 프리필용 상품 — 검색 결과와 같은 모양. FE 는 이 둘을 장바구니에 담고 보정 화면으로 간다.
    # 못 찾으면 None(FE 는 이름으로 검색 화면 폴백)
    food: FoodSearchItem | None = None
    companion_food: FoodSearchItem | None = None


class MenuBudget(BaseModel):
    """v2 가 이번 끼니에 잡은 예산 — FE 가 '점심 예산 700kcal 중' 같은 문구를 만들 재료."""

    meal_type: MealType
    meal_budget: int
    remaining_today: int
    goal_calories: int
    ratio_source: str  # personal | default — 개인 끼니 비율을 썼는지
    protein_gap: float  # 오늘 단백질 목표 대비 부족분(g). 음수면 이미 충분


class MenuResponse(BaseModel):
    recommended_menus: list[MenuItem]
    alternative_menus: list[MenuItem]
    caution_text: str
    ai_call_log_id: int | None = None  # legacy 만. v2 는 AI 를 부르지 않는다
    engine: Literal["legacy", "v2"] = "legacy"
    # v2: 생성 로그 id. 새 FE는 카드별 recommendation_item_id로 실제 노출·피드백을 전송한다.
    recommendation_log_id: int | None = None
    budget: MenuBudget | None = None


class AcceptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)  # 탭한 카드의 name 그대로


class AcceptResponse(BaseModel):
    accepted: bool


class ItemFeedbackRequest(BaseModel):
    action: Literal["impression", "accept", "reject"]
    reason: Literal["not_now", "dislike"] | None = None

    @model_validator(mode="after")
    def _reason_for_rejection(self) -> "ItemFeedbackRequest":
        if (self.action == "reject") != (self.reason is not None):
            raise ValueError("reason 은 reject 동작에서만 필수입니다.")
        return self


class ItemFeedbackResponse(BaseModel):
    recorded: bool


class LocationMenuRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: Category | None = None
    remaining_calories: int | None = Field(default=None, ge=0)
    meal_timing: Literal["lunch", "dinner"] | None = None


class LocationMenuResponse(BaseModel):
    nearby_recommendations: list[RecommendationItem]
    caution_text: str
    ai_call_log_id: int
