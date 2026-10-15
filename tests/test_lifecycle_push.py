"""기록 습관 라이프사이클 푸시 + 발송 이력(push_send_logs) 중복 방지."""
from __future__ import annotations

import importlib.util
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.core.timeutil import KST, to_utc
from app.models import MealRecord, NotificationSetting, PushSendLog, PushToken, User
from app.models.push import PUSH_CHANNEL_LIFECYCLE, PUSH_CHANNEL_WEEKLY_REPORT
from app.push_client.base import PushSendReport
from app.push_client.mock import MockPushClient
from app.services import lifecycle_push
from app.services.lifecycle_push import (
    PUSH_DATA,
    SLOT_DINNER,
    SLOT_LUNCH,
    TEMPLATES,
    choose_template,
    classify_segment,
    in_quiet_hours,
    lifecycle_push_recipients,
    lifecycle_slot_due,
    send_lifecycle_push,
    streak_if_logged_today,
)
from app.services.push_send_log import claim_push_send
from app.services.weekly_report_push import send_weekly_report_push

TODAY = date(2026, 10, 15)
LUNCH = datetime(2026, 10, 15, 12, 0, tzinfo=KST)
DINNER = datetime(2026, 10, 15, 18, 40, tzinfo=KST)
LONG_AGO = datetime(2026, 9, 1, 9, 0, tzinfo=KST)


# --- 헬퍼 ---

def _user(db, sid: str, *, created_at: datetime = LONG_AGO, token: bool = True, test_device=None) -> User:
    user = User(
        social_provider="google",
        social_id=sid,
        nickname=f"u-{sid}",
        created_at=to_utc(created_at),
        is_test_device=test_device,
    )
    db.add(user)
    db.commit()
    if token:
        db.add(PushToken(user_id=user.id, device_id=f"dev-{sid}", push_token=f"t-{sid}", platform="android"))
        db.commit()
    return user


def _meal(db, user_id: int, day: date, hour: int = 12, *, skipped: bool = False) -> None:
    db.add(
        MealRecord(
            user_id=user_id,
            meal_type="lunch",
            eaten_at=to_utc(datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST)),
            total_calories=0 if skipped else 500,
            is_skipped=skipped,
        )
    )
    db.commit()


def _log(db, user_id: int, day: date, *, kind: str, template_id: str, channel=PUSH_CHANNEL_LIFECYCLE) -> None:
    db.add(
        PushSendLog(
            user_id=user_id,
            channel=channel,
            kind=kind,
            template_id=template_id,
            logical_date=day,
            sent_at=to_utc(datetime(day.year, day.month, day.day, 12, 0, tzinfo=KST)),
        )
    )
    db.commit()


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def _sent_tokens(client: MockPushClient) -> list[str]:
    return [token for message in client.sent for token in message["tokens"]]


# --- 세그먼트 분류 ---

@pytest.mark.parametrize(
    ("days", "expected"),
    [
        ({_ago(1)}, "keep_going"),
        ({_ago(1), _ago(2), _ago(3)}, "keep_going"),
        ({_ago(2)}, "missed_one"),
        ({_ago(2), _ago(5)}, "missed_one"),
        ({_ago(3)}, "lapsed"),
        ({_ago(13)}, "lapsed"),
        ({_ago(14)}, None),
        ({TODAY}, None),
        ({TODAY, _ago(2)}, None),
        ({TODAY + timedelta(days=1)}, None),  # 미래 날짜 기록만 있음
    ],
)
def test_classify_by_last_log_gap(days, expected):
    seg = classify_segment(
        days, TODAY, has_any_record=True, signed_up_at=LONG_AGO, now=LUNCH
    )
    assert seg == expected


def test_classify_returns_none_when_only_old_records_outside_lookback():
    assert classify_segment(set(), TODAY, has_any_record=True, signed_up_at=LONG_AGO, now=LUNCH) is None


