"""식단 저장/수정/삭제 서비스 (Phase 6 — 심장부).

- meal_records + meal_items(스냅샷) + correction_logs + 요약 재계산을
  하나의 트랜잭션으로 처리한다 (커밋은 이 모듈이 담당).
- 합계 캐시(total_*)는 항목 합으로 서버가 재계산한다.
- soft delete(deleted_at) — 조회 계열은 전부 deleted_at IS NULL 필터.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.core.timeutil import kst_date_of, now_utc, to_utc
from app.models import (
    AiCallLog,
    CorrectionLog,
    FoodCandidate,
    MealImage,
    MealItem,
    MealRecord,
    NutritionItem,
    User,
)
from app.schemas.meal import MealCreateRequest, MealItemInput, MealUpdateRequest
from app.services.game_profile import ensure_game_profile
from app.services.game_rewards import apply_meal_rewards
from app.services.recommend.feedback import mark_eaten as mark_recommendation_eaten
from app.services.recommend.feedback import reconcile_meal_feedback
from app.services.recommend.groups import load_group_index
from app.services.summary import recompute_daily_summary

logger = logging.getLogger(__name__)

# 슬라이더 양 조정 로그의 correction_type — 보정 칩(half/large/…)과 구분한다.
# 상세 응답의 correction_type(칩 라벨)에는 섞이지 않는다.
SERVING_ADJUSTED = "serving_adjusted"
_SERVING_EPS = 1e-6


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


def _owned_ai_call_log_id(db: Session, user: User, ai_call_log_id: int | None) -> int | None:
    """분석 로그 키는 저장을 막지 않는다 — 없거나 남의 것이면 NULL 로 저장하고 경고만 남긴다."""
    if ai_call_log_id is None:
        return None
    log = db.get(AiCallLog, ai_call_log_id)
    if log is None or (log.user_id is not None and log.user_id != user.id):
        logger.warning("meal.ai_call_log_id ignored id=%s user=%s", ai_call_log_id, user.id)
        return None
    return log.id


def _owned_candidate(db: Session, user: User, candidate_id: int) -> FoodCandidate | None:
    """후보 소유권은 ai_call_logs.user_id 또는 meal_images.user_id 로 확인한다."""
    cand = db.get(FoodCandidate, candidate_id)
    if cand is None:
        return None
    if cand.ai_call_log_id is not None:
        log = db.get(AiCallLog, cand.ai_call_log_id)
        if log is not None and log.user_id is not None and log.user_id != user.id:
            return None
    if cand.meal_image_id is not None:
        image = db.get(MealImage, cand.meal_image_id)
        if image is not None and image.user_id != user.id:
            return None
    return cand


def _mark_selected_candidates(db: Session, user: User, items: list[MealItemInput]) -> None:
    """저장 항목이 가리키는 AI 후보를 is_selected=True 로 — 정답지(AI 채택률)의 원천.

    항목 하나라도 food_candidate_id 를 보냈을 때만 동작한다. 같은 AI 호출의 다른 후보는
    False 로 되돌려, 초안을 바꿔 다시 저장해도 마지막 선택만 True 로 남는다.
    """
    ids = [i.food_candidate_id for i in items if i.food_candidate_id is not None]
    if not ids:
        return
    chosen = [c for c in (_owned_candidate(db, user, cid) for cid in ids) if c is not None]
    if len(chosen) != len(ids):
        logger.warning("meal.food_candidate_id partially ignored user=%s ids=%s", user.id, ids)
    call_log_ids = {c.ai_call_log_id for c in chosen if c.ai_call_log_id is not None}
    if call_log_ids:
        siblings = db.scalars(
            select(FoodCandidate).where(FoodCandidate.ai_call_log_id.in_(call_log_ids))
        )
        for sib in siblings:
            sib.is_selected = False
    for c in chosen:
        c.is_selected = True


def _totals(items: list[MealItemInput]) -> dict[str, float]:
    return {
        "calories": round(sum(i.calories for i in items), 2),
        "carbs": round(sum(i.carbs for i in items), 2),
        "protein": round(sum(i.protein for i in items), 2),
        "fat": round(sum(i.fat for i in items), 2),
    }


def _resolve_food_groups(db: Session, items: list[MealItemInput]) -> list[int | None]:
    """항목별 음식군 id — 매칭 상품의 군 → 이름 alias → 군명 정확일치. 못 찾으면 None(미분류).

    docs/음식군-DB-계약.md §3 I. 어미 추정은 하지 않는다 (오탐이 개인 빈도를 오염시킨다).
    군 테이블이 비어 있으면 전부 None — 군 도입 전과 동일하게 저장된다.
    """
    index = load_group_index(db)
    if not index.enabled:
        return [None] * len(items)
    ids = [i.nutrition_item_id for i in items if i.nutrition_item_id is not None]
    item_groups: dict[int, int | None] = {}
    if ids:
        item_groups = dict(
            db.execute(
                select(NutritionItem.id, NutritionItem.food_group_id).where(NutritionItem.id.in_(ids))
            ).all()
        )
    resolved: list[int | None] = []
    for item in items:
        group = index.resolve(item.food_name, item_groups.get(item.nutrition_item_id))
        resolved.append(group.id if group else None)
    return resolved


def _insert_items(
    db: Session, meal: MealRecord, items: list[MealItemInput]
) -> list[MealItem]:
    """meal_items 생성 + 음식군 스냅샷 + 보정이 있으면 correction_logs(before/after) 기록."""
    created: list[MealItem] = []
    group_ids = _resolve_food_groups(db, items)
    for item, group_id in zip(items, group_ids):
        row = MealItem(
            meal_record_id=meal.id,
            nutrition_item_id=item.nutrition_item_id,
            food_group_id=group_id,
            food_name=item.food_name,
            serving_amount=item.serving_amount,
            calories=item.calories,
            carbs=item.carbs,
            protein=item.protein,
            fat=item.fat,
            bbox=item.bbox.model_dump() if item.bbox else None,
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

        # AI 추정량과 최종 섭취량이 다르면 슬라이더 양 조정으로 기록한다
        # (보정 칩과 별개 행 — AI 추정량 대비 최종량이 정답지 지표의 재료다)
        if (
            item.estimated_serving is not None
            and abs(float(item.estimated_serving) - float(item.serving_amount)) > _SERVING_EPS
        ):
            db.add(
                CorrectionLog(
                    meal_record_id=meal.id,
                    meal_item_id=row.id,
                    correction_type=SERVING_ADJUSTED,
                    before_data={"serving_amount": float(item.estimated_serving)},
                    after_data={"serving_amount": float(item.serving_amount)},
                )
            )
    return created


def create_meal(
    db: Session, user: User, body: MealCreateRequest, *, commit: bool = True
) -> MealRecord:
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
        entry_method=body.entry_method,
        ai_call_log_id=_owned_ai_call_log_id(db, user, body.ai_call_log_id),
        total_calories=totals["calories"],
        total_carbs=totals["carbs"],
        total_protein=totals["protein"],
        total_fat=totals["fat"],
    )
    db.add(meal)
    db.flush()
    _insert_items(db, meal, body.items)
    _mark_selected_candidates(db, user, body.items)

    # 추천 카드에서 시작한 기록만 연결한다. 다른 음식·과거 기록이면 연결은 무시한다.
    if not body.is_skipped and body.recommendation_item_id is not None:
        mark_recommendation_eaten(
            db, user.id, meal.id, recommendation_item_id=body.recommendation_item_id,
        )

    recompute_daily_summary(db, user.id, kst_date_of(eaten_at))
    if commit:
        db.commit()
    return meal


def create_meal_with_rewards(
    db: Session, user: User, body: MealCreateRequest
) -> tuple[MealRecord, dict | None]:
    """식단 저장 + 게이미피케이션 보상을 **한 트랜잭션**으로 처리한다.

    보상이 성공하면 둘이 함께 커밋되고, 실패하면 둘 다 롤백한 뒤 **식단만 다시
    저장한다**. 게임 도메인(마이그레이션 지연·카탈로그 손상 등)이 기록 자체를
    막아서는 안 되기 때문이다. 이때 rewards 는 None 이고 FE 는 연출을 건너뛴다.
    """
    try:
        # 기록을 넣기 **전에** 프로필을 보장한다 — 기존 사용자 백필이 방금 저장한
        # 이 기록까지 세어 유대가 두 번 오르는 것을 막는다.
        ensure_game_profile(db, user)
    except Exception:  # noqa: BLE001
        logger.exception("게임 프로필 보장 실패 (기록은 정상 저장)")
        db.rollback()
        return create_meal(db, user, body, commit=True), None

    meal = create_meal(db, user, body, commit=False)
    try:
        rewards = apply_meal_rewards(db, user, meal)
    except Exception:  # noqa: BLE001 — 보상 실패가 기록 실패가 되면 안 된다
        logger.exception("게이미피케이션 보상 지급 실패 (기록은 정상 저장)")
        db.rollback()
        meal = create_meal(db, user, body, commit=True)
        return meal, None
    db.commit()
    return meal, rewards


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
        _mark_selected_candidates(db, user, body.items)
        totals = _totals(body.items)
        meal.total_calories = totals["calories"]
        meal.total_carbs = totals["carbs"]
        meal.total_protein = totals["protein"]
        meal.total_fat = totals["fat"]

    new_date = kst_date_of(meal.eaten_at)
    reconcile_meal_feedback(db, meal)
    recompute_daily_summary(db, user.id, old_date)
    if new_date != old_date:
        recompute_daily_summary(db, user.id, new_date)
    db.commit()
    return meal


def delete_meal(db: Session, user: User, meal_id: int) -> None:
    meal = get_owned_meal(db, user, meal_id)
    meal.deleted_at = now_utc()
    reconcile_meal_feedback(db, meal)
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
        # 양 조정 로그는 칩이 아니므로 표시용 correction_type 에서 제외
        if log.meal_item_id is not None and log.correction_type != SERVING_ADJUSTED:
            correction_by_item[log.meal_item_id] = log.correction_type
    return [(item, correction_by_item.get(item.id)) for item in items]
