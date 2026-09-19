"""일일 AI 사용량 조회 (FE 가 잔여 횟수를 표시할 수 있게).

GET /v1/usage/daily — 분석/추천 각각의 오늘(KST) 사용량·한도·잔여와 평생 무료 크레딧.
한도가 비활성(0 이하)이면 limit/remaining 은 null (무제한).
플래그가 켜지면 기능별 일일 한도는 없고 무료 크레딧만 통합 적용된다.
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter

from app.core.config import settings
from app.core.deps import DB, CurrentUser
from app.services.subscription import is_premium
from app.services.usage_limit import (
    FREE_CREDIT_LIMIT,
    count_lifetime_success,
    count_today_success,
    daily_limit,
)

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageQuota(BaseModel):
    limit: int | None  # null = 무제한
    used: int
    remaining: int | None


class DailyUsageResponse(BaseModel):
    analyze: UsageQuota
    recommend: UsageQuota
    free_credits: UsageQuota
    # FE 가 "무제한(프리미엄)" 배지와 구독 유도 CTA 중 무엇을 보일지 판단한다
    is_premium: bool = False


def _quota(db, user_id: int, task_type: str, limit: int) -> UsageQuota:
    used = count_today_success(db, user_id, task_type)
    if limit <= 0:
        return UsageQuota(limit=None, used=used, remaining=None)
    return UsageQuota(limit=limit, used=used, remaining=max(limit - used, 0))


@router.get("/daily", response_model=DailyUsageResponse)
def get_daily_usage(user: CurrentUser, db: DB) -> DailyUsageResponse:
    premium = is_premium(db, user.id)
    free_used = count_lifetime_success(db, user.id)
    # 새 정책에는 기능별 일일 한도가 없고, 무료 크레딧만 통합 적용한다.
    analyze_limit = 0 if settings.ai_premium_gate else daily_limit("analyze", premium)
    recommend_limit = 0 if settings.ai_premium_gate else daily_limit("recommend", premium)
    return DailyUsageResponse(
        analyze=_quota(db, user.id, "analyze", analyze_limit),
        recommend=_quota(db, user.id, "recommend", recommend_limit),
        free_credits=UsageQuota(
            limit=FREE_CREDIT_LIMIT,
            used=free_used,
            remaining=max(FREE_CREDIT_LIMIT - free_used, 0),
        ),
        is_premium=premium,
    )