@pytest.mark.parametrize(
    ("signed_up_at", "expected"),
    [
        (datetime(2026, 10, 15, 8, 0, tzinfo=KST), "first_log"),  # 오늘 아침 가입
        (datetime(2026, 10, 15, 10, 30, tzinfo=KST), None),  # 가입 2시간 안 — 아직 보내지 않는다
        (datetime(2026, 10, 9, 12, 0, tzinfo=KST), "first_log"),  # 6일 전
        (datetime(2026, 10, 8, 12, 0, tzinfo=KST), None),  # 7일 전 — 창 밖
        (LONG_AGO, None),
    ],
)
def test_classify_first_log_window(signed_up_at, expected):
    assert classify_segment(
        set(), TODAY, has_any_record=False, signed_up_at=signed_up_at, now=LUNCH
    ) == expected


def test_streak_if_logged_today_counts_today_in():
    assert streak_if_logged_today({_ago(1)}, TODAY) == 2
    assert streak_if_logged_today({_ago(1), _ago(2), _ago(3), _ago(5)}, TODAY) == 4


# --- 시각 ---

@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [(7, 59, True), (8, 0, False), (12, 0, False), (20, 59, False), (21, 0, True), (23, 30, True), (2, 0, True)],
)
def test_quiet_hours_boundaries(hour, minute, quiet):
    assert in_quiet_hours(datetime(2026, 10, 15, hour, minute, tzinfo=KST)) is quiet


def test_slot_due_after_slot_until_quiet_hours():
    at = "11:50"
    assert not lifecycle_slot_due(LUNCH.replace(hour=11, minute=49), at)
    assert lifecycle_slot_due(LUNCH.replace(hour=11, minute=50), at)
    assert lifecycle_slot_due(LUNCH.replace(hour=20, minute=59), at)
    assert not lifecycle_slot_due(LUNCH.replace(hour=21, minute=0), at)
    assert not lifecycle_slot_due(LUNCH, "noon")


def test_slot_due_respects_slot_end():
    # 늦은 재기동으로 점심 문구가 저녁에 한꺼번에 나가지 않는다 (점심 15시, 저녁 20:30 마감)
    assert lifecycle_slot_due(LUNCH.replace(hour=14, minute=59), "11:50", SLOT_LUNCH)
    assert not lifecycle_slot_due(LUNCH.replace(hour=15, minute=0), "11:50", SLOT_LUNCH)
    assert lifecycle_slot_due(LUNCH.replace(hour=20, minute=29), "18:30", SLOT_DINNER)
    assert not lifecycle_slot_due(LUNCH.replace(hour=20, minute=30), "18:30", SLOT_DINNER)


def test_send_stops_when_quiet_hours_reached_mid_run(db_factory, monkeypatch):
    db = db_factory()
    for sid in ("q1", "q2", "q3"):
        user = _user(db, sid)
        _meal(db, user.id, _ago(2))
    # 첫 사용자 뒤로 시계가 9시간 흐른 것처럼 — 12:00 + 9h = 21:00 이 되면 멈춘다
    ticks = iter([0.0, 0.0, 9 * 3600.0, 9 * 3600.0, 9 * 3600.0])
    monkeypatch.setattr(lifecycle_push.time, "monotonic", lambda: next(ticks, 9 * 3600.0))
    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert len(client.sent) == 1
    assert summary.skipped["quiet_hours_reached"] == 1
    db.close()


class _Exploding(MockPushClient):
    def send(self, tokens, title, body, data=None):
        if "t-boom" in tokens:
            raise RuntimeError("fcm down")
        return super().send(tokens, title, body, data)


def test_send_exception_for_one_user_keeps_going(db_factory):
    db = db_factory()
    for sid in ("boom", "fine"):
        user = _user(db, sid)
        _meal(db, user.id, _ago(2))
    client = _Exploding()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert _sent_tokens(client) == ["t-fine"]
    assert summary.failure == 1
    db.close()


def test_no_send_during_quiet_hours(db_factory):
    db = db_factory()
    user = _user(db, "night")
    _meal(db, user.id, _ago(2))
    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=datetime(2026, 10, 15, 21, 5, tzinfo=KST))
    assert client.sent == [] and sum(summary.sent.values()) == 0
    assert db.scalars(select(PushSendLog)).all() == []
    db.close()


# --- 대상 ---

