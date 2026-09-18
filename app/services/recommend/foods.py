"""추천 카드 → 기록 초안 프리필용 상품(nutrition_items) 선택.

카드는 군(또는 이름 키) 단위지만 기록 초안은 상품 1행이 필요하다. FE 는 검색 결과와 같은
모양(FoodSearchItem)으로 받아 메인·동반(밥)을 장바구니에 바로 담고 보정 화면으로 간다 —
"닭갈비 추천받고 쌀밥은 또 검색해야 하는" 불편을 없애기 위한 것. 못 찾으면 None(FE 는 검색 폴백).

선택 순서: 대표 행(is_representative) 중 1인분 기준(per_serving) → 시드/총칭 → 음식편(D) →
가공식품 동명 대표(rep:) → 그 외. 군이 없으면(이름 키 폴백) 정규화 이름이 같은 행.
"""
from __future__ import annotations

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.models import NutritionItem
from app.schemas.food import FoodSearchItem
from app.services.matching import base_serving_text, normalize_name

_SOURCE_PRIORITY = case(
    (NutritionItem.source == "seed", 0),
    (NutritionItem.external_id.like("gen:%"), 0),
    (NutritionItem.external_id.like("D%"), 1),
    (NutritionItem.external_id.like("rep:%"), 2),
    else_=3,
)
_PER_SERVING_FIRST = case((NutritionItem.serving_basis == "per_serving", 0), else_=1)


def representative_item(
    db: Session, *, group_id: int | None, name: str | None
) -> NutritionItem | None:
    """군 대표 상품 1행. 군이 없거나 비어 있으면 이름으로 찾는다."""
    order = (_PER_SERVING_FIRST, _SOURCE_PRIORITY, NutritionItem.id)
    if group_id is not None:
        row = db.scalars(
            select(NutritionItem)
            .where(NutritionItem.food_group_id == group_id, NutritionItem.is_representative.is_(True))
            .order_by(*order)
            .limit(1)
        ).first()
        if row is not None:
            return row
    if not name:
        return None
    key = normalize_name(name)
    if not key:
        return None
    return db.scalars(
        select(NutritionItem)
        .where(NutritionItem.normalized_name == key)
        .order_by(NutritionItem.is_representative.desc(), *order)
        .limit(1)
    ).first()


def food_item_payload(item: NutritionItem | None) -> FoodSearchItem | None:
    """검색 결과(FoodSearchItem)와 같은 모양 — FE 장바구니가 그대로 받는다."""
    if item is None:
        return None
    return FoodSearchItem(
        nutrition_item_id=item.id,
        name=item.name,
        base_serving=base_serving_text(item),
        calories=float(item.calories),
        carbs=float(item.carbs),
        protein=float(item.protein),
        fat=float(item.fat),
    )
