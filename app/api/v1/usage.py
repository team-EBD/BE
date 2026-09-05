"""일일 AI 사용량 조회 (FE 가 잔여 횟수를 표시할 수 있게).

GET /v1/usage/daily — 분석/추천 각각의 오늘(KST) 사용량·한도·잔여.
한도가 비활성(0 이하)이면 limit/remaining 은 null (무제한).
프리미엄 구독자는 구독자용 한도가 적용된다(기본 무제한).
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter

from app.core.deps import DB, CurrentUser
from app.services.subscription import is_premium
from app.services.usage_limit import count_today_success, daily_limit

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageQuota(BaseModel):
    limit: int | None  # null = 무제한
    used: int
    remaining: int | None


class DailyUsageResponse(BaseModel):
    analyze: UsageQuota
    recommend: UsageQuota
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
    return DailyUsageResponse(
        analyze=_quota(db, user.id, "analyze", daily_limit("analyze", premium)),
        recommend=_quota(db, user.id, "recommend", daily_limit("recommend", premium)),
        is_premium=premium,
    )