def test_recipients_exclude_disabled_and_local_reminders(db_factory):
    db = db_factory()
    default = _user(db, "default")
    on = _user(db, "on")
    db.add(NotificationSetting(user_id=on.id, is_enabled=True))
    off = _user(db, "off")
    db.add(NotificationSetting(user_id=off.id, is_enabled=False))
    lunch_local = _user(db, "lunch-local")
    db.add(NotificationSetting(user_id=lunch_local.id, is_enabled=True, lunch_time="12:00"))
    dinner_local = _user(db, "dinner-local")
    db.add(NotificationSetting(user_id=dinner_local.id, is_enabled=True, dinner_time="19:00"))
    _user(db, "no-token", token=False)
    db.commit()

    assert set(lifecycle_push_recipients(db)) == {default.id, on.id}
    db.close()


@pytest.mark.parametrize(
    ("lunch", "dinner", "skipped"),
    [
        ("09:00", None, True),
        ("15:59", None, True),
        ("01:00", None, False),  # 24시간 피커 실수 — 식사 알림으로 치지 않는다
        ("08:59", None, False),
        ("16:00", None, False),
        (None, "15:00", True),
        (None, "23:30", True),
        (None, "00:30", False),
        (None, "06:30", False),
        ("01:00", "02:00", False),  # 둘 다 엉뚱하면 라이프사이클 푸시를 받는다
        ("01:00", "19:00", True),  # 하나라도 그럴듯하면 기기가 알려 준다
        ("bad", None, False),
    ],
)
def test_only_plausible_local_reminders_exclude_user(db_factory, lunch, dinner, skipped):
    db = db_factory()
    user = _user(db, "reminder")
    db.add(NotificationSetting(user_id=user.id, is_enabled=True, lunch_time=lunch, dinner_time=dinner))
    db.commit()
    assert (user.id not in lifecycle_push_recipients(db)) is skipped
    db.close()


def test_implausible_reminder_user_still_gets_push(db_factory):
    db = db_factory()
    user = _user(db, "midnight-dinner")
    _meal(db, user.id, _ago(2))
    db.add(NotificationSetting(user_id=user.id, is_enabled=True, lunch_time="01:00", dinner_time="00:30"))
    db.commit()
    client = MockPushClient()
    send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert _sent_tokens(client) == ["t-midnight-dinner"]
    db.close()


def test_send_segments_per_slot(db_factory):
    db = db_factory()
    newbie = _user(db, "newbie", created_at=datetime(2026, 10, 13, 9, 0, tzinfo=KST))
    missed = _user(db, "missed")
    _meal(db, missed.id, _ago(2))
    going = _user(db, "going")
    _meal(db, going.id, _ago(1))
    _meal(db, going.id, _ago(2))
    lapsed = _user(db, "lapsed")
    _meal(db, lapsed.id, _ago(6))
    dormant = _user(db, "dormant")
    _meal(db, dormant.id, _ago(20))
    old_newbie = _user(db, "old-newbie", created_at=datetime(2026, 10, 1, 9, 0, tzinfo=KST))
    today_logger = _user(db, "today")
    _meal(db, today_logger.id, TODAY, hour=9)
    _meal(db, today_logger.id, _ago(2))
    robot = _user(db, "robot", test_device=True, created_at=datetime(2026, 10, 14, 9, 0, tzinfo=KST))

    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert sorted(_sent_tokens(client)) == ["t-lapsed", "t-missed", "t-newbie"]
    assert dict(summary.sent) == {"first_log": 1, "missed_one": 1, "lapsed": 1}
    assert summary.skipped["logged_today"] == 1
    assert summary.skipped["other_slot"] == 1  # keep_going 은 저녁 슬롯
    assert summary.skipped["no_segment"] == 2  # dormant, old_newbie
    assert summary.skipped["test_device"] == 1
    assert all(m["data"] == PUSH_DATA for m in client.sent)

    dinner = MockPushClient()
    summary = send_lifecycle_push(db, dinner, SLOT_DINNER, now=DINNER)
    assert _sent_tokens(dinner) == ["t-going"]
    assert "3일 연속" in dinner.sent[0]["body"]
    assert summary.skipped["other_slot"] == 3  # 점심 세그먼트는 저녁 슬롯 대상이 아니다

    rows = db.execute(select(PushSendLog.user_id, PushSendLog.kind, PushSendLog.logical_date)).all()
    assert {(r.user_id, r.kind) for r in rows} == {
        (newbie.id, "first_log"), (missed.id, "missed_one"), (lapsed.id, "lapsed"), (going.id, "keep_going"),
    }
    assert {r.logical_date for r in rows} == {TODAY}
    assert dormant.id and old_newbie.id and robot.id
    db.close()


