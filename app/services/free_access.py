"""전면 무료 기간 (free_access_until).

기간 안에는 구독 여부와 상관없이 AI 분석·추천과 프리미엄 기능을 모두 연다 —
무료 사용권(free_credit_limit)·기능별 일일 한도 대신 남용 방지 상한
(free_access_daily_limit)만 적용한다 (app/services/usage_limit.py).

날짜는 KST 달력 기준이고 그 날 자정(다음 날 00:00)까지 포함한다.
값이 비었거나 날짜로 읽을 수 없으면 무료 기간이 없는 것으로 본다 — 설정 오타로
서버가 기동하지 못하거나 한도가 영영 풀리는 일이 없게 한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from app.core.config import settings
from app.core.timeutil import kst_date_of, now_utc

logger = logging.getLogger("eatlog.free_access")


def free_access_until() -> date | None:
    """무료 기간의 마지막 날(KST, 포함). 설정이 없거나 잘못됐으면 None."""
    raw = (settings.free_access_until or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("FREE_ACCESS_UNTIL 을 날짜로 읽을 수 없어 무시한다: %r", raw)
        return None


def is_free_access_active(now: datetime | None = None) -> bool:
    until = free_access_until()
    if until is None:
        return False
    return kst_date_of(now or now_utc()) <= until


def active_free_access_until(now: datetime | None = None) -> date | None:
    """지금 무료 기간이면 마지막 날, 아니면 None (API 응답용)."""
    return free_access_until() if is_free_access_active(now) else None
