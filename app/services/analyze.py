"""AI 분석 서비스 (Phase 8, 명세서 6.1).

역할 분담(아키텍처 §8): AI 서버는 원본 후보만 반환하고, BE 가
① food_name→nutrition_items 매칭 ② 식습관 보정(habit_adjusted)
③ food_candidates 저장 ④ ai_call_logs 기록을 담당한다.
"""
from __future__ import annotations

import time

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_client.base import AIClient, AICallLogPayload
from app.core.errors import APIError
from app.models import AiCallLog, EatingHabit, FoodCandidate, MealImage, User
from app.schemas.meal import (
    AnalyzeCandidate,
    AnalyzeFailedResponse,
    AnalyzeSuccessResponse,
    BoundingBox,
    CandidateNutrition,
    HabitAdjusted,
)
from app.services.correction import FACTORS, habit_factor
from app.services.matching import base_serving_text, match_food_name, normalize_name


# AI 서버와 동일한 상한 (AI 서버가 이미 지키지만 방어적으로 재적용)
MAX_FOODS = 5
MAX_PREDICTIONS_PER_FOOD = 3


def _grouped_candidates(raw: list) -> list[tuple[int, object]]:
    """(food_index, candidate) 목록으로 정규화한다.

    - food_index 는 등장 순서 기준으로 0부터 재부여
    - 서로 다른 음식 최대 MAX_FOODS 개, 음식당 예측 최대 MAX_PREDICTIONS_PER_FOOD 개
    - 구버전 AI 서버(food_index 없음)는 전부 0 그룹 → 기존 상위 3개와 동일 동작
    """
    counts: dict[int, int] = {}
    reindex: dict[int, int] = {}
    grouped: list[tuple[int, object]] = []
    for cand in raw:
        original = getattr(cand, "food_index", 0) or 0
        if original not in reindex:
            if len(reindex) >= MAX_FOODS:
                continue
            reindex[original] = len(reindex)
        if counts.get(original, 0) >= MAX_PREDICTIONS_PER_FOOD:
            continue
        counts[original] = counts.get(original, 0) + 1
        grouped.append((reindex[original], cand))
    return grouped


def _bbox_of(cand) -> BoundingBox | None:
    """AI 후보의 위치 좌표를 응답 스키마로 옮긴다.

    범위를 벗어난 좌표(구/오작동 AI 서버)는 분석 전체를 500 으로 만들지 않고
    좌표만 버린다 — 오버레이가 없을 뿐 기록은 정상 진행된다.
    """
    raw = getattr(cand, "bbox", None)
    if raw is None:
        return None
    try:
        return BoundingBox(**raw.model_dump())
    except ValidationError:
        return None


# 환산 결과 상한 — food_candidates.estimated_serving 은 Numeric(8,2) 이고
# 사용자가 보정 슬라이더로 다시 만지므로 상식 범위를 벗어나면 1인분으로 되돌린다.
_SERVING_MIN, _SERVING_MAX = 0.1, 10.0


def _reconcile_serving(cand, matched) -> float:
    """AI 의 절대량(g)을 **우리 영양DB 기준량**으로 나눠 배수로 바꾼다.

    AI 는 우리 DB 의 1인분이 몇 g 인지 모른다. 그래서 AI 가 준 배수
    (estimated_serving)는 "AI 가 생각하는 1인분"에 대한 배수이고, 우리 기준과
    다르면 그대로 곱했을 때 몇 배씩 어긋난다 — 피자를 AI 는 1판, DB 는 1조각으로
    볼 수 있다. 절대량이 오면 그것을 기준으로 다시 계산한다.

    절대량이 없거나(구버전 AI·추정 실패) 매칭된 항목이 없으면 기존 배수를 쓴다.
    """
    grams = getattr(cand, "estimated_serving_g", None)
    if grams is None or matched is None:
        return float(cand.estimated_serving)
    base = float(matched.base_amount or 0)
    if base <= 0:
        return float(cand.estimated_serving)
    serving = grams / base
    if not (_SERVING_MIN <= serving <= _SERVING_MAX):
        return float(cand.estimated_serving)
    return round(serving, 2)


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
    db: Session,
    user: User,
    meal_image_id: int,
    ai: AIClient,
    user_text: str | None = None,
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    started = time.perf_counter()
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

    # 사용자가 사진과 함께 적은 설명 — AI 가 식별·수량 힌트로 쓴다.
    # 설명이 있을 때만 키워드를 넘겨 구 시그니처 클라이언트와의 호환을 유지한다.
    cleaned_text = (user_text or "").strip() or None
    if cleaned_text:
        result = ai.analyze(image.image_url, habits_payload, user_text=cleaned_text)
    else:
        result = ai.analyze(image.image_url, habits_payload)
    return _postprocess(db, user, habit, result, meal_image_id, started)


