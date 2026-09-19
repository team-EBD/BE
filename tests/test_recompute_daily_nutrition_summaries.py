"""06시 논리 날짜 전환용 일회성 캐시 재계산 스크립트."""
from __future__ import annotations

from datetime import date, datetime
from io import StringIO

from sqlalchemy import event, select

from app.core.timeutil import KST, to_utc
from app.models import DailyNutritionSummary, MealRecord, User
from scripts.recompute_daily_nutrition_summaries import run


def _user(db, name: str) -> User:
    user = User(
        social_provider="google", social_id=name, nickname=name, nickname_tag="0001"
    )
    db.add(user)
    db.flush()
    return user


def _meal(
    db, user_id: int, day: int, hour: int, calories: int,
    *, deleted: bool = False, skipped: bool = False,
) -> None:
    eaten_at = to_utc(datetime(2026, 9, day, hour, tzinfo=KST))
    db.add(MealRecord(
        user_id=user_id,
        meal_type="snack",
        eaten_at=eaten_at,
        total_calories=calories,
        total_carbs=calories / 10,
        total_protein=calories / 20,
        total_fat=calories / 40,
        is_skipped=skipped,
        deleted_at=eaten_at if deleted else None,
    ))


def _old_cache(db, user_id: int, day: int, calories: int) -> None:
    db.add(DailyNutritionSummary(
        user_id=user_id,
        summary_date=date(2026, 9, day),
        total_calories=calories,
        total_carbs=calories / 10,
        total_protein=calories / 20,
        total_fat=calories / 40,
        meal_count=1,
        summary_text="자정 경계의 이전 문구",
    ))


def _cache(db_factory, user_id: int) -> dict[date, DailyNutritionSummary]:
    with db_factory() as db:
        return {
            row.summary_date: row for row in db.scalars(
                select(DailyNutritionSummary).where(DailyNutritionSummary.user_id == user_id)
            )
        }


def test_old_midnight_cache_moves_early_meal_and_clears_ghost(db_factory):
    with db_factory() as db:
        user = _user(db, "midnight")
        _meal(db, user.id, 19, 23, 100)
        _meal(db, user.id, 20, 3, 200)
        _old_cache(db, user.id, 19, 100)
        _old_cache(db, user.id, 20, 200)
        db.commit()
        user_id = user.id

    counts = run(db_factory, apply=True, out=StringIO())
    rows = _cache(db_factory, user_id)
    assert (counts.created, counts.updated) == (0, 2)
    assert float(rows[date(2026, 9, 19)].total_calories) == 300
    assert rows[date(2026, 9, 19)].meal_count == 2
    assert float(rows[date(2026, 9, 20)].total_calories) == 0
    assert rows[date(2026, 9, 20)].meal_count == 0
    assert "기록이 없어요" in rows[date(2026, 9, 20)].summary_text


def test_meal_only_date_is_created_and_deleted_meal_is_excluded(db_factory):
    with db_factory() as db:
        user = _user(db, "new-date")
        _meal(db, user.id, 21, 3, 250)
        _meal(db, user.id, 22, 12, 500, deleted=True)
        _old_cache(db, user.id, 21, 250)
        db.commit()
        user_id = user.id

    counts = run(db_factory, apply=True, out=StringIO())
    rows = _cache(db_factory, user_id)
    assert (counts.created, counts.updated) == (1, 1)
    assert set(rows) == {date(2026, 9, 20), date(2026, 9, 21)}
    assert float(rows[date(2026, 9, 20)].total_calories) == 250
    assert float(rows[date(2026, 9, 21)].total_calories) == 0


def test_second_run_is_idempotent_and_resume_skips_prior_user(db_factory):
    with db_factory() as db:
        first = _user(db, "first")
        second = _user(db, "second")
        _meal(db, first.id, 20, 3, 100)
        _meal(db, second.id, 20, 3, 200)
        db.commit()
        first_id, second_id = first.id, second.id

    resumed = run(
        db_factory, apply=True, since_user_id=second_id, batch_size=1, out=StringIO()
    )
    assert (resumed.users, resumed.created) == (1, 1)
    assert _cache(db_factory, first_id) == {}

    first = run(db_factory, apply=True, batch_size=1, out=StringIO())
    second = run(db_factory, apply=True, batch_size=1, out=StringIO())
    assert (first.created, first.updated) == (1, 0)
    assert (second.created, second.updated, second.unchanged) == (0, 0, 2)


def test_default_dry_run_never_sends_write_sql(db_factory):
    with db_factory() as db:
        user = _user(db, "preview")
        _meal(db, user.id, 20, 3, 200)
        _old_cache(db, user.id, 20, 200)
        db.commit()
        user_id = user.id

    writes: list[str] = []

    def track_write(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(db_factory.kw["bind"], "before_cursor_execute", track_write)
    try:
        output = StringIO()
        counts = run(db_factory, out=output)
    finally:
        event.remove(db_factory.kw["bind"], "before_cursor_execute", track_write)

    assert writes == []
    assert (counts.created, counts.updated) == (1, 1)
    assert "--apply" in output.getvalue()
    rows = _cache(db_factory, user_id)
    assert set(rows) == {date(2026, 9, 20)}
    assert float(rows[date(2026, 9, 20)].total_calories) == 200