def test_skipped_meal_today_does_not_count_as_logged(db_factory):
    db = db_factory()
    user = _user(db, "skip-only")
    _meal(db, user.id, _ago(2))
    _meal(db, user.id, TODAY, hour=8, skipped=True)
    client = MockPushClient()
    send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert _sent_tokens(client) == ["t-skip-only"]
    db.close()


def test_skipped_only_history_counts_as_never_logged(db_factory):
    db = db_factory()
    user = _user(db, "skip-history", created_at=datetime(2026, 10, 14, 9, 0, tzinfo=KST))
    _meal(db, user.id, _ago(1), skipped=True)
    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert dict(summary.sent) == {"first_log": 1}
    db.close()


def test_users_with_local_reminders_or_disabled_get_nothing(db_factory):
    db = db_factory()
    local = _user(db, "local")
    _meal(db, local.id, _ago(2))
    db.add(NotificationSetting(user_id=local.id, is_enabled=True, lunch_time="12:00"))
    off = _user(db, "off")
    _meal(db, off.id, _ago(2))
    db.add(NotificationSetting(user_id=off.id, is_enabled=False))
    db.commit()
    client = MockPushClient()
    send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert client.sent == []
    db.close()


# --- 하루 1건 ---

def test_second_run_same_day_sends_nothing(db_factory):
    db = db_factory()
    user = _user(db, "twice")
    _meal(db, user.id, _ago(2))
    first, second = MockPushClient(), MockPushClient()
    send_lifecycle_push(db, first, SLOT_LUNCH, now=LUNCH)
    summary = send_lifecycle_push(db, second, SLOT_LUNCH, now=LUNCH + timedelta(minutes=5))
    assert len(first.sent) == 1 and second.sent == []
    assert summary.skipped["already_sent"] == 1
    db.close()


def test_claim_push_send_integrity_error_returns_false(db_factory):
    db = db_factory()
    user = _user(db, "claim")
    kwargs = dict(user_id=user.id, channel=PUSH_CHANNEL_LIFECYCLE, kind="lapsed", template_id="x", logical_date=TODAY)
    assert claim_push_send(db, **kwargs) is True
    assert claim_push_send(db, **{**kwargs, "template_id": "y"}) is False
    # 채널이 다르면 같은 날이어도 별개
    assert claim_push_send(db, **{**kwargs, "channel": PUSH_CHANNEL_WEEKLY_REPORT}) is True
    # 세션은 롤백 후에도 계속 쓸 수 있다
    assert db.scalar(select(PushSendLog.template_id).where(PushSendLog.channel == PUSH_CHANNEL_LIFECYCLE)) == "x"
    db.close()


def test_concurrent_instance_claim_blocks_send(db_factory, monkeypatch):
    """다른 인스턴스가 사전 조회 직후 먼저 행을 넣은 경우 — 유일 제약이 발송을 막는다."""
    db = db_factory()
    user = _user(db, "race")
    _meal(db, user.id, _ago(2))
    _log(db, user.id, TODAY, kind="missed_one", template_id="missed_one_light")
    monkeypatch.setattr(lifecycle_push, "sent_logs_since", lambda *a, **k: [])
    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert client.sent == []
    assert summary.skipped["already_sent"] == 1
    db.close()


# --- lapsed 3일 간격 ---

@pytest.mark.parametrize(("last_sent_ago", "sends"), [(1, False), (2, False), (3, True), (4, True)])
def test_lapsed_at_most_every_three_days(db_factory, last_sent_ago, sends):
    db = db_factory()
    user = _user(db, "lapsed")
    _meal(db, user.id, _ago(8))
    _log(db, user.id, _ago(last_sent_ago), kind="lapsed", template_id="lapsed_hello")
    client = MockPushClient()
    summary = send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert bool(client.sent) is sends
    if not sends:
        assert summary.skipped["lapsed_cooldown"] == 1
    db.close()


