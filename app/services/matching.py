"""음식 검색·영양 매칭 (Phase 5).

- 검색: nutrition_items.normalized_name / name 부분일치(MVP 는 단순 LIKE).
- 매칭: AI 후보 food_name → nutrition_items 1건 매칭 (정확일치 우선 → 부분일치).
- 정규화 규칙: 공백 제거. (시드의 normalized_name 과 동일 규칙)
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import PageParams
from app.models import NutritionItem


def normalize_name(name: str) -> str:
    return name.replace(" ", "").strip()


def base_serving_text(item: NutritionItem) -> str:
    """기준 제공량 표기 (예: '1인분(400g)', 공공DB 항목은 '100g당')."""
    amount = float(item.base_amount)
    amount_text = f"{amount:g}"
    if item.source == "public":
        # 공공DB 는 100g/100ml 당 값 — '1인분' 으로 표기하면 오해라 기준량 그대로 노출
        return f"{amount_text}{item.base_unit}당"
    if item.base_unit == "g":
        return f"1인분({amount_text}g)"
    return f"{amount_text}{item.base_unit}"


def search_items(
    db: Session, query: str, params: PageParams
) -> tuple[list[NutritionItem], int]:
    """부분일치 검색 + 페이지네이션. (items, total)

    관련도 정렬: 정확일치 → 전방일치 → 이름 짧은 순 (공공DB 4.7만 건에서
    id 순 정렬은 무의미하므로). 브랜드명(brand)도 검색 대상에 포함.
    """
    normalized = normalize_name(query)
    condition = or_(
        NutritionItem.normalized_name.contains(normalized),
        NutritionItem.name.contains(query.strip()),
        NutritionItem.brand.contains(query.strip()),
    )
    exact_match = NutritionItem.normalized_name == normalized
    prefix_match = NutritionItem.normalized_name.startswith(normalized)
    total = db.scalar(select(func.count()).select_from(NutritionItem).where(condition)) or 0
    items = list(
        db.scalars(
            select(NutritionItem)
            .where(condition)
            .order_by(
                exact_match.desc(),
                prefix_match.desc(),
                func.length(NutritionItem.name),
                NutritionItem.id,
            )
            .offset(params.offset)
            .limit(params.limit)
        )
    )
    return items, total


def match_food_name(db: Session, food_name: str) -> NutritionItem | None:
    """AI 후보 음식명을 영양 DB 1건에 매칭. 없으면 None."""
    normalized = normalize_name(food_name)
    if not normalized:
        return None
    exact = db.scalar(
        select(NutritionItem).where(NutritionItem.normalized_name == normalized).limit(1)
    )
    if exact is not None:
        return exact
    return db.scalar(
        select(NutritionItem)
        .where(NutritionItem.normalized_name.contains(normalized))
        # 가장 짧은(일반적인) 이름 우선 — 동률이면 낮은 id(시드 우선)
        .order_by(func.length(NutritionItem.normalized_name), NutritionItem.id)
        .limit(1)
    )
