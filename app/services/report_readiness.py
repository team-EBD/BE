"""리포트 열람 조건 판정 (SCRUM-275).

리포트(일간·주간·월간)는 **마감된 기간**에 대해 **기록이 충분할 때만** 통계를 보여 준다.
판정은 서버가 하고 앱은 `report_readiness` 필드만 읽는다 — 규칙이 바뀌면 서버만 배포한다.

- "채운 날" = 하루에 식단 기록이 MIN_RECORDS_PER_DAY(2)건 이상인 날. 끼니 생략(is_skipped)
  기록도 '기록했다'는 행동이므로 포함한다 (영양 합계용 meal_count 와 다른 기준 — record_count).
- 마감 = 기간 마지막 날의 하루 경계(settings.day_start_hour, 기본 06:00)가 지난 시점.
  일간은 다음 날 06:00, 주간(일~토)은 일요일 06:00, 월간은 다음 달 1일 06:00.

status
- pending       : 아직 마감되지 않음(오늘·이번 주·이번 달, 또는 미래). 앱은 "마감되면 조건 충족 시 보여 준다" 안내.
- insufficient  : 마감됐지만 조건 미달. 앱은 "데이터가 부족해요" 안내.
- ready         : 마감 + 조건 충족. 앱은 통계를 그대로 표시.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.core.config import settings
from app.core.timeutil import kst_day_bounds, to_kst

MIN_RECORDS_PER_DAY = 2          # 하루를 '채운 날'로 보는 최소 기록 수(생략 포함)
DAILY_REQUIRED_RECORDS = 2       # 일간: 그 날 기록 수
WEEKLY_REQUIRED_FILLED_DAYS = 3  # 주간: 채운 날 수 (일~토 7일 중)
MONTHLY_REQUIRED_FILLED_DAYS = 10  # 월간: 채운 날 수

STATUS_PENDING = "pending"
STATUS_INSUFFICIENT = "insufficient"
STATUS_READY = "ready"


def is_filled_day(total: dict) -> bool:
    """aggregate_day/aggregate_range 의 하루 집계가 '채운 날'인가."""
    return total["record_count"] >= MIN_RECORDS_PER_DAY


def count_filled_days(day_totals: dict[date, dict], start: date, end: date) -> int:
    """[start, end] 안의 채운 날 수. 범위 밖 날짜(day_totals 가 더 넓을 때)는 세지 않는다."""
    return sum(
        1 for day, total in day_totals.items() if start <= day <= end and is_filled_day(total)
    )


def closes_at(period_end: date, day_start_hour: int = settings.day_start_hour) -> str:
    """기간 마감 시각 — 마지막 날의 하루 경계가 끝나는 시각(KST, ISO 8601)."""
    _, end = kst_day_bounds(period_end, day_start_hour)
    return to_kst(end).isoformat()


def readiness(
    period_end: date, today: date, filled: int, required: int,
    day_start_hour: int = settings.day_start_hour,
) -> dict:
    """세 리포트 공통 판정. today 는 논리 날짜(day_start_hour 경계 적용).

    period_end 가 오늘이거나 미래면 아직 마감 전(pending)이다 — 오늘 06:00 전에 보는
    '어제'는 논리 날짜상 아직 오늘이므로 자연히 pending 으로 남는다.
    """
    if period_end >= today:
        status = STATUS_PENDING
    elif filled >= required:
        status = STATUS_READY
    else:
        status = STATUS_INSUFFICIENT
    return {
        "status": status,
        "closes_at": closes_at(period_end, day_start_hour),
        "filled": filled,
        "required": required,
        "min_records_per_day": MIN_RECORDS_PER_DAY,
    }


def daily_readiness(
    day: date, today: date, total: dict, day_start_hour: int = settings.day_start_hour
) -> dict:
    return readiness(day, today, total["record_count"], DAILY_REQUIRED_RECORDS, day_start_hour)


def weekly_readiness(
    week_start: date, today: date, day_totals: dict[date, dict],
    day_start_hour: int = settings.day_start_hour,
) -> dict:
    week_end = week_start + timedelta(days=6)
    filled = count_filled_days(day_totals, week_start, week_end)
    return readiness(week_end, today, filled, WEEKLY_REQUIRED_FILLED_DAYS, day_start_hour)


def monthly_readiness(
    first: date, last: date, today: date, day_totals: dict[date, dict],
    day_start_hour: int = settings.day_start_hour,
) -> dict:
    filled = count_filled_days(day_totals, first, last)
    return readiness(last, today, filled, MONTHLY_REQUIRED_FILLED_DAYS, day_start_hour)
