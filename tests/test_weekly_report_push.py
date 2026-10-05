"""주간 리포트 도착 푸시 — 발송 시점·직전 주 계산·기록 일수별 개인화·토큰 정리.

리포트 열람 조건(SCRUM-275): 직전 주에 2끼 이상 기록한 날이 3일 미만이면 리포트가
열리지 않으므로 푸시도 보내지 않는다. 아래 '발송되는' 사용자는 모두 그 조건을 채운다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.timeutil import KST, to_utc
from app.models import MealRecord, NotificationSetting, PushToken, User
from app.push_client.base import PushSendReport
from app.push_client.mock import MockPushClient
from app.services.weekly_report_push import (
    build_push_content,
    last_completed_week_start,
    recorded_days_in_week,
    send_weekly_report_push,
    week_record_stats,
    weekly_push_due,
    weekly_report_push_recipients,
)

# 2026-07-26 은 일요일 (직전 완결 주: 07-19(일) ~ 07-25(토))
_SUNDAY_0900 = datetime(2026, 7, 26, 9, 0, tzinfo=KST)


class InvalidatingPushClient:
    """지정한 토큰을 등록 해제 상태로 보고하는 대역."""

    def __init__(self, invalid: list[str]) -> None:
        self.invalid = invalid

    def send(self, tokens, title, body, data) -> PushSendReport:
        bad = [t for t in tokens if t in self.invalid]
        return PushSendReport(
            success_count=len(tokens) - len(bad),
            failure_count=len(bad),
            invalid_tokens=bad,
        )


def test_due_only_on_configured_weekday():
    assert weekly_push_due(_SUNDAY_0900, "sun", "09:00", already_sent_today=False)
    assert not weekly_push_due(_SUNDAY_0900, "mon", "09:00", already_sent_today=False)


def test_due_after_configured_time_not_before():
    before = _SUNDAY_0900.replace(hour=8, minute=59)
    assert not weekly_push_due(before, "sun", "09:00", already_sent_today=False)
    # 정확한 시각을 지나쳤어도(루프 지연·재기동) 그날 안이면 발송해야 한다
    late = _SUNDAY_0900.replace(hour=13, minute=30)
    assert weekly_push_due(late, "sun", "09:00", already_sent_today=False)


def test_due_dedupes_same_day_and_rejects_bad_day_config():
    assert not weekly_push_due(_SUNDAY_0900, "sun", "09:00", already_sent_today=True)
    assert not weekly_push_due(_SUNDAY_0900, "sunday?", "09:00", already_sent_today=False)


def test_last_completed_week_start_is_previous_sunday():
    # 일요일 발송 → 직전 완결 주는 지난 일요일부터
    assert last_completed_week_start(_SUNDAY_0900) == date(2026, 7, 19)
    # 월요일 기준 — 어제 시작된 주는 진행 중이므로 그 전 주가 완결 주
    monday = datetime(2026, 7, 20, 9, 0, tzinfo=KST)
    assert last_completed_week_start(monday) == date(2026, 7, 12)


def test_push_content_varies_by_recorded_days():
    title0, body0 = build_push_content(0)
    assert "다시 시작" in title0 and "기록이 없었어요" in body0
    for days, keyword in [(1, "조금 더 자주"), (4, "꾸준함"), (7, "최고예요")]:
        title, body = build_push_content(days)
        assert title == "주간 리포트가 도착했어요"
        assert f"{days}일" in body and keyword in body


def _make_user(db, social_id: str) -> User:
    user = User(social_provider="google", social_id=social_id, nickname=f"u-{social_id}")
    db.add(user)
    db.commit()
    return user


def _add_token(db, user_id: int, token: str) -> None:
    db.add(
        PushToken(
            user_id=user_id, device_id=f"dev-{token}", push_token=token, platform="android"
        )
    )
    db.commit()


def _add_meal(db, user_id: int, eaten_at: datetime, *, is_skipped: bool = False) -> None:
    db.add(
        MealRecord(
            user_id=user_id,
            meal_type="lunch",
            eaten_at=eaten_at,
            total_calories=0 if is_skipped else 500,
            is_skipped=is_skipped,
        )
    )
    db.commit()


def _fill_day(db, user_id: int, day: date, *, hours=(8, 12)) -> None:
    """하루를 '채운 날'(기록 2건)로 만든다."""
    for hour in hours:
        _add_meal(db, user_id, datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST))


def test_recipients_respect_notification_settings(db_factory):
    db = db_factory()

    no_setting = _make_user(db, "no-setting")
    _add_token(db, no_setting.id, "t-default")

    enabled = _make_user(db, "enabled")
    _add_token(db, enabled.id, "t-enabled")
    db.add(NotificationSetting(user_id=enabled.id, is_enabled=True, weekly_report_enabled=True))

    weekly_off = _make_user(db, "weekly-off")
    _add_token(db, weekly_off.id, "t-weekly-off")
    db.add(NotificationSetting(user_id=weekly_off.id, is_enabled=True, weekly_report_enabled=False))

    all_off = _make_user(db, "all-off")
    _add_token(db, all_off.id, "t-all-off")
    db.add(NotificationSetting(user_id=all_off.id, is_enabled=False, weekly_report_enabled=True))

    _make_user(db, "no-token")  # 토큰 없는 사용자는 자연히 제외
    db.commit()

    # 설정 없음(기본 켬)·명시적 켬 사용자만 대상
    recipients = weekly_report_push_recipients(db)
    assert set(recipients) == {no_setting.id, enabled.id}
    assert recipients[no_setting.id] == ["t-default"]
    db.close()


def test_send_personalizes_body_per_user(db_factory):
    db = db_factory()

    # 지난주(07-19~25) 3일을 2끼씩 채운 사용자(리포트 열림) vs 하루 1끼씩 3일 기록한 사용자(조건 미달)
    active = _make_user(db, "active")
    _add_token(db, active.id, "t-active")
    for day in (19, 21, 23):
        _fill_day(db, active.id, date(2026, 7, day))
    # 이번 주(진행 중) 기록은 직전 완결 주 집계에 포함되면 안 된다
    _add_meal(db, active.id, datetime(2026, 7, 26, 8, 0, tzinfo=KST))

    light = _make_user(db, "light")
    _add_token(db, light.id, "t-light")
    for day in (19, 21, 23):
        _add_meal(db, light.id, datetime(2026, 7, day, 12, 0, tzinfo=KST))

    silent = _make_user(db, "silent")
    _add_token(db, silent.id, "t-silent")

    client = MockPushClient()
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 1
    assert [m["tokens"] for m in client.sent] == [["t-active"]]  # 리포트가 열리는 사용자만
    assert "3일" in client.sent[0]["body"]
    assert client.sent[0]["data"] == {"type": "weekly_report"}
    db.close()


def test_skipped_meals_count_toward_report_readiness(db_factory):
    """끼니 '건너뜀' 체크도 기록으로 세어 채운 날이 된다."""
    db = db_factory()
    user = _make_user(db, "skipper")
    _add_token(db, user.id, "t-skipper")
    for day in (20, 22, 24):
        _add_meal(db, user.id, datetime(2026, 7, day, 8, 0, tzinfo=KST), is_skipped=True)
        _add_meal(db, user.id, datetime(2026, 7, day, 12, 0, tzinfo=KST))

    recorded_days, report_ready = week_record_stats(db, user.id, date(2026, 7, 19))
    assert (recorded_days, report_ready) == (3, True)
    client = MockPushClient()
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 1
    db.close()


def test_early_sunday_meal_counts_in_previous_week_push(db_factory):
    db = db_factory()
    user = _make_user(db, "early-sunday")
    _add_token(db, user.id, "t-early-sunday")
    _fill_day(db, user.id, date(2026, 7, 20))
    _fill_day(db, user.id, date(2026, 7, 22))
    # 일요일 03:00 KST는 논리 날짜상 직전 토요일(07-25)이다 — 토요일 저녁 기록과 합쳐 3일째 '채운 날'
    _add_meal(db, user.id, datetime(2026, 7, 25, 19, 0, tzinfo=KST))
    _add_meal(db, user.id, to_utc(datetime(2026, 7, 26, 3, 0, tzinfo=KST)))

    client = MockPushClient()
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 1
    assert "지난주 3일 기록" in client.sent[0]["body"]
    db.close()


def test_weekly_record_days_exclude_previous_sunday_early_meal(db_factory):
    db = db_factory()
    user = _make_user(db, "week-boundary")
    week_start = date(2026, 7, 19)
    # 주 시작 일요일 03:00은 직전 주 토요일, 토요일 03:00은 이번 주 금요일이다.
    _add_meal(db, user.id, to_utc(datetime(2026, 7, 19, 3, 0, tzinfo=KST)))
    _add_meal(db, user.id, to_utc(datetime(2026, 7, 25, 3, 0, tzinfo=KST)))

    assert recorded_days_in_week(db, user.id, week_start) == 1
    db.close()


def test_send_without_recipients_is_noop(db_factory):
    db = db_factory()
    client = MockPushClient()
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 0
    assert client.sent == []
    db.close()


def test_send_purges_unregistered_tokens(db_factory):
    db = db_factory()
    user = _make_user(db, "purge")
    _add_token(db, user.id, "t-live")
    _add_token(db, user.id, "t-dead")
    for day in (19, 20, 21):
        _fill_day(db, user.id, date(2026, 7, day))

    client = InvalidatingPushClient(invalid=["t-dead"])
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 1

    remaining = db.scalars(select(PushToken.push_token)).all()
    assert remaining == ["t-live"]
    db.close()
