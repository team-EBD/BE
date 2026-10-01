"""AI 분석 서비스 (Phase 8, 명세서 6.1).

역할 분담(아키텍처 §8): AI 서버는 원본 후보만 반환하고, BE 가
① food_name→nutrition_items 매칭 ② 식습관 보정(habit_adjusted)
③ food_candidates 저장 ④ ai_call_logs 기록을 담당한다.
"""
from __future__ import annotations

import logging
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
from app.services.game_skills import DEPTH_CLARIFIER, candidate_depth
from app.services.matching import (
    base_serving_text,
    db_candidates_for_text,
    match_food_name,
    normalize_name,
)


logger = logging.getLogger("eatlog.analyze")

# AI 서버와 동일한 상한 (AI 서버가 이미 지키지만 방어적으로 재적용)
MAX_FOODS = 5
MAX_PREDICTIONS_PER_FOOD = 3
# '발견 돋보기'(candidate_depth="clarifier")는 음식당 대체 후보를 1개 더 준다.
# 여기 상한을 올려 주지 않으면 4번째 후보가 방어 컷에 잘려 스킬이 무효가 된다.
MAX_PREDICTIONS_PER_FOOD_CLARIFIER = MAX_PREDICTIONS_PER_FOOD + 1

# 유사도(fuzzy) 매칭은 확신도를 한 단계 감산해 내려보낸다 (SCRUM-246).
# 별도 "유사 매칭" UI 를 만들지 않고 기존 confidence 채널로 불확실성을 전달
# — 이미지 분석의 정답률 표시와 신호가 이원화되지 않게 한다 (PM 결정 08-11).
FUZZY_CONFIDENCE_PENALTY = 0.2


