"""영양 요약 라우터 (Phase 7, 명세서 9장). LLM 미사용 (NFR-011)."""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.core.deps import DB, CurrentUser
from app.services.summary import (
    daily_summary_response,
    monthly_summary_response,
    weekly_summary_response,
)

router = APIRouter(prefix="/nutrition", tags=["nutrition"])

_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/daily-summary")
def daily_summary(
    user: CurrentUser,
    db: DB,
    date_: date = Query(alias="date"),
    # 하루 경계 시각 (0=자정, 6=06시~다음날 06시를 한 날로). 홈 화면이 6 을 쓴다.
    day_start_hour: int = Query(default=0, ge=0, le=12),
) -> dict:
    return daily_summary_response(db, user.id, date_, day_start_hour)


@router.get("/weekly-summary")
def weekly_summary(
    user: CurrentUser, db: DB, week_start: date = Query()
) -> dict:
    return weekly_summary_response(db, user.id, week_start)


@router.get("/monthly-summary")
def monthly_summary(
    user: CurrentUser, db: DB, month: str = Query(description="YYYY-MM")
) -> dict:
    # 전역 핸들러가 요청 검증 오류를 400 으로 매핑하므로,
    # 계약(422)을 지키기 위해 형식 검증을 라우터에서 직접 수행한다.
    if not _MONTH_PATTERN.fullmatch(month):
        raise HTTPException(status_code=422, detail="month 는 YYYY-MM 형식이어야 합니다.")
    year, month_num = (int(part) for part in month.split("-"))
    return monthly_summary_response(db, user.id, year, month_num)
