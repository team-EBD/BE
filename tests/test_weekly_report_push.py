"""주간 리포트 도착 푸시 — 발송 시점·직전 주 계산·기록 일수별 개인화·토큰 정리."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.timeutil import KST
from app.models import MealRecord, NotificationSetting, PushToken, User
from app.push_client.base import PushSendReport
from app.push_client.mock import MockPushClient
from app.services.weekly_report_push import (
    build_push_content,
    last_completed_week_start,
    send_weekly_report_push,
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


def _add_meal(db, user_id: int, eaten_at: datetime) -> None:
    db.add(
        MealRecord(
            user_id=user_id,
            meal_type="lunch",
            eaten_at=eaten_at,
            total_calories=500,
        )
    )
    db.commit()


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

    # 지난주(07-19~25) 3일 기록한 사용자 vs 기록 없는 사용자
    active = _make_user(db, "active")
    _add_token(db, active.id, "t-active")
    for day in (19, 21, 23):
        _add_meal(db, active.id, datetime(2026, 7, day, 12, 0, tzinfo=KST))
    # 이번 주(진행 중) 기록은 직전 완결 주 집계에 포함되면 안 된다
    _add_meal(db, active.id, datetime(2026, 7, 26, 8, 0, tzinfo=KST))

    silent = _make_user(db, "silent")
    _add_token(db, silent.id, "t-silent")

    client = MockPushClient()
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 2
    assert len(client.sent) == 2  # 사용자별 개별 발송

    by_token = {m["tokens"][0]: m for m in client.sent}
    assert "3일" in by_token["t-active"]["body"]
    assert "기록이 없었어요" in by_token["t-silent"]["body"]
    assert all(m["data"] == {"type": "weekly_report"} for m in client.sent)
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

    client = InvalidatingPushClient(invalid=["t-dead"])
    assert send_weekly_report_push(db, client, now=_SUNDAY_0900) == 1

    remaining = db.scalars(select(PushToken.push_token)).all()
    assert remaining == ["t-live"]
    db.close()
