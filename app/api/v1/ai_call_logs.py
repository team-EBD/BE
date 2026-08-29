"""AI 호출 로그 조회 라우터 (Phase 11, 명세서 6.2 — 운영/디버깅용).

본인 호출 로그만 조회한다. status/task_type/기간 필터 + 페이지네이션.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.deps import DB, CurrentUser
from app.core.pagination import PageParams, Pagination, page_params
from app.core.timeutil import kst_day_bounds
from app.models import AiCallLog
from app.schemas.ai_log import AiCallLogItem, AiCallLogListResponse

router = APIRouter(prefix="/ai-call-logs", tags=["ai-call-logs"])


@router.get("", response_model=AiCallLogListResponse)
def list_ai_call_logs(
    user: CurrentUser,
    db: DB,
    status_: Literal["success", "failed"] | None = Query(default=None, alias="status"),
    task_type: Literal["analyze", "recommend"] | None = Query(default=None),
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    params: PageParams = Depends(page_params),
) -> AiCallLogListResponse:
    conditions = [AiCallLog.user_id == user.id]
    if status_ is not None:
        conditions.append(AiCallLog.status == status_)
    if task_type is not None:
        conditions.append(AiCallLog.task_type == task_type)
    if from_ is not None:
        conditions.append(AiCallLog.created_at >= kst_day_bounds(from_)[0])
    if to is not None:
        conditions.append(AiCallLog.created_at < kst_day_bounds(to)[1])  # to 날짜 포함

    total = db.scalar(select(func.count()).select_from(AiCallLog).where(*conditions)) or 0
    logs = db.scalars(
        select(AiCallLog)
        .where(*conditions)
        .order_by(AiCallLog.id.desc())
        .offset(params.offset)
        .limit(params.limit)
    )

    items = []
    for log in logs:
        token_total = None
        if log.token_input is not None or log.token_output is not None:
            token_total = (log.token_input or 0) + (log.token_output or 0)
        items.append(
            AiCallLogItem(
                id=log.id,
                provider=log.provider,
                model_name=log.model_name,
                task_type=log.task_type,
                status=log.status,
                latency_ms=log.latency_ms,
                error_message=log.error_message,
                token=token_total,
                cost=float(log.cost_estimate) if log.cost_estimate is not None else None,
                created_at=log.created_at,
            )
        )
    return AiCallLogListResponse(items=items, pagination=Pagination.build(params, total))
