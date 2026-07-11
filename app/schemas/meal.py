"""이미지·AI 분석·식단 스키마 (명세서 5·6·8장)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import KSTDateTime

CorrectionType = Literal["half", "large", "no_soup", "no_sauce", "custom"]
MealType = Literal["breakfast", "lunch", "dinner", "snack"]


# --- 이미지 업로드 (5.1) ---

class MealImageResponse(BaseModel):
    meal_image_id: int
    image_url: str
    storage_key: str
    uploaded_at: KSTDateTime


# --- AI 분석 (6.1) ---

class AnalyzeRequest(BaseModel):
    meal_image_id: int


class CandidateNutrition(BaseModel):
    base_serving: str
    calories: float
    carbs: float
    protein: float
    fat: float


class HabitAdjusted(BaseModel):
    applied_factor: float
    applied_corrections: list[str]
    calories: float


class AnalyzeCandidate(BaseModel):
    food_candidate_id: int
    nutrition_item_id: int | None = None  # 매칭된 영양 DB 항목 (식단 저장 시 참조)
    normalized_name: str
    confidence_score: float
    estimated_serving: float
    nutrition: CandidateNutrition | None = None
    habit_adjusted: HabitAdjusted | None = None


class AnalyzeSuccessResponse(BaseModel):
    draft_notice: str
    candidates: list[AnalyzeCandidate]
    ai_call_log_id: int


class AnalyzeFailedResponse(BaseModel):
    status: Literal["failed"] = "failed"
    reason: str
    fallback_action: str = "manual_food_search"
    ai_call_log_id: int


# --- 식단 저장/수정 (8.1, 8.3) ---

class NutritionSnapshot(BaseModel):
    calories: float
    carbs: float
    protein: float
    fat: float


class MealItemInput(BaseModel):
    nutrition_item_id: int | None = None
    food_name: str = Field(min_length=1, max_length=100)
    serving_amount: float = Field(default=1.0, gt=0)
    correction_type: CorrectionType | None = None
    calories: float = Field(ge=0)
    carbs: float = Field(ge=0)
    protein: float = Field(ge=0)
    fat: float = Field(ge=0)
    before_data: NutritionSnapshot | None = None


class MealCreateRequest(BaseModel):
    meal_type: MealType
    eaten_at: datetime
    meal_image_id: int | None = None
    memo: str | None = Field(default=None, max_length=500)
    items: list[MealItemInput] = Field(min_length=1)


class MealUpdateRequest(BaseModel):
    meal_type: MealType | None = None
    eaten_at: datetime | None = None
    memo: str | None = Field(default=None, max_length=500)
    items: list[MealItemInput] | None = Field(default=None, min_length=1)


# --- 식단 응답 (8.1~8.6) ---

class MealItemBrief(BaseModel):
    meal_item_id: int
    food_name: str
    calories: float


class MealCreateResponse(BaseModel):
    meal_id: int
    meal_type: str
    eaten_at: KSTDateTime
    total_calories: float
    total_carbs: float
    total_protein: float
    total_fat: float
    items: list[MealItemBrief]


class MealItemDetail(BaseModel):
    meal_item_id: int
    food_name: str
    serving_amount: float
    correction_type: str | None
    calories: float
    carbs: float
    protein: float
    fat: float


class MealDetailResponse(BaseModel):
    meal_id: int
    meal_type: str
    eaten_at: KSTDateTime
    memo: str | None
    image_url: str | None
    total_calories: float
    total_carbs: float
    total_protein: float
    total_fat: float
    items: list[MealItemDetail]


class MealDeleteResponse(BaseModel):
    deleted: bool = True
    meal_id: int


class MealListEntry(BaseModel):
    meal_id: int
    meal_type: str
    eaten_at: KSTDateTime
    image_url: str | None
    total_calories: float


class MealListResponse(BaseModel):
    date: str
    meals: list[MealListEntry]


class CalendarDay(BaseModel):
    date: str
    meal_count: int
    total_calories: float


class CalendarResponse(BaseModel):
    month: str
    days: list[CalendarDay]
