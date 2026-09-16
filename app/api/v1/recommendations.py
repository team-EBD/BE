"""추천 라우터 (Phase 9·10, 명세서 10장).

- NFR-010: 사용자 명시 호출 시에만. 모든 응답에 caution_text (FR-REC-003).
- BE 가 daily_summary 를 계산해 AI 서버에 전달한다(현행 internal 계약).
- 실패 시 502 AI_PROVIDER_ERROR / 504 AI_TIMEOUT (명세서 10.1).
  AI 서버는 실패도 200 + status=failed 로 주므로, reason 을 서버 로그와
  error.details 에 남겨 5xx 의 원인(no_candidates/provider_error 등)을 추적 가능하게 한다.
- 위치 기반(10.3)은 위치 동의 사용자만(403), 좌표는 Body 로만 받는다.
- /menu 는 settings.recommend_engine 로 갈린다: legacy(AI) | v2(이력 기반 규칙 엔진,
  app/services/recommend). v2 는 AI·일일 한도를 쓰지 않고 노출을 recommendation_items 에 남긴다.
  카드 탭은 POST /recommendations/{log_id}/accept 로 신고한다 (채택률 → 랭킹 accept 항).
"""
from __future__ import annotations

import logging
import time
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_client import get_ai_client
from app.ai_client.base import AIClient, RecommendResult
from app.core.config import settings
from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.timeutil import KST, kst_day_bounds, now_utc, to_kst
from app.models import (
    AiCallLog,
    LocationConsent,
    MealItem,
    MealRecord,
    RecommendationLog,
    User,
)
from app.schemas.recommendation import (
    AcceptRequest,
    AcceptResponse,
    LocationMenuRequest,
    LocationMenuResponse,
    MenuBudget,
    MenuItem,
    MenuRequest,
    MenuResponse,
    NextMealRequest,
    NextMealResponse,
    RecommendationItem,
)
from app.services.recommend import recommend as recommend_v2
from app.services.recommend.feedback import log_exposure, mark_accepted
from app.services.summary import aggregate_day, get_goals
from app.services.usage_limit import enforce_daily_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

DEFAULT_CATEGORY = "convenience_store"
DEFAULT_CAUTION = "추천은 생활 식단 참고용이며 의학적 조언이 아닙니다."


def _default_meal_timing() -> str:
    """요청에 meal_timing 이 없을 때의 기본값 — KST 현재 시각 기준.

    10시 이전 breakfast / 15시 이전 lunch / 그 외 dinner.
    """
    hour = to_kst(now_utc()).hour
    if hour < 10:
        return "breakfast"
    return "lunch" if hour < 15 else "dinner"


def _daily_summary_payload(db: Session, user_id: int, day: date) -> dict:
    """AI 서버 RecommendRequest.daily_summary 계약(ai-server schemas 1:1)."""
    total = aggregate_day(db, user_id, day)
    goals = get_goals(db, user_id)
    return {
        "total_calories": total["calories"],
        "total_carbs": total["carbs"],
        "total_protein": total["protein"],
        "total_fat": total["fat"],
        "goal_calories": goals["calories"],
        "goal_protein": goals["protein"],
    }


def _history_context_payload(db: Session, user_id: int, day: date) -> dict | None:
    """오늘 먹은 음식 이력 → AI RecommendRequest.user_history_context 계약.

    reason 이 실제 먹은 음식(특히 직전 식사)을 근거로 작성되도록 음식 이름을
    eaten_at 순으로 전달한다. 기록이 없으면 None(필드 생략).
    """
    start, end = kst_day_bounds(day)
    records = db.scalars(
        select(MealRecord)
        .where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),  # 생략 기록은 먹은 이력이 아니다
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
        .order_by(MealRecord.eaten_at)
    ).all()
    if not records:
        return None

    items_by_record: dict[int, list[str]] = {r.id: [] for r in records}
    items = db.scalars(
        select(MealItem)
        .where(MealItem.meal_record_id.in_(items_by_record))
        .order_by(MealItem.id)
    ).all()
    for item in items:
        items_by_record[item.meal_record_id].append(item.food_name)

    today_foods = [name for r in records for name in items_by_record[r.id]]
    last = records[-1]
    return {
        "today_foods": today_foods,
        "last_meal_type": last.meal_type,
        "last_meal_foods": items_by_record[last.id],
    }


