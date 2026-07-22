"""주간 리포트 도착 푸시 발송 (SCRUM-65).

정책: 매주 설정된 요일·시각(KST, 기본 일요일 09:00 — 리포트 주 단위가 일~토라
직전 주가 토요일 밤에 완결된 직후)에 사용자별로 직전 완결 주의 기록 일수를
집계해 **개인화된 문구**로 푸시를 보낸다. 이 개인화(서버만 아는 DB 집계값을
알림 본문에 싣는 것)가 로컬 알림 대신 서버 발송을 쓰는 이유다.

대상은 푸시 토큰이 등록된 사용자 중 알림 설정에서 전체 알림(is_enabled)과
주간 리포트 알림(weekly_report_enabled)이 켜진 사용자다. 설정 행이 없는
사용자는 기본값(둘 다 켬, api/v1/users.py `_notification_response`)으로 간주한다.

트리거: main.py lifespan 의 분 단위 체크 루프. 발송 여부는 프로세스 메모리로
중복 방지하므로, 발송 시각 이후 같은 날 재기동하면 한 번 더 나갈 수 있다
(주 1회 알림이라 허용 — 정확히 1회가 필요해지면 발송 이력 테이블로 승격).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc, to_kst
from app.models import NotificationSetting, PushToken
from app.push_client.base import PushClient
from app.services.summary import aggregate_range

logger = logging.getLogger("eatlog.weekly_report_push")

# FE 수신 핸들러가 알림 탭 시 이동할 화면을 정하는 라우팅 키
PUSH_DATA = {"type": "weekly_report"}

# 요일 문자열(mon~sun) → datetime.weekday() 값
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekly_push_due(
    now: datetime, day: str, at: str, already_sent_today: bool
) -> bool:
    """지금이 발송 시점인지 판단 — 설정 요일이고 설정 시각(KST)을 지났으며 오늘 미발송."""
    if already_sent_today:
        return False
    weekday = _WEEKDAYS.get(day.strip().lower())
    if weekday is None:
        logger.warning("weekly_report_push_day 설정이 올바르지 않습니다: %r", day)
        return False
    now_kst = to_kst(now)
    if now_kst.weekday() != weekday:
        return False
    hour, minute = (int(part) for part in at.split(":"))
    return (now_kst.hour, now_kst.minute) >= (hour, minute)


def last_completed_week_start(now: datetime) -> date:
    """직전 완결 주(KST, 일~토 — FE toWeekStartKey 와 동일 기준)의 시작 일요일."""
    today = to_kst(now).date()
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday + 7)


def build_push_content(recorded_days: int) -> tuple[str, str]:
    """기록 일수 구간별 (제목, 본문). 기록 없는 주도 재시작 유도로 보낸다."""
    if recorded_days == 0:
        return (
            "이번 주엔 다시 시작해요",
            "지난주엔 식사 기록이 없었어요. 가벼운 한 끼부터 다시 시작해 볼까요?",
        )
    if recorded_days <= 2:
        return (
            "주간 리포트가 도착했어요",
            f"지난주 {recorded_days}일 기록했어요. 이번 주엔 조금 더 자주 남겨 볼까요? "
            "리포트에서 지난주 식단을 확인해 보세요.",
        )
    if recorded_days <= 5:
        return (
            "주간 리포트가 도착했어요",
            f"지난주 {recorded_days}일 기록 — 꾸준함이 붙고 있어요. "
            "주간 리포트에서 한 주를 돌아보세요.",
        )
    return (
        "주간 리포트가 도착했어요",
        f"지난주 {recorded_days}일이나 기록했어요, 최고예요! "
        "주간 리포트로 한 주 식단을 정리해 보세요.",
    )


def weekly_report_push_recipients(db: Session) -> dict[int, list[str]]:
    """발송 대상 — 주간 리포트 알림이 켜진(또는 미설정=기본 켬) 사용자의 user_id→토큰들."""
    rows = db.execute(
        select(PushToken, NotificationSetting).outerjoin(
            NotificationSetting, NotificationSetting.user_id == PushToken.user_id
        )
    ).all()
    recipients: dict[int, list[str]] = {}
    for token, setting in rows:
        if setting is None or (setting.is_enabled and setting.weekly_report_enabled):
            recipients.setdefault(token.user_id, []).append(token.push_token)
    return recipients


def recorded_days_in_week(db: Session, user_id: int, week_start: date) -> int:
    """해당 주(일~토)에 식사 기록이 있는 날 수 — 리포트 화면의 recorded_days 와 동일 기준."""
    day_totals = aggregate_range(db, user_id, week_start, week_start + timedelta(days=6))
    return sum(1 for total in day_totals.values() if total["meal_count"] > 0)


def send_weekly_report_push(
    db: Session, client: PushClient, now: datetime | None = None
) -> int:
    """사용자별 개인화 문구로 주간 리포트 푸시를 발송하고 성공 건수를 반환한다.

    무효(등록 해제) 토큰은 발송 결과를 모아 DB 에서 정리한다.
    """
    recipients = weekly_report_push_recipients(db)
    if not recipients:
        logger.info("주간 리포트 푸시 대상 없음")
        return 0

    week_start = last_completed_week_start(now or now_utc())
    success = failure = 0
    invalid_tokens: list[str] = []

    for user_id, tokens in recipients.items():
        recorded_days = recorded_days_in_week(db, user_id, week_start)
        title, body = build_push_content(recorded_days)
        report = client.send(tokens, title, body, PUSH_DATA)
        success += report.success_count
        failure += report.failure_count
        invalid_tokens.extend(report.invalid_tokens)

    if invalid_tokens:
        stale = db.scalars(
            select(PushToken).where(PushToken.push_token.in_(invalid_tokens))
        ).all()
        for row in stale:
            db.delete(row)
        db.commit()
        logger.info("등록 해제된 푸시 토큰 %d건 정리", len(stale))

    logger.info(
        "주간 리포트 푸시 발송: 성공 %d / 실패 %d (사용자 %d명, week_start=%s)",
        success,
        failure,
        len(recipients),
        week_start.isoformat(),
    )
    return success
