"""추천 입력 신호의 집계 단위·시각 경계·관측 기회를 검증한다."""
from datetime import UTC, datetime, timedelta

import pytest

from app.models import FoodGroup, MealItem, MealRecord, RecommendationItem, RecommendationLog, User
from app.services.recommend.feedback import acceptance_rates, last_exposure, mark_eaten, source_stats
from app.services.recommend.signals import (
    companion_stats,
    decayed_frequency,
    global_popularity,
    meal_budget,
    recency_penalties,
)

NOW = datetime(2026, 8, 28, 9, tzinfo=UTC)


def _user(db, name):
    user = User(social_provider="google", social_id=name, nickname=name, email=f"{name}@example.com")
    db.add(user)
    db.flush()
    return user


def _meal(db, user, at, names, *, group_ids=None, meal_type="dinner"):
    record = MealRecord(
        user_id=user.id, meal_type=meal_type, eaten_at=at, is_skipped=False,
        total_calories=400 * len(names), total_carbs=40 * len(names),
        total_protein=25 * len(names), total_fat=15 * len(names),
    )
    db.add(record)
    db.flush()
    for i, name in enumerate(names):
        db.add(MealItem(
            meal_record_id=record.id, food_name=name, serving_amount=1,
            food_group_id=group_ids[i] if group_ids else None,
            calories=400, carbs=40, protein=25, fat=15,
        ))
    db.flush()
    return record


def _exposure(db, user, at, *, accepted_at=None, eaten_at=None, rejected_at=None):
    log = RecommendationLog(user_id=user.id, created_at=at, meal_context={}, recommended_items=[])
    db.add(log)
    db.flush()
    row = RecommendationItem(
        log_id=log.id, name="검증음식", source="popular", rank=1,
        accepted_at=accepted_at, eaten_at=eaten_at, rejected_at=rejected_at,
        shown_at=at,
    )
    db.add(row)
    db.flush()
    return log, row


def test_frequency_counts_one_food_group_per_meal(db_factory):
    db = db_factory()
    user = _user(db, "duplicates")
    group = FoodGroup(name="검증요리", family="밥류", role="meal", calories=400, carbs=40, protein=25, fat=15)
    db.add(group)
    db.flush()
    _meal(db, user, NOW - timedelta(days=1), ["상품A", "상품B"], group_ids=[group.id, group.id])
    stat = decayed_frequency(db, user.id, "dinner", now=NOW, min_items=1)[0]
    assert stat.count == 1
    assert stat.score == pytest.approx(0.5 ** (1 / 45), abs=0.0001)
    assert stat.calories == 400


def test_duplicate_items_do_not_prevent_sparse_meal_fallback(db_factory):
    db = db_factory()
    user = _user(db, "fallback")
    _meal(db, user, NOW - timedelta(days=1), ["같은음식"] * 5)
    _meal(db, user, NOW - timedelta(days=2), ["점심음식"], meal_type="lunch")
    assert {s.name for s in decayed_frequency(db, user.id, "dinner", now=NOW)} == {"같은음식", "점심음식"}


def test_popularity_counts_people_and_decays_old_support(db_factory):
    db = db_factory()
    prolific = _user(db, "prolific")
    for day in range(1, 20):
        _meal(db, prolific, NOW - timedelta(days=day), ["한사람음식", "한사람음식"])
    for name in ("person_a", "person_b"):
        user = _user(db, name)
        _meal(db, user, NOW - timedelta(days=1), ["여럿음식"])
        _meal(db, user, NOW - timedelta(days=31), ["오래된음식"])
    stats = global_popularity(db, "dinner", now=NOW)
    by_name = {s.name: s for s in stats}
    assert stats[0].name == "여럿음식"
    assert by_name["한사람음식"].count == 19
    assert by_name["한사람음식"].user_count == 1
    assert by_name["한사람음식"].score < 1
    assert by_name["여럿음식"].user_count == 2
    assert by_name["여럿음식"].score == pytest.approx(2 * by_name["오래된음식"].score, abs=0.0002)
    other_stats = global_popularity(db, "dinner", now=NOW, exclude_user_id=prolific.id)
    assert "한사람음식" not in {s.name for s in other_stats}