# --- 템플릿 ---

def test_templates_follow_copy_rules():
    banned = ("무료", "혜택", "할인", "이벤트", "kcal", "칼로리", "체중", "몸무게", "광고")
    seen_ids = set()
    for segment, pool in TEMPLATES.items():
        assert len(pool) >= 3, segment
        for t in pool:
            assert t.template_id not in seen_ids
            seen_ids.add(t.template_id)
            assert t.template_id.startswith(segment)
            # 제목은 "{name}: 대사" — 펫이 직접 말한다
            assert t.title.startswith("{name}: "), t.template_id
            line, body = t.title.removeprefix("{name}: "), t.body.format(streak=12)
            assert len(line) <= 20, line
            assert (line + body).count("🌱") <= 1
            assert not any(word in line + body for word in banned), t.template_id
            # 펫 말투는 반말로 통일 (홈 말풍선·튜토리얼·코칭과 같다) — 해요체 어미가 없다
            assert not re.search(r"요([.!?\s]|$)", line + " " + body), t.template_id
    assert all("{streak}" in t.body for t in TEMPLATES["keep_going"])


def test_push_title_uses_users_pet_name(db_factory):
    from app.models import GameProfile, PetBond

    db = db_factory()
    named = _user(db, "named")
    _meal(db, named.id, _ago(2))
    db.add(GameProfile(user_id=named.id, active_pet_code="pet_cat"))
    db.add(PetBond(user_id=named.id, pet_code="pet_cat", nickname="두부"))
    plain = _user(db, "plain")
    _meal(db, plain.id, _ago(2))
    db.add(GameProfile(user_id=plain.id, active_pet_code="pet_cat"))
    db.commit()
    assert lifecycle_push.pet_names(db, [named.id, plain.id]) == {named.id: "두부", plain.id: "모카"}

    client = MockPushClient()
    send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    titles = {m["tokens"][0]: m["title"] for m in client.sent}
    assert titles["t-named"].startswith("두부: ")
    assert titles["t-plain"].startswith("모카: ")
    db.close()


def test_choose_template_avoids_recent_and_is_deterministic():
    ids = [t.template_id for t in TEMPLATES["missed_one"]]
    picked = choose_template("missed_one", 7, TODAY, set(ids[:2]))
    assert picked.template_id == ids[2]
    # 전부 최근에 받았으면 전체에서 고른다
    assert choose_template("missed_one", 7, TODAY, set(ids)).template_id in ids
    # 같은 사용자·날짜면 같은 결과
    assert choose_template("lapsed", 42, TODAY, set()) == choose_template("lapsed", 42, TODAY, set())


def test_send_rotates_templates_using_last_seven_days(db_factory):
    db = db_factory()
    user = _user(db, "rotate")
    _meal(db, user.id, _ago(2))
    ids = [t.template_id for t in TEMPLATES["missed_one"]]
    _log(db, user.id, _ago(3), kind="missed_one", template_id=ids[0])
    _log(db, user.id, _ago(6), kind="missed_one", template_id=ids[1])
    client = MockPushClient()
    send_lifecycle_push(db, client, SLOT_LUNCH, now=LUNCH)
    assert db.scalar(
        select(PushSendLog.template_id).where(PushSendLog.logical_date == TODAY)
    ) == ids[2]
    expected = next(t for t in TEMPLATES["missed_one"] if t.template_id == ids[2])
    # 게임 프로필이 없는 사용자는 기본 펫 이름으로 말한다
    assert client.sent[0]["title"] == expected.title.format(name="모카")
    db.close()


def test_template_older_than_seven_days_can_repeat(db_factory):
    db = db_factory()
    user = _user(db, "old-log")
    _meal(db, user.id, _ago(2))
    ids = [t.template_id for t in TEMPLATES["missed_one"]]
    for tid in ids:
        _log(db, user.id, _ago(7) - timedelta(days=ids.index(tid)), kind="missed_one", template_id=tid)
    expected = choose_template("missed_one", user.id, TODAY, set())
    send_lifecycle_push(db, MockPushClient(), SLOT_LUNCH, now=LUNCH)
    assert db.scalar(
        select(PushSendLog.template_id).where(PushSendLog.logical_date == TODAY)
    ) == expected.template_id
    db.close()


