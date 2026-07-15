"""식단 저장/수정/삭제 서비스 (Phase 6 — 심장부).

- meal_records + meal_items(스냅샷) + correction_logs + 요약 재계산을
  하나의 트랜잭션으로 처리한다 (커밋은 이 모듈이 담당).
- 합계 캐시(total_*)는 항목 합으로 서버가 재계산한다.
- soft delete(deleted_at) — 조회 계열은 전부 deleted_at IS NULL 필터.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.core.timeutil import kst_date_of, now_utc, to_utc
from app.models import CorrectionLog, MealImage, MealItem, MealRecord, User
from app.schemas.meal import MealCreateRequest, MealItemInput, MealUpdateRequest
from app.services.summary import recompute_daily_summary


def get_owned_meal(db: Session, user: User, meal_id: int) -> MealRecord:
    """존재+소유권 확인. 404/403 (횡단 관심사)."""
    meal = db.get(MealRecord, meal_id)
    if meal is None or meal.deleted_at is not None:
        raise APIError(404, "NOT_FOUND", "식단 기록을 찾을 수 없습니다.")
    if meal.user_id != user.id:
        raise APIError(403, "FORBIDDEN", "다른 사용자의 식단에 접근할 수 없습니다.")
    return meal


def _validate_image(db: Session, user: User, meal_image_id: int | None) -> None:
    if meal_image_id is None:
        return
    image = db.get(MealImage, meal_image_id)
    if image is None:
        raise APIError(404, "NOT_FOUND", "업로드된 이미지를 찾을 수 없습니다.")
    if image.user_id != user.id:
        raise APIError(403, "FORBIDDEN", "다른 사용자의 이미지입니다.")


def _totals(items: list[MealItemInput]) -> dict[str, float]:
    return {
        "calories": round(sum(i.calories for i in items), 2),
        "carbs": round(sum(i.carbs for i in items), 2),
        "protein": round(sum(i.protein for i in items), 2),
        "fat": round(sum(i.fat for i in items), 2),
    }


def _insert_items(
    db: Session, meal: MealRecord, items: list[MealItemInput]
) -> list[MealItem]:
    """meal_items 생성 + 보정이 있으면 correction_logs(before/after) 기록."""
    created: list[MealItem] = []
    for item in items:
        row = MealItem(
            meal_record_id=meal.id,
            nutrition_item_id=item.nutrition_item_id,
            food_name=item.food_name,
            serving_amount=item.serving_amount,
            calories=item.calories,
            carbs=item.carbs,
            protein=item.protein,
            fat=item.fat,
        )
        db.add(row)
        db.flush()
        created.append(row)

        if item.correction_type is not None:
            after = {
                "calories": item.calories,
                "carbs": item.carbs,
                "protein": item.protein,
                "fat": item.fat,
            }
            db.add(
                CorrectionLog(
                    meal_record_id=meal.id,
                    meal_item_id=row.id,
                    correction_type=item.correction_type,
                    before_data=item.before_data.model_dump() if item.before_data else None,
                    after_data=after,
                )
            )
    return created


def create_meal(db: Session, user: User, body: MealCreateRequest) -> MealRecord:
    _validate_image(db, user, body.meal_image_id)
    eaten_at = to_utc(body.eaten_at)
    totals = _totals(body.items)

    meal = MealRecord(
        user_id=user.id,
        meal_image_id=body.meal_image_id,
        meal_type=body.meal_type,
        eaten_at=eaten_at,
        is_skipped=body.is_skipped,
        memo=body.memo,
        total_calories=totals["calories"],
        total_carbs=totals["carbs"],
        total_protein=totals["protein"],
        total_fat=totals["fat"],
    )
    db.add(meal)
    db.flush()
    _insert_items(db, meal, body.items)

    recompute_daily_summary(db, user.id, kst_date_of(eaten_at))
    db.commit()
    return meal


def update_meal(db: Session, user: User, meal_id: int, body: MealUpdateRequest) -> MealRecord:
    meal = get_owned_meal(db, user, meal_id)
    old_date = kst_date_of(meal.eaten_at)

    if body.meal_type is not None:
        meal.meal_type = body.meal_type
    if body.eaten_at is not None:
        meal.eaten_at = to_utc(body.eaten_at)
    if body.memo is not None:
        meal.memo = body.memo

    if body.is_skipped is True:
        # 생략으로 전환 — 기존 항목 제거 + 합계 0 (correction_logs 는 이력이므로 유지)
        meal.is_skipped = True
        for old in db.scalars(select(MealItem).where(MealItem.meal_record_id == meal.id)):
            db.delete(old)
        db.flush()
        meal.total_calories = 0
        meal.total_carbs = 0
        meal.total_protein = 0
        meal.total_fat = 0
    elif body.is_skipped is False and meal.is_skipped and body.items is None:
        raise APIError(
            400, "VALIDATION_ERROR", "생략을 해제하려면 items 를 함께 보내야 합니다.",
            details=[{"field": "items", "reason": "required"}],
        )

    if body.items is not None:
        meal.is_skipped = False  # 항목이 담기면 생략 기록이 아니다
        # 항목 전체 교체(기존 항목·보정 로그는 이력이므로 로그는 남기고 항목만 재구성)
        for old in db.scalars(select(MealItem).where(MealItem.meal_record_id == meal.id)):
            db.delete(old)
        db.flush()
        _insert_items(db, meal, body.items)
        totals = _totals(body.items)
        meal.total_calories = totals["calories"]
        meal.total_carbs = totals["carbs"]
        meal.total_protein = totals["protein"]
        meal.total_fat = totals["fat"]

    new_date = kst_date_of(meal.eaten_at)
    recompute_daily_summary(db, user.id, old_date)
    if new_date != old_date:
        recompute_daily_summary(db, user.id, new_date)
    db.commit()
    return meal


def delete_meal(db: Session, user: User, meal_id: int) -> None:
    meal = get_owned_meal(db, user, meal_id)
    meal.deleted_at = now_utc()
    recompute_daily_summary(db, user.id, kst_date_of(meal.eaten_at))
    db.commit()


def meal_items_with_corrections(
    db: Session, meal: MealRecord
) -> list[tuple[MealItem, str | None]]:
    """상세 응답용: 항목별 최신 correction_type (correction_logs 조인)."""
    items = list(
        db.scalars(
            select(MealItem)
            .where(MealItem.meal_record_id == meal.id)
            .order_by(MealItem.id)
        )
    )
    logs = db.scalars(
        select(CorrectionLog)
        .where(CorrectionLog.meal_record_id == meal.id)
        .order_by(CorrectionLog.id)
    )
    correction_by_item: dict[int, str] = {}
    for log in logs:
        if log.meal_item_id is not None:
            correction_by_item[log.meal_item_id] = log.correction_type
    return [(item, correction_by_item.get(item.id)) for item in items]