def test_future_meals_do_not_affect_signals_or_budget(db_factory):
    db = db_factory()
    user = _user(db, "future")
    _meal(db, user, NOW - timedelta(hours=1), ["지금음식"])
    for offset in range(1, 8):
        _meal(db, user, NOW + timedelta(minutes=offset), ["미래음식"], meal_type="breakfast")
    assert {s.name for s in decayed_frequency(db, user.id, "dinner", now=NOW)} == {"지금음식"}
    assert {s.name for s in global_popularity(db, None, now=NOW)} == {"지금음식"}
    assert recency_penalties(db, user.id, now=NOW) == {"지금음식": 1.0}
    budget = meal_budget(db, user.id, "dinner", now=NOW, day_start_hour=0)
    assert budget.ratio_source == "default"
    assert budget.remaining_today == 1600
    assert budget.protein_gap == 95


def test_companion_learning_excludes_future_meals(db_factory):
    db = db_factory()
    user = _user(db, "future_companion")
    main = FoodGroup(name="검증국", family="국·탕·찌개류", role="meal")
    rice = FoodGroup(name="검증밥", family="밥류", role="companion")
    db.add_all([main, rice])
    db.flush()
    _meal(db, user, NOW - timedelta(days=1), [main.name], group_ids=[main.id])
    _meal(db, user, NOW + timedelta(hours=1), [main.name, rice.name], group_ids=[main.id, rice.id])
    stat = next(iter(companion_stats(db, user.id, now=NOW).values()))
    assert stat.meals == 1
    assert stat.share == 0


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 8, 27, 18, tzinfo=UTC), 1.0),  # UTC 어제, KST 같은 날 03:00
        (datetime(2026, 8, 27, 14, tzinfo=UTC), 0.8),  # KST 어제 23:00
    ],
)
def test_recency_uses_kst_calendar_dates(db_factory, at, expected):
    db = db_factory()
    user = _user(db, "kst")
    _meal(db, user, at, ["검증음식"])
    assert recency_penalties(db, user.id, now=NOW)["검증음식"] == expected


def test_acceptance_does_not_treat_pending_impressions_as_rejections(db_factory):
    db = db_factory()
    user = _user(db, "observation")
    _exposure(db, user, NOW - timedelta(days=1), accepted_at=NOW - timedelta(hours=23))
    _exposure(db, user, NOW - timedelta(hours=5))
    _exposure(db, user, NOW - timedelta(minutes=5))  # 아직 반응 기회가 남음
    _exposure(db, user, NOW - timedelta(minutes=4), accepted_at=NOW)
    _exposure(db, user, NOW + timedelta(minutes=1), accepted_at=NOW + timedelta(minutes=2))
    # 성숙한 노출 두 개만 사용. 기록 시작 탭(0.2)은 실제 식사(1)와 구분한다.
    assert acceptance_rates(db, user.id, now=NOW, min_exposures=2) == {"검증음식": 0.1}
    assert acceptance_rates(db, user.id, now=NOW, min_exposures=3) == {}


def test_feedback_ignores_future_events_and_exposures(db_factory):
    db = db_factory()
    user = _user(db, "future_feedback")
    past, _ = _exposure(db, user, NOW - timedelta(days=1), accepted_at=NOW + timedelta(hours=1))
    _exposure(db, user, NOW + timedelta(hours=1), accepted_at=NOW + timedelta(hours=2))
    assert acceptance_rates(db, user.id, now=NOW, min_exposures=1) == {"검증음식": 0}
    assert source_stats(db, now=NOW) == {"popular": {"shown": 1, "accepted": 0, "rejected": 0, "eaten": 0}}
    assert last_exposure(db, user.id, now=NOW) == past.created_at
    meal = _meal(db, user, NOW, ["검증음식"])
    assert mark_eaten(db, user.id, meal.id, ["검증음식"], now=NOW) == 0


def test_recent_explicit_rejection_is_observed(db_factory):
    db = db_factory()
    user = _user(db, "rejected")
    _exposure(db, user, NOW - timedelta(minutes=1), rejected_at=NOW)
    assert acceptance_rates(db, user.id, now=NOW, min_exposures=1) == {}
    assert acceptance_rates(db, user.id, now=NOW + timedelta(hours=4), min_exposures=1) == {"검증음식": 0}