# --- 토큰 정리 ---

class _Invalidating:
    def __init__(self, invalid):
        self.invalid = invalid
        self.sent = []

    def send(self, tokens, title, body, data) -> PushSendReport:
        self.sent.append(tokens)
        bad = [t for t in tokens if t in self.invalid]
        return PushSendReport(success_count=len(tokens) - len(bad), failure_count=len(bad), invalid_tokens=bad)


def test_invalid_tokens_are_purged(db_factory):
    db = db_factory()
    user = _user(db, "purge")
    db.add(PushToken(user_id=user.id, device_id="dev-dead", push_token="t-dead", platform="android"))
    db.commit()
    _meal(db, user.id, _ago(2))
    summary = send_lifecycle_push(db, _Invalidating(["t-dead"]), SLOT_LUNCH, now=LUNCH)
    assert (summary.success, summary.failure) == (1, 1)
    assert db.scalars(select(PushToken.push_token)).all() == ["t-purge"]
    db.close()


# --- 주간 리포트 푸시도 이력 테이블로 중복 방지 ---

def test_weekly_report_push_dedupes_via_send_log(db_factory):
    db = db_factory()
    user = _user(db, "weekly")
    for day in (date(2026, 7, 19), date(2026, 7, 21), date(2026, 7, 23)):
        _meal(db, user.id, day, hour=8)
        _meal(db, user.id, day, hour=12)
    sunday = datetime(2026, 7, 26, 9, 0, tzinfo=KST)

    first, restarted = MockPushClient(), MockPushClient()
    assert send_weekly_report_push(db, first, now=sunday) == 1
    # 같은 날 재기동(메모리 플래그 초기화)해도 두 번 나가지 않는다
    assert send_weekly_report_push(db, restarted, now=sunday + timedelta(hours=2)) == 0
    assert restarted.sent == []

    row = db.scalars(select(PushSendLog)).one()
    assert (row.channel, row.kind, row.template_id, row.logical_date) == (
        PUSH_CHANNEL_WEEKLY_REPORT, "report_arrival", "weekly_mid", date(2026, 7, 26)
    )
    # 라이프사이클 이력과는 채널이 달라 서로 막지 않는다
    assert claim_push_send(
        db, user_id=user.id, channel=PUSH_CHANNEL_LIFECYCLE, kind="lapsed", template_id="x",
        logical_date=date(2026, 7, 26),
    )
    db.close()


# --- 모델·마이그레이션 ---

def test_push_send_log_model_has_unique_key():
    table = PushSendLog.__table__
    assert {c.name for c in table.columns} == {
        "id", "user_id", "channel", "kind", "template_id", "logical_date", "sent_at",
    }
    uniques = [tuple(c.name for c in con.columns) for con in table.constraints if con.__class__.__name__ == "UniqueConstraint"]
    assert ("user_id", "channel", "logical_date") in uniques
    assert table.c.user_id.index


def test_push_send_logs_migration_roundtrip():
    path = Path(__file__).parents[1] / "alembic/versions/2026_10_15_1000-f5a8d2c6e917_push_send_logs.py"
    spec = importlib.util.spec_from_file_location("push_send_logs", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "f5a8d2c6e917"
    assert migration.down_revision == "e3c7a9b2d804"

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        insp = inspect(conn)
        assert "push_send_logs" in insp.get_table_names()
        uniques = insp.get_unique_constraints("push_send_logs")
        assert any(u["column_names"] == ["user_id", "channel", "logical_date"] for u in uniques)
        assert any(i["column_names"] == ["user_id"] for i in insp.get_indexes("push_send_logs"))
        migration.downgrade()
        assert "push_send_logs" not in inspect(conn).get_table_names()


def test_migration_is_single_head():
    versions = Path(__file__).parents[1] / "alembic/versions"
    texts = [p.read_text(encoding="utf-8") for p in versions.glob("*.py")]
    assert sum('down_revision = "e3c7a9b2d804"' in t for t in texts) == 1
    assert not any('down_revision = "f5a8d2c6e917"' in t for t in texts)
