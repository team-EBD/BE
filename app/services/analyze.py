"""AI 분석 서비스 (Phase 8, 명세서 6.1).

역할 분담(아키텍처 §8): AI 서버는 원본 후보만 반환하고, BE 가
① food_name→nutrition_items 매칭 ② 식습관 보정(habit_adjusted)
③ food_candidates 저장 ④ ai_call_logs 기록을 담당한다.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_client.base import AIClient, AICallLogPayload
from app.core.errors import APIError
from app.models import AiCallLog, EatingHabit, FoodCandidate, MealImage, User
from app.schemas.meal import (
    AnalyzeCandidate,
    AnalyzeFailedResponse,
    AnalyzeSuccessResponse,
    CandidateNutrition,
    HabitAdjusted,
)
from app.services.correction import habit_factor
from app.services.matching import base_serving_text, match_food_name, normalize_name


def _save_call_log(
    db: Session, user_id: int, meal_image_id: int | None, payload: AICallLogPayload,
    error_message: str | None = None,
) -> AiCallLog:
    log = AiCallLog(
        user_id=user_id,
        meal_image_id=meal_image_id,
        task_type=payload.task_type,
        provider=payload.provider,
        model_name=payload.model_name,
        status=payload.status,
        latency_ms=payload.latency_ms,
        error_message=error_message,
    )
    db.add(log)
    db.flush()
    return log


def analyze_meal_image(
    db: Session, user: User, meal_image_id: int, ai: AIClient
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    image = db.get(MealImage, meal_image_id)
    if image is None:
        raise APIError(404, "NOT_FOUND", "업로드된 이미지를 찾을 수 없습니다.")
    if image.user_id != user.id:
        raise APIError(403, "FORBIDDEN", "다른 사용자의 이미지입니다.")

    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    habits_payload = None
    if habit is not None:
        habits_payload = {
            "default_portion": habit.default_portion,
            "soup_preference": habit.soup_preference,
            "sauce_preference": habit.sauce_preference,
        }

    result = ai.analyze(image.image_url, habits_payload)
    call_log = _save_call_log(
        db, user.id, meal_image_id, result.ai_call_log, error_message=result.reason
    )

    if result.status == "failed":
        db.commit()
        return AnalyzeFailedResponse(
            reason=result.reason or "provider_error",
            fallback_action=result.fallback_action or "manual_food_search",
            ai_call_log_id=call_log.id,
        )

    factor, applied = habit_factor(habit)
    candidates: list[AnalyzeCandidate] = []
    for rank, cand in enumerate(result.candidates[:3], start=1):
        matched = match_food_name(db, cand.food_name)
        row = FoodCandidate(
            meal_image_id=meal_image_id,
            ai_call_log_id=call_log.id,
            nutrition_item_id=matched.id if matched else None,
            food_name=cand.food_name,
            normalized_name=normalize_name(cand.food_name),
            confidence_score=cand.confidence,
            estimated_serving=cand.estimated_serving,
            rank=rank,
        )
        db.add(row)
        db.flush()

        nutrition = None
        habit_adjusted = None
        if matched is not None:
            nutrition = CandidateNutrition(
                base_serving=base_serving_text(matched),
                calories=float(matched.calories),
                carbs=float(matched.carbs),
                protein=float(matched.protein),
                fat=float(matched.fat),
            )
            if applied:  # 식습관 설정 없으면 생략 (명세서 6.1)
                habit_adjusted = HabitAdjusted(
                    applied_factor=factor,
                    applied_corrections=applied,
                    calories=round(float(matched.calories) * factor, 1),
                )
        candidates.append(
            AnalyzeCandidate(
                food_candidate_id=row.id,
                nutrition_item_id=row.nutrition_item_id,
                normalized_name=row.normalized_name,
                confidence_score=float(cand.confidence),
                estimated_serving=float(cand.estimated_serving),
                nutrition=nutrition,
                habit_adjusted=habit_adjusted,
            )
        )

    db.commit()
    return AnalyzeSuccessResponse(
        draft_notice=result.draft_notice or "AI가 분석한 기록 초안입니다.",
        candidates=candidates,
        ai_call_log_id=call_log.id,
    )
