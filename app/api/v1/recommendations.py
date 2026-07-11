"""추천 라우터 (Phase 9·10, 명세서 10장).

- NFR-010: 사용자 명시 호출 시에만. 모든 응답에 caution_text (FR-REC-003).
- BE 가 daily_summary 를 계산해 AI 서버에 전달한다(현행 internal 계약).
- 실패 시 502 AI_PROVIDER_ERROR / 504 AI_TIMEOUT (명세서 10.1).
- 위치 기반(10.3)은 위치 동의 사용자만(403), 좌표는 Body 로만 받는다.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_client import get_ai_client
from app.ai_client.base import AIClient, RecommendResult
from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.timeutil import KST, now_utc
from app.models import AiCallLog, LocationConsent, RecommendationLog, User
from app.schemas.recommendation import (
    LocationMenuRequest,
    LocationMenuResponse,
    MenuItem,
    MenuRequest,
    MenuResponse,
    NextMealRequest,
    NextMealResponse,
    RecommendationItem,
)
from app.services.summary import aggregate_day, get_goals

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

DEFAULT_CATEGORY = "convenience_store"
DEFAULT_CAUTION = "추천은 생활 식단 참고용이며 의학적 조언이 아닙니다."


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


def _call_and_log(
    db: Session,
    user: User,
    ai: AIClient,
    day: date,
    preferred_category: str,
    meal_timing: str,
) -> tuple[RecommendResult, AiCallLog]:
    """AI 추천 호출 + ai_call_logs 기록(성공/실패 예외 없이). 실패 시 5xx 변환."""
    summary_payload = _daily_summary_payload(db, user.id, day)
    result = ai.recommend(summary_payload, preferred_category, meal_timing)

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

    if result.status == "failed":
        db.commit()  # 실패도 로그는 남긴다 (횡단 관심사: AI 로깅)
        if result.reason == "ai_timeout":
            raise APIError(504, "AI_TIMEOUT", "AI 응답 시간이 초과되었습니다.")
        raise APIError(502, "AI_PROVIDER_ERROR", "AI 추천 호출에 실패했습니다.")

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
    meal_timing = body.meal_timing or ("lunch" if now_utc().astimezone(KST).hour < 15 else "dinner")
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


@router.post("/menu", response_model=MenuResponse)
def menu(
    body: MenuRequest,
    user: CurrentUser,
    db: DB,
    ai: AIClient = Depends(get_ai_client),
) -> MenuResponse:
    today = now_utc().astimezone(KST).date()
    result, call_log = _call_and_log(
        db, user, ai, today, body.preferred_category or DEFAULT_CATEGORY, body.meal_type
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
    )


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
    meal_timing = "lunch" if now_utc().astimezone(KST).hour < 15 else "dinner"
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
