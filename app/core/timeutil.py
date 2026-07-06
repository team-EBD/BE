"""시간대 규칙 (명세서 1.6).

- 응답의 일시는 ISO8601 `+09:00`(KST)로 표기한다.
- 저장은 UTC 로 정규화한다. Postgres 는 timestamptz(aware UTC), SQLite 는
  naive UTC 로 저장되므로, 읽을 때 naive 값은 UTC 로 간주한다.
- "하루"의 경계는 KST 기준이다 (예: eaten_at 2026-06-27T00:30+09:00 은 6/27 기록).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """저장용 정규화. naive 입력은 KST 로 간주한다(클라이언트 표기 규칙)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(UTC)


def from_db(dt: datetime) -> datetime:
    """DB 에서 읽은 값의 정규화. naive(SQLite 저장값)는 UTC 로 간주한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_kst(dt: datetime) -> datetime:
    """표현용 변환. naive(SQLite 저장값)는 UTC 로 간주한다."""
    return from_db(dt).astimezone(KST)


def kst_date_of(dt: datetime) -> date:
    return to_kst(dt).date()


def kst_day_bounds(day: date) -> tuple[datetime, datetime]:
    """해당 KST 날짜의 [시작, 끝) UTC 경계."""
    start = datetime.combine(day, time.min, tzinfo=KST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def kst_month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """해당 KST 월의 [시작, 끝) UTC 경계."""
    start = datetime(year, month, 1, tzinfo=KST)
    end = datetime(year + 1, 1, 1, tzinfo=KST) if month == 12 else datetime(year, month + 1, 1, tzinfo=KST)
    return start.astimezone(UTC), end.astimezone(UTC)
