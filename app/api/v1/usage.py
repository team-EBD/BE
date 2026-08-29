"""일일 AI 사용량 조회 (FE 가 잔여 횟수를 표시할 수 있게).

GET /v1/usage/daily — 분석/추천 각각의 오늘(KST) 사용량·한도·잔여.
한도가 비활성(0 이하)이면 limit/remaining 은 null (무제한).
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter

from app.core.config import settings
from app.core.deps import DB, CurrentUser
from app.services.usage_limit import count_today_success

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageQuota(BaseModel):
    limit: int | None  # null = 무제한
    used: int
    remaining: int | None


class DailyUsageResponse(BaseModel):
    analyze: UsageQuota
    recommend: UsageQuota


def _quota(db, user_id: int, task_type: str, limit: int) -> UsageQuota:
    used = count_today_success(db, user_id, task_type)
    if limit <= 0:
        return UsageQuota(limit=None, used=used, remaining=None)
    return UsageQuota(limit=limit, used=used, remaining=max(limit - used, 0))


@router.get("/daily", response_model=DailyUsageResponse)
def get_daily_usage(user: CurrentUser, db: DB) -> DailyUsageResponse:
    return DailyUsageResponse(
        analyze=_quota(db, user.id, "analyze", settings.analyze_daily_limit),
        recommend=_quota(db, user.id, "recommend", settings.recommend_daily_limit),
    )
