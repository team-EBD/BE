"""음식 검색 스키마 (명세서 7장)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.pagination import Pagination


class FoodSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class FoodSearchItem(BaseModel):
    nutrition_item_id: int
    name: str
    base_serving: str
    calories: float
    carbs: float
    protein: float
    fat: float


class FoodSearchResponse(BaseModel):
    items: list[FoodSearchItem]
    pagination: Pagination
