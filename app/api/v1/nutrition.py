"""영양 요약 라우터 (Phase 7, 명세서 9장). LLM 미사용 (NFR-011)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.core.deps import DB, CurrentUser
from app.services.summary import daily_summary_response, weekly_summary_response

router = APIRouter(prefix="/nutrition", tags=["nutrition"])


@router.get("/daily-summary")
def daily_summary(
    user: CurrentUser, db: DB, date_: date = Query(alias="date")
) -> dict:
    return daily_summary_response(db, user.id, date_)


@router.get("/weekly-summary")
def weekly_summary(
    user: CurrentUser, db: DB, week_start: date = Query()
) -> dict:
    return weekly_summary_response(db, user.id, week_start)