def _call_and_log(
    db: Session,
    user: User,
    ai: AIClient,
    day: date,
    preferred_category: str,
    meal_timing: str,
) -> tuple[RecommendResult, AiCallLog]:
    """AI 추천 호출 + ai_call_logs 기록(성공/실패 예외 없이). 실패 시 5xx 변환."""
    started = time.perf_counter()
    enforce_daily_limit(db, user.id, "recommend")  # 일일 한도 초과 시 429
    summary_payload = _daily_summary_payload(db, user.id, day)
    history_payload = _history_context_payload(db, user.id, day)
    # 현재 KST 시각을 함께 전달 — AI reason 이 시간대(늦은 밤 등)를 고려한다
    current_time = to_kst(now_utc()).strftime("%H:%M")
    result = ai.recommend(
        summary_payload, preferred_category, meal_timing, history_payload,
        current_time=current_time,
    )

    call_log = AiCallLog(
        user_id=user.id,
        task_type=result.ai_call_log.task_type,
        provider=result.ai_call_log.provider,
        model_name=result.ai_call_log.model_name,
        status=result.ai_call_log.status,
        latency_ms=result.ai_call_log.latency_ms,
        error_message=result.reason,
    )
    db.add(call_log)
    db.flush()
    # BE 처리 시간 (요약·이력 조회 + AI 호출 포함) — AI 내부 latency_ms 와 분해용
    call_log.total_ms = int((time.perf_counter() - started) * 1000)

    if result.status == "failed":
        db.commit()  # 실패도 로그는 남긴다 (횡단 관심사: AI 로깅)
        reason = result.reason or "provider_error"
        # AI 서버는 실패도 200 + status=failed 로 응답하므로, 여기서 reason 을
        # 남기지 않으면 5xx 원인을 알 수 없다 (예: no_candidates=후보 DB 비어있음,
        # provider_error=AI 서버 DB 접속/Gemini 오류, invalid_response=응답 계약 불일치).
        logger.warning(
            "AI recommend 실패: reason=%s latency_ms=%d user_id=%d ai_call_log_id=%d",
            reason, result.ai_call_log.latency_ms, user.id, call_log.id,
        )
        details = [{"field": "ai", "reason": reason}]
        if reason == "ai_timeout":
            raise APIError(504, "AI_TIMEOUT", "AI 응답 시간이 초과되었습니다.", details=details)
        raise APIError(502, "AI_PROVIDER_ERROR", "AI 추천 호출에 실패했습니다.", details=details)

    db.add(
        RecommendationLog(
            user_id=user.id,
            ai_call_log_id=call_log.id,
            meal_context={**summary_payload, "meal_timing": meal_timing},
            preferred_category=preferred_category,
            recommendation_summary=f"{meal_timing} 추천 {len(result.recommendations)}건",
            recommended_items=[r.model_dump() for r in result.recommendations],
            caution_text=result.caution_text or DEFAULT_CAUTION,
        )
    )
    db.commit()
    return result, call_log


@router.post("/next-meal", response_model=NextMealResponse)
def next_meal(
    body: NextMealRequest,
    user: CurrentUser,
    db: DB,
    ai: AIClient = Depends(get_ai_client),
) -> NextMealResponse:
    meal_timing = body.meal_timing or _default_meal_timing()
    result, call_log = _call_and_log(
        db, user, ai, body.date, body.preferred_category or DEFAULT_CATEGORY, meal_timing
    )
    return NextMealResponse(
        recommendations=[
            RecommendationItem(**r.model_dump()) for r in result.recommendations
        ],
        caution_text=result.caution_text or DEFAULT_CAUTION,
        ai_call_log_id=call_log.id,
    )


