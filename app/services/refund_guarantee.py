"""첫 월 구독 결제 후 30일, 식사 기록 90건 무료 연장 진행도와 신청 접수.

보상은 구독 1개월 무료 연장이다(과거에는 환불로 설계됨).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from math import ceil

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.core.timeutil import from_db, kst_date_of, now_utc
from app.models.billing import RefundGuaranteeClaim, Subscription
from app.models.meal import MealRecord

MONTHLY_PRODUCT_ID = "eatlog_premium_monthly"
TARGET = 90
DAILY_CAP = 3
PERIOD = timedelta(days=30)


def _first_monthly_subscription(db: Session, user_id: int) -> Subscription | None:
    subscriptions = db.scalars(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.product_id == MONTHLY_PRODUCT_ID,
            Subscription.started_at.is_not(None),
            Subscription.status != "pending",
        )
    ).all()
    return min(subscriptions, key=lambda sub: (from_db(sub.started_at), sub.id)) if subscriptions else None


def _recorded_count(db: Session, user_id: int, start: datetime, end: datetime) -> int:
    eaten_ats = db.scalars(
        select(MealRecord.eaten_at).where(
            MealRecord.user_id == user_id,
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
        )
    )
    by_day = Counter(kst_date_of(eaten_at, settings.day_start_hour) for eaten_at in eaten_ats)
    return sum(min(count, DAILY_CAP) for count in by_day.values())


def _claim(db: Session, sub: Subscription, period_start: datetime) -> RefundGuaranteeClaim | None:
    return db.scalar(
        select(RefundGuaranteeClaim).where(
            RefundGuaranteeClaim.subscription_id == sub.id,
            RefundGuaranteeClaim.period_start == period_start,
        )
    )


def progress(db: Session, user_id: int, *, at: datetime | None = None) -> dict:
    now = at or now_utc()
    sub = _first_monthly_subscription(db, user_id)
    if sub is None:
        return {
            "eligible_program": False,
            "recorded": None,
            "target": None,
            "daily_cap": None,
            "period_start": None,
            "period_end": None,
            "days_left": None,
            "achieved": None,
            "claim_status": None,
        }

    start = from_db(sub.started_at)
    end = start + PERIOD
    recorded = _recorded_count(db, user_id, start, min(now, end))
    existing = _claim(db, sub, start)
    return {
        "eligible_program": True,
        "recorded": recorded,
        "target": TARGET,
        "daily_cap": DAILY_CAP,
        "period_start": start,
        "period_end": end,
        "days_left": max(0, ceil((end - now).total_seconds() / 86400)),
        "achieved": recorded >= TARGET,
        "claim_status": existing.status if existing else None,
    }


def _reject(reason: str, message: str) -> None:
    raise APIError(
        409,
        "CONFLICT",
        message,
        details=[{"field": "refund_guarantee", "reason": reason}],
    )


def request_claim(db: Session, user_id: int) -> dict:
    now = now_utc()
    sub = _first_monthly_subscription(db, user_id)
    if sub is None:
        _reject("not_eligible_program", "이 결제는 무료 연장 신청 대상이 아닙니다.")

    start = from_db(sub.started_at)
    end = start + PERIOD
    if _claim(db, sub, start) is not None:
        _reject("already_claimed", "이미 무료 연장을 신청했습니다.")
    if sub.status == "revoked":
        _reject("not_eligible_program", "이 결제는 무료 연장 신청 대상이 아닙니다.")
    if now >= end:
        _reject("period_expired", "무료 연장 신청 기간이 지났습니다.")

    recorded = _recorded_count(db, user_id, start, now)
    if recorded < TARGET:
        _reject("not_enough_records", "인정된 식사 기록이 90건 미만입니다.")

    db.add(
        RefundGuaranteeClaim(
            user_id=user_id,
            subscription_id=sub.id,
            period_start=start,
            period_end=end,
            recorded_count=recorded,
            status="requested",
            requested_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if _claim(db, sub, start) is not None:
            _reject("already_claimed", "이미 무료 연장을 신청했습니다.")
        raise
    return progress(db, user_id, at=now)
