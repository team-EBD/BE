"""음식 검색 라우터 (Phase 5, 명세서 7장) + 자주 먹은 음식·즐겨찾기."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select

from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.pagination import PageParams, Pagination
from app.core.timeutil import now_utc
from app.models import FavoriteFood, MealItem, MealRecord, NutritionItem
from app.schemas.food import (
    FavoriteFoodCreate,
    FavoriteFoodItem,
    FavoriteFoodsResponse,
    FoodSearchItem,
    FoodSearchRequest,
    FoodSearchResponse,
    FrequentFoodItem,
    FrequentFoodsResponse,
)
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


@router.get("/frequent", response_model=FrequentFoodsResponse)
def frequent_foods(
    user: CurrentUser,
    db: DB,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=8, ge=1, le=30),
) -> FrequentFoodsResponse:
    """최근 N일간 자주 기록한 음식 (기록 횟수순).

    영양값은 기록 시점 스냅샷(보정 반영값)이 아니라 영양 DB 원본을 쓰므로,
    매칭된(nutrition_item_id 있는) 항목만 집계한다.
    """
    cutoff = now_utc() - timedelta(days=days)
    count = func.count(MealItem.id)
    rows = db.execute(
        select(NutritionItem, count)
        .join(MealItem, MealItem.nutrition_item_id == NutritionItem.id)
        .join(MealRecord, MealItem.meal_record_id == MealRecord.id)
        .where(
            MealRecord.user_id == user.id,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= cutoff,
        )
        .group_by(NutritionItem.id)
        .order_by(count.desc(), func.max(MealRecord.eaten_at).desc())
        .limit(limit)
    ).all()
    return FrequentFoodsResponse(
        items=[
            FrequentFoodItem(
                nutrition_item_id=item.id,
                name=item.name,
                base_serving=base_serving_text(item),
                calories=float(item.calories),
                carbs=float(item.carbs),
                protein=float(item.protein),
                fat=float(item.fat),
                count=int(cnt),
            )
            for item, cnt in rows
        ]
    )


def _favorite_out(fav: FavoriteFood) -> FavoriteFoodItem:
    return FavoriteFoodItem(
        favorite_id=fav.id,
        nutrition_item_id=fav.nutrition_item_id,
        food_name=fav.food_name,
        base_serving=fav.base_serving,
        calories=float(fav.calories),
        carbs=float(fav.carbs),
        protein=float(fav.protein),
        fat=float(fav.fat),
    )


@router.get("/favorites", response_model=FavoriteFoodsResponse)
def list_favorites(user: CurrentUser, db: DB) -> FavoriteFoodsResponse:
    favorites = db.scalars(
        select(FavoriteFood)
        .where(FavoriteFood.user_id == user.id)
        .order_by(FavoriteFood.id.desc())
    ).all()
    return FavoriteFoodsResponse(items=[_favorite_out(f) for f in favorites])


@router.post("/favorites", response_model=FavoriteFoodItem, status_code=201)
def add_favorite(
    body: FavoriteFoodCreate, user: CurrentUser, db: DB, response: Response
) -> FavoriteFoodItem:
    """즐겨찾기 등록 — 같은 이름이 이미 있으면 기존 항목을 반환한다 (200)."""
    existing = db.scalar(
        select(FavoriteFood).where(
            FavoriteFood.user_id == user.id,
            FavoriteFood.food_name == body.food_name,
        )
    )
    if existing is not None:
        response.status_code = 200
        return _favorite_out(existing)

    fav = FavoriteFood(
        user_id=user.id,
        nutrition_item_id=body.nutrition_item_id,
        food_name=body.food_name,
        base_serving=body.base_serving,
        calories=body.calories,
        carbs=body.carbs,
        protein=body.protein,
        fat=body.fat,
    )
    db.add(fav)
    db.commit()
    return _favorite_out(fav)


@router.delete("/favorites/{favorite_id}", status_code=204, response_class=Response)
def remove_favorite(favorite_id: int, user: CurrentUser, db: DB) -> Response:
    fav = db.get(FavoriteFood, favorite_id)
    if fav is None or fav.user_id != user.id:
        raise APIError(404, "NOT_FOUND", "즐겨찾기를 찾을 수 없습니다.")
    db.delete(fav)
    db.commit()
    return Response(status_code=204)
