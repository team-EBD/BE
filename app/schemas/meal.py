"""이미지·AI 분석·식단 스키마 (명세서 5·6·8장)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    # 사진 속 몇 번째 음식에 대한 예측인지 (0부터). 같은 food_index 는
    # 같은 음식에 대한 대체 예측(최대 3개)이다.
    food_index: int = 0
    normalized_name: str
    confidence_score: float
    estimated_serving: float
    # 국물/소스가 실제로 있는 음식인지 (AI 판별). False 면 FE 가
    # '국물 제외/소스 제외' 보정 버튼을 감춘다. 미판별 시 True.
    has_soup: bool = True
    has_sauce: bool = True
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
    # 식사 생략(안 먹음) 기록 — true 면 items 없이 저장한다 (합계 0)
    is_skipped: bool = False
    items: list[MealItemInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_items_by_skip(self) -> "MealCreateRequest":
        if self.is_skipped and self.items:
            raise ValueError("생략한 식사에는 items 를 포함할 수 없습니다.")
        if not self.is_skipped and not self.items:
            raise ValueError("items 는 최소 1개 이상이어야 합니다.")
        return self


class MealUpdateRequest(BaseModel):
    meal_type: MealType | None = None
    eaten_at: datetime | None = None
    memo: str | None = Field(default=None, max_length=500)
    is_skipped: bool | None = None
    items: list[MealItemInput] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_items_by_skip(self) -> "MealUpdateRequest":
        if self.is_skipped is True and self.items:
            raise ValueError("생략한 식사에는 items 를 포함할 수 없습니다.")
        return self


# --- 식단 응답 (8.1~8.6) ---

class MealItemBrief(BaseModel):
    meal_item_id: int
    food_name: str
    calories: float


class MealCreateResponse(BaseModel):
    meal_id: int
    meal_type: str
    eaten_at: KSTDateTime
    is_skipped: bool
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
    is_skipped: bool
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
    is_skipped: bool
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
