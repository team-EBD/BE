"""추천 스키마 (명세서 10장)."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

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


class MenuRequest(BaseModel):
    meal_type: Literal["lunch", "dinner"]
    preferred_category: Category | None = None
    remaining_calories: int | None = Field(default=None, ge=0)
    household_type: Literal["single", "multi", "none"] | None = None
    location_enabled: bool | None = None


class MenuItem(BaseModel):
    name: str
    category: str
    estimated_calories: float
    exceed_flag: bool
    reason: str


class MenuResponse(BaseModel):
    recommended_menus: list[MenuItem]
    alternative_menus: list[MenuItem]
    caution_text: str
    ai_call_log_id: int


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
