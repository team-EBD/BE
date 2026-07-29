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


# --- 자주 먹은 음식 ---

class FrequentFoodItem(FoodSearchItem):
    count: int  # 최근 기간 내 기록 횟수


class FrequentFoodsResponse(BaseModel):
    items: list[FrequentFoodItem]


# --- 즐겨찾기 ---

class FavoriteFoodCreate(BaseModel):
    nutrition_item_id: int | None = None
    food_name: str = Field(min_length=1, max_length=100)
    base_serving: str = Field(default="1인분", max_length=50)
    calories: float = Field(ge=0)
    carbs: float = Field(ge=0)
    protein: float = Field(ge=0)
    fat: float = Field(ge=0)


class FavoriteFoodItem(BaseModel):
    favorite_id: int
    nutrition_item_id: int | None
    food_name: str
    base_serving: str
    calories: float
    carbs: float
    protein: float
    fat: float


class FavoriteFoodsResponse(BaseModel):
    items: list[FavoriteFoodItem]
