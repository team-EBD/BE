"""음식 검색 라우터 (Phase 5, 명세서 7장)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import DB, CurrentUser
from app.core.pagination import PageParams, Pagination
from app.schemas.food import FoodSearchItem, FoodSearchRequest, FoodSearchResponse
from app.services.matching import base_serving_text, search_items

router = APIRouter(prefix="/foods", tags=["foods"])


@router.post("/search", response_model=FoodSearchResponse)
def search_foods(body: FoodSearchRequest, user: CurrentUser, db: DB) -> FoodSearchResponse:
    params = PageParams(page=body.page, size=body.size)
    items, total = search_items(db, body.query, params)
    return FoodSearchResponse(
        items=[
            FoodSearchItem(
                nutrition_item_id=item.id,
                name=item.name,
                base_serving=base_serving_text(item),
                calories=float(item.calories),
                carbs=float(item.carbs),
                protein=float(item.protein),
                fat=float(item.fat),
            )
            for item in items
        ],
        pagination=Pagination.build(params, total),
    )