def _menu_v2(db: Session, user: User, body: MenuRequest) -> MenuResponse:
    """이력 기반 엔진 — AI 호출·일일 한도 없음. 노출을 기록하고 그 로그 id 를 돌려준다.

    exceed_flag 는 오늘 남은 칼로리 기준(동반 포함 합산)이지만 v2 에서는 카드를
    alternative 로 빼지 않는다 — 엔진이 예산의 2배까지 이미 잘랐고, 남은 칼로리가 거의
    없는 날 recommended 가 비어 버리면 FE 가 아무것도 못 보여 준다.
    """
    result = recommend_v2(db, user.id, meal_type=body.meal_type, mood=body.mood)
    log = log_exposure(db, user.id, result)  # commit 포함
    b = result.budget
    remaining = body.remaining_calories if body.remaining_calories is not None else b.remaining_today
    category = body.preferred_category or DEFAULT_CATEGORY
    menus = [
        MenuItem(
            name=item.name,
            category=category,
            estimated_calories=item.calories,
            exceed_flag=item.total_calories > remaining,
            reason=item.reason,
            source=item.source,
            budget_label=item.budget_label,
            total_calories=item.total_calories,
            companion_name=item.companion_name,
            companion_calories=item.companion_kcal or None,
            group_id=item.group_id,
            group_name=item.group_name,
            family=item.family,
        )
        for item in result.items
    ]
    return MenuResponse(
        recommended_menus=menus,
        alternative_menus=[],
        caution_text=DEFAULT_CAUTION,
        ai_call_log_id=None,
        engine="v2",
        recommendation_log_id=log.id,
        budget=MenuBudget(
            meal_type=result.meal_type,
            meal_budget=b.meal_budget,
            remaining_today=b.remaining_today,
            goal_calories=b.goal_calories,
            ratio_source=b.ratio_source,
            protein_gap=round(b.protein_gap, 1),
        ),
    )


@router.post("/menu", response_model=MenuResponse)
def menu(
    body: MenuRequest,
    user: CurrentUser,
    db: DB,
    ai: AIClient = Depends(get_ai_client),
) -> MenuResponse:
    if settings.recommend_engine == "v2":
        return _menu_v2(db, user, body)

    today = now_utc().astimezone(KST).date()
    # legacy(AI) 는 snack 을 모른다 — 생략·간식이면 시각 기준 끼니로
    meal_timing = body.meal_type if body.meal_type in ("breakfast", "lunch", "dinner") else None
    result, call_log = _call_and_log(
        db, user, ai, today, body.preferred_category or DEFAULT_CATEGORY,
        meal_timing or _default_meal_timing(),
    )

    # 남은 칼로리 필터: 미지정 시 오늘 요약에서 계산 (FR-LIMIT-002~003)
    remaining = body.remaining_calories
    if remaining is None:
        total = aggregate_day(db, user.id, today)
        goals = get_goals(db, user.id)
        remaining = max(round(goals["calories"] - total["calories"]), 0)

    recommended: list[MenuItem] = []
    alternatives: list[MenuItem] = []
    for r in result.recommendations:
        exceed = r.estimated_calories > remaining
        item = MenuItem(
            name=r.name,
            category=r.category,
            estimated_calories=r.estimated_calories,
            exceed_flag=exceed,
            reason=r.reason,
        )
        (alternatives if exceed else recommended).append(item)

    return MenuResponse(
        recommended_menus=recommended,
        alternative_menus=alternatives,
        caution_text=result.caution_text or DEFAULT_CAUTION,
        ai_call_log_id=call_log.id,
        engine="legacy",
    )


@router.post("/{log_id}/accept", response_model=AcceptResponse)
def accept(log_id: int, body: AcceptRequest, user: CurrentUser, db: DB) -> AcceptResponse:
    """추천 카드 탭(명시 채택) 신고 — v2 노출 로그의 해당 항목에 accepted_at 을 남긴다.

    같은 카드를 다시 눌러도 200 (처음 시각 유지). 남의 로그·없는 항목은 404.
    """
    if not mark_accepted(db, user.id, log_id, body.name):
        raise APIError(404, "NOT_FOUND", "해당 추천 항목을 찾을 수 없습니다.")
    return AcceptResponse(accepted=True)


@router.post("/location-based-menu", response_model=LocationMenuResponse)
def location_based_menu(
    body: LocationMenuRequest,
    user: CurrentUser,
    db: DB,
    ai: AIClient = Depends(get_ai_client),
) -> LocationMenuResponse:
    consent = db.scalar(select(LocationConsent).where(LocationConsent.user_id == user.id))
    if consent is None or not consent.consent_status or consent.revoked_at is not None:
        raise APIError(403, "FORBIDDEN", "위치 정보 동의가 필요합니다.")

    today = now_utc().astimezone(KST).date()
    meal_timing = body.meal_timing or _default_meal_timing()
    result, call_log = _call_and_log(
        db, user, ai, today, body.category or DEFAULT_CATEGORY, meal_timing
    )
    return LocationMenuResponse(
        nearby_recommendations=[
            RecommendationItem(**r.model_dump()) for r in result.recommendations
        ],
        caution_text=result.caution_text or DEFAULT_CAUTION,
        ai_call_log_id=call_log.id,
    )