def analyze_meal_text(
    db: Session, user: User, text: str, ai: AIClient
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    """자연어 식사 서술 → 기록 초안 (이미지 분석과 동일한 후처리·응답 계약).

    이미지가 없으므로 food_candidates.meal_image_id 는 NULL 로 저장된다.
    사용량은 이미지 분석과 같은 analyze 쿼터를 공유한다 (라우터에서 enforce).
    """
    started = time.perf_counter()
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    result = ai.parse_text(text.strip())
    return _postprocess(db, user, habit, result, None, started)


def _postprocess(
    db: Session,
    user: User,
    habit: EatingHabit | None,
    result,
    meal_image_id: int | None,
    started: float,
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    """AI 결과 공통 후처리 — 로그 기록, 영양 매칭, 식습관 보정, 후보 저장."""
    call_log = _save_call_log(
        db, user.id, meal_image_id, result.ai_call_log, error_message=result.reason
    )

    if result.status == "failed":
        call_log.total_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        return AnalyzeFailedResponse(
            reason=result.reason or "provider_error",
            fallback_action=result.fallback_action or "manual_food_search",
            ai_call_log_id=call_log.id,
        )

    factor, applied = habit_factor(habit)
    candidates: list[AnalyzeCandidate] = []
    for rank, (food_index, cand) in enumerate(_grouped_candidates(result.candidates), start=1):
        matched = match_food_name(db, cand.food_name)
        serving = _reconcile_serving(cand, matched)
        row = FoodCandidate(
            meal_image_id=meal_image_id,
            ai_call_log_id=call_log.id,
            nutrition_item_id=matched.id if matched else None,
            food_name=cand.food_name,
            normalized_name=normalize_name(cand.food_name),
            confidence_score=cand.confidence,
            estimated_serving=serving,
            rank=rank,
        )
        db.add(row)
        db.flush()

        # 영양값 우선순위: ① 영양 DB 매칭(정확) ② AI 추정치(초안 fallback).
        # 시드 DB에 없는 음식도 사용자가 수정 가능한 초안으로 기록을 이어갈 수 있다.
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
        elif cand.nutrition is not None:
            nutrition = CandidateNutrition(
                base_serving=cand.nutrition.base_serving,
                calories=float(cand.nutrition.calories),
                carbs=float(cand.nutrition.carbs),
                protein=float(cand.nutrition.protein),
                fat=float(cand.nutrition.fat),
            )
        # 식습관 보정 중 국물/소스 관련 항목은 그 음식에 국물/소스가 있을 때만
        # 적용한다 (예: soup_preference=leave 여도 공기밥엔 no_soup 미적용).
        cand_applied = [
            c for c in applied
            if not (c == "no_soup" and not cand.has_soup)
            and not (c == "no_sauce" and not cand.has_sauce)
        ]
        if cand_applied == applied:
            cand_factor = factor
        else:
            cand_factor = 1.0
            for c in cand_applied:
                cand_factor *= FACTORS[c]
            cand_factor = round(cand_factor, 4)
        if nutrition is not None and cand_applied:  # 식습관 설정 없으면 생략 (명세서 6.1)
            habit_adjusted = HabitAdjusted(
                applied_factor=cand_factor,
                applied_corrections=cand_applied,
                calories=round(nutrition.calories * cand_factor, 1),
            )
        candidates.append(
            AnalyzeCandidate(
                food_candidate_id=row.id,
                nutrition_item_id=row.nutrition_item_id,
                food_index=food_index,
                normalized_name=row.normalized_name,
                confidence_score=float(cand.confidence),
                estimated_serving=serving,
                has_soup=cand.has_soup,
                has_sauce=cand.has_sauce,
                bbox=_bbox_of(cand),
                nutrition=nutrition,
                habit_adjusted=habit_adjusted,
            )
        )

    # BE 전체 처리 시간 — AI 내부(latency_ms)와의 차이가 매칭·저장 오버헤드
    call_log.total_ms = int((time.perf_counter() - started) * 1000)
    db.commit()
    return AnalyzeSuccessResponse(
        draft_notice=result.draft_notice or "AI가 분석한 기록 초안입니다.",
        candidates=candidates,
        ai_call_log_id=call_log.id,
    )