def _grouped_candidates(
    raw: list, max_per_food: int = MAX_PREDICTIONS_PER_FOOD
) -> list[tuple[int, object]]:
    """(food_index, candidate) 목록으로 정규화한다.

    - food_index 는 등장 순서 기준으로 0부터 재부여
    - 서로 다른 음식 최대 MAX_FOODS 개, 음식당 예측 최대 max_per_food 개
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
        if counts.get(original, 0) >= max_per_food:
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


# 그릇·접시 음식(낱개 단위 없음)은 g 환산이 이 범위면 '한 그릇' 으로 본다 — AI 의 g 눈대중이 ±30% 는 흔들려서
# 라면 한 그릇이 1.2·1.3인분으로 찍히던 잡음을 없앤다. 범위 밖(반 그릇 0.5, 두 그릇 2.0)은 계산값 그대로.
_SNAP_LOW, _SNAP_HIGH = 0.7, 1.3


def _is_product_row(matched) -> bool:
    """식약처 가공식품(봉지·키트·동명 대표) 행 — 기준량이 포장 단위라 사진의 조리량(g)과 맞지 않는다.

    신라면 봉지 120g 에 조리된 한 그릇 560g 을 나누면 4.7인분, 평양냉면 키트 200g 에 650g 은 3.25인분이 됐다.
    시드·총칭·식약처 음식편 행은 조리된 요리 무게라 g 로 나눠도 된다.
    """
    ext = getattr(matched, "external_id", None) or ""
    return getattr(matched, "source", None) == "public" and (ext.startswith("P") or ext.startswith("rep:"))


def _reconcile_serving(cand, matched) -> float:
    """AI 의 절대량(g)을 **우리 영양DB 기준량**으로 나눠 배수로 바꾼다.

    AI 는 우리 DB 의 1인분이 몇 g 인지 모른다. 그래서 AI 가 준 배수(estimated_serving)는 "AI 가 생각하는
    1인분"에 대한 배수이고, 우리 기준과 다르면 몇 배씩 어긋난다(피자를 AI 는 1판, DB 는 2조각으로 본다).
    절대량이 오면 그것을 기준으로 다시 계산한다 — 낱개 음식(count_unit)은 개수 × 1단위 g 이라 특히 믿을 만하다.

    - 절대량이 없거나 매칭이 없으면 AI 배수 그대로.
    - 가공식품 행은 g 을 쓰지 않는다 (포장 단위 기준량).
    - 그릇 음식(단위 없음)은 0.7~1.3 을 1.0 으로 스냅한다. 낱개 음식은 스냅하지 않는다(2개는 2.0).
    """
    ai_serving = float(cand.estimated_serving)
    grams = getattr(cand, "estimated_serving_g", None)
    if grams is None or matched is None or _is_product_row(matched):
        return ai_serving
    base = float(matched.base_amount or 0)
    if base <= 0:
        return ai_serving
    serving = grams / base
    if not (_SERVING_MIN <= serving <= _SERVING_MAX):
        return ai_serving
    if not getattr(cand, "count_unit", None) and _SNAP_LOW <= serving <= _SNAP_HIGH:
        return 1.0
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

    # '발견 돋보기' 판정 — 켜져 있고 장착·충전이 맞으면 후보를 1개 더 받는다.
    # 판정은 예외를 밖으로 내지 않으므로 여기서 분석이 막히지는 않는다.
    depth = candidate_depth(db, user.id)

    # 사용자가 사진과 함께 적은 설명 — AI 가 식별·수량 힌트로 쓴다.
    # 설명이 있을 때만 키워드를 넘겨 구 시그니처 클라이언트와의 호환을 유지한다.
    # candidate_depth 도 같은 이유로 **clarifier 일 때만** 넘긴다 — 기본값이면
    # 구버전 AI 서버·테스트 더블이 모르는 필드를 받을 일이 없다.
    cleaned_text = (user_text or "").strip() or None
    extra: dict = {}
    if cleaned_text:
        extra["user_text"] = cleaned_text
    if depth == DEPTH_CLARIFIER:
        extra["candidate_depth"] = depth
    result = ai.analyze(image.image_url, habits_payload, **extra)
    return _postprocess(
        db, user, habit, result, meal_image_id, started,
        max_per_food=(
            MAX_PREDICTIONS_PER_FOOD_CLARIFIER
            if depth == DEPTH_CLARIFIER
            else MAX_PREDICTIONS_PER_FOOD
        ),
    )


def analyze_meal_text(
    db: Session, user: User, text: str, ai: AIClient
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    """자연어 식사 서술 → 기록 초안 (이미지 분석과 동일한 후처리·응답 계약).

    이미지가 없으므로 food_candidates.meal_image_id 는 NULL 로 저장된다.
    사용량은 이미지 분석과 같은 analyze 쿼터를 공유한다 (라우터에서 enforce).

    선(先)-매칭: 문장에 이름이 등장하는 영양 DB 항목을 먼저 찾아 AI 에
    함께 보낸다 — AI 음식명이 DB 명명에 정렬돼 사후 매칭 실패(→ 추정
    영양 폴백)가 줄어든다. 후보가 없으면 기존과 동일하게 자유 추출.
    """
    started = time.perf_counter()
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    cleaned = text.strip()
    rows = db_candidates_for_text(db, cleaned)
    if rows:
        db_cands = [
            {"name": r.name, "base_serving": base_serving_text(r)} for r in rows
        ]
        result = ai.parse_text(cleaned, db_candidates=db_cands)
    else:
        # 후보 없을 땐 구 시그니처 호출 — 테스트 더블·구버전 클라이언트 호환
        result = ai.parse_text(cleaned)
    return _postprocess(db, user, habit, result, None, started)


def _postprocess(
    db: Session,
    user: User,
    habit: EatingHabit | None,
    result,
    meal_image_id: int | None,
    started: float,
    max_per_food: int = MAX_PREDICTIONS_PER_FOOD,
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
    for rank, (food_index, cand) in enumerate(
        _grouped_candidates(result.candidates, max_per_food), start=1
    ):
        matched, match_path = match_food_name(db, cand.food_name)
        # 경로별 비율(exact/substring/fuzzy/none)이 유사도 컷 튜닝의 근거 (SCRUM-246)
        logger.info(
            "nutrition_match path=%s food=%s item_id=%s",
            match_path, cand.food_name, matched.id if matched else None,
        )
        confidence = float(cand.confidence)
        if match_path == "fuzzy":
            confidence = round(max(confidence - FUZZY_CONFIDENCE_PENALTY, 0.0), 4)
        serving = _reconcile_serving(cand, matched)
        count = getattr(cand, "count", None)
        count_unit = getattr(cand, "count_unit", None)
        if not count or not count_unit:  # 둘 다 있어야 낱개 음식
            count = count_unit = None
        grams = getattr(cand, "estimated_serving_g", None)
        row = FoodCandidate(
            meal_image_id=meal_image_id,
            ai_call_log_id=call_log.id,
            nutrition_item_id=matched.id if matched else None,
            food_name=cand.food_name,
            normalized_name=normalize_name(cand.food_name),
            confidence_score=confidence,
            estimated_serving=serving,
            quantity=count,
            quantity_unit=count_unit,
            grams_per_unit=round(float(grams) / count, 2) if (count and grams) else None,
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
                confidence_score=confidence,
                estimated_serving=serving,
                has_soup=cand.has_soup,
                has_sauce=cand.has_sauce,
                bbox=_bbox_of(cand),
                nutrition=nutrition,
                habit_adjusted=habit_adjusted,
                quantity=float(count) if count else None,
                quantity_unit=count_unit,
                serving_per_unit=round(serving / count, 4) if count else None,
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
