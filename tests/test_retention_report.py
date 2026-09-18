"""리텐션 D1/D7 리포트 스크립트 (scripts/retention_report.py).

읽기 전용 CLI 라 엔드포인트가 없다 — 세션 팩토리를 주입해 직접 돌린다.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

from app.core.timeutil import KST, to_utc
from app.models import ClientEvent, MealRecord, User
from scripts.retention_report import (
    build_rows,
    game_event_rows,
    render_table,
    report,
    resolve_range,
)

COHORT = date(2026, 9, 1)


def at(day: date, hour: int = 12) -> datetime:
    """그 논리 날짜(KST 06시 경계) 한가운데의 UTC 시각."""
    return to_utc(datetime.combine(day, datetime.min.time(), tzinfo=KST).replace(hour=hour))


def make_user(db, day: date, social_id: str) -> User:
    user = User(
        social_provider="google",
        social_id=social_id,
        nickname=social_id,
        nickname_tag="0001",
        created_at=at(day, hour=9),
    )
    db.add(user)
    db.flush()
    return user


def add_meal(db, user: User, day: date, *, skipped: bool = False, deleted: bool = False) -> None:
    db.add(
        MealRecord(
            user_id=user.id,
            meal_type="lunch",
            eaten_at=at(day),
            is_skipped=skipped,
            deleted_at=at(day) if deleted else None,
        )
    )


def rows_for(db_factory, since: date = COHORT, until: date = COHORT):
    with db_factory() as db:
        return build_rows(db, since, until)


def test_user_who_records_the_next_day_counts_as_d1(db_factory):
    with db_factory() as db:
        user = make_user(db, COHORT, "d1-user")
        add_meal(db, user, COHORT + timedelta(days=1))
        db.commit()

    row = rows_for(db_factory)[0]

    assert (row.signups, row.d1, row.d1_d7) == (1, 1, 1)
    assert row.d7 == 0
    assert row.rate(row.d1) == 100.0


def test_skip_only_user_is_not_active(db_factory):
    """생략 기록은 활동이 아니다 — 보상을 안 주는 기준과 같다."""
    with db_factory() as db:
        user = make_user(db, COHORT, "skipper")
        add_meal(db, user, COHORT + timedelta(days=1), skipped=True)
        add_meal(db, user, COHORT + timedelta(days=7), skipped=True)
        db.commit()

    row = rows_for(db_factory)[0]

    assert (row.signups, row.d1, row.d7, row.d1_d7) == (1, 0, 0, 0)


def test_deleted_record_is_not_active(db_factory):
    with db_factory() as db:
        user = make_user(db, COHORT, "deleter")
        add_meal(db, user, COHORT + timedelta(days=1), deleted=True)
        db.commit()

    assert rows_for(db_factory)[0].d1 == 0


def test_empty_cohort_does_not_divide_by_zero(db_factory):
    """가입자 0 인 날은 0% 가 아니라 `-` 다."""
    row = rows_for(db_factory)[0]

    assert (row.signups, row.d1) == (0, 0)
    assert row.rate(row.d1) is None
    table = render_table([row], [])
    line = next(l for l in table.splitlines() if l.startswith(COHORT.isoformat()))
    assert line.split() == [COHORT.isoformat(), "0", "-", "-", "-"]  # 0.0% 가 아니다


def test_d7_and_cumulative_window(db_factory):
    """D7 은 +7 일 당일, D1-D7 은 +1~+7 중 하루라도."""
    with db_factory() as db:
        exact = make_user(db, COHORT, "d7-user")
        add_meal(db, exact, COHORT + timedelta(days=7))
        middle = make_user(db, COHORT, "d4-user")
        add_meal(db, middle, COHORT + timedelta(days=4))
        late = make_user(db, COHORT, "d8-user")
        add_meal(db, late, COHORT + timedelta(days=8))  # 창 밖
        db.commit()

    row = rows_for(db_factory)[0]

    assert (row.signups, row.d1, row.d7, row.d1_d7) == (3, 0, 1, 2)


def test_signup_day_activity_alone_is_not_retention(db_factory):
    with db_factory() as db:
        user = make_user(db, COHORT, "day0-only")
        add_meal(db, user, COHORT)
        db.commit()

    row = rows_for(db_factory)[0]

    assert (row.signups, row.d1_d7) == (1, 0)


def test_rows_cover_every_day_in_range(db_factory):
    rows = rows_for(db_factory, COHORT, COHORT + timedelta(days=2))

    assert [r.cohort_date for r in rows] == [
        COHORT, COHORT + timedelta(days=1), COHORT + timedelta(days=2)
    ]


def test_game_events_report_unique_users_without_meta(db_factory):
    with db_factory() as db:
        a = make_user(db, COHORT, "event-a")
        b = make_user(db, COHORT, "event-b")
        for _ in range(3):  # 같은 사용자의 반복은 1로 접힌다
            db.add(ClientEvent(user_id=a.id, event_type="game_stage_view", created_at=at(COHORT)))
        db.add(ClientEvent(user_id=b.id, event_type="game_stage_view", created_at=at(COHORT)))
        db.add(ClientEvent(user_id=a.id, event_type="meal_saved", created_at=at(COHORT)))
        db.commit()

    with db_factory() as db:
        events = game_event_rows(db, COHORT, COHORT)

    assert events == [(COHORT, "game_stage_view", 2)]  # game_* 만, meta 는 보지 않는다


def test_report_prints_table_with_the_churn_caveat(db_factory):
    with db_factory() as db:
        user = make_user(db, COHORT, "printer")
        add_meal(db, user, COHORT + timedelta(days=1))
        db.commit()
    out = io.StringIO()

    report(
        db_factory, since=COHORT.isoformat(), until=COHORT.isoformat(), stream=out
    )
    text = out.getvalue()

    assert "탈퇴한 사용자" in text  # 한계 고지가 머리말에 있다
    assert "2026-09-01" in text and "(100.0%)" in text
    assert "게임 이벤트" in text


def test_report_csv_mode(db_factory):
    with db_factory() as db:
        user = make_user(db, COHORT, "csv-user")
        add_meal(db, user, COHORT + timedelta(days=1))
        db.commit()
    out = io.StringIO()

    report(db_factory, since=COHORT.isoformat(), until=COHORT.isoformat(), as_csv=True, stream=out)
    lines = out.getvalue().splitlines()

    assert lines[0].startswith("section,cohort_date,signups")
    assert lines[1] == "cohort,2026-09-01,1,1,100.0,0,0.0,1,100.0"
    assert any(line.startswith("total,") for line in lines)


def test_resolve_range_defaults_to_last_n_days():
    today = date(2026, 9, 18)

    assert resolve_range(30, None, None, today=today) == (date(2026, 8, 20), today)
    assert resolve_range(30, "2026-09-01", None, today=today) == (date(2026, 9, 1), today)
    assert resolve_range(30, None, "2026-09-10", today=today) == (
        date(2026, 8, 12), date(2026, 9, 10)
    )
