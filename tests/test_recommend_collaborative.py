"""협업 후보가 실제 다른 사용자의 음식군 관계가 있을 때만 생성되는지 검증한다."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import FoodGroup, MealItem, MealRecord, User
from app.services.recommend.candidates import Candidate, merge
from app.services.recommend.collaborative import collaborative_candidates
from app.services.recommend.explain import reason
from app.services.recommend.groups import EMPTY_INDEX
from app.services.recommend.ranking import Ranked
from app.services.recommend.signals import Budget

NOW = datetime(2026, 9, 18, 9, tzinfo=UTC)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _user(db, name, *, email=None):
    user = User(
        social_provider="google", social_id=name, nickname=name,
        email=email if email is not None else f"{name}@example.com",
    )
    db.add(user)
    db.flush()
    return user


def _group(db, name, *, family="국·탕·찌개류", role="meal", calories=320, companion=None):
    group = FoodGroup(
        name=name, family=family, role=role, calories=calories, carbs=30, protein=20, fat=10,
        companion_group_id=companion.id if companion else None,
    )
    db.add(group)
    db.flush()
    return group


def _meal(db, user, groups, *, days=1, skipped=False, deleted=False):
    record = MealRecord(
        user_id=user.id, meal_type="dinner", eaten_at=NOW - timedelta(days=days), is_skipped=skipped,
        total_calories=sum(float(group.calories or 0) for group in groups),
        total_carbs=sum(float(group.carbs or 0) for group in groups),
        total_protein=sum(float(group.protein or 0) for group in groups),
        total_fat=sum(float(group.fat or 0) for group in groups),
        deleted_at=NOW if deleted else None,
    )
    db.add(record)
    db.flush()
    for group in groups:
        db.add(MealItem(
            meal_record_id=record.id, food_name=group.name, food_group_id=group.id,
            serving_amount=1, calories=group.calories, carbs=group.carbs,
            protein=group.protein or 0, fat=group.fat,
        ))
    db.flush()
    return record


def _profile(db):
    user = _user(db, "requester")
    first = _group(db, "김치찌개")
    second = _group(db, "치즈버거", family="버거·피자·샌드위치", calories=500)
    _meal(db, user, [first], days=1)
    _meal(db, user, [second], days=3)
    return user, first, second


def _support(db, prefix, groups, count=3):
    for number in range(count):
        _meal(db, _user(db, f"{prefix}{number}"), groups)


@pytest.mark.parametrize("profile_size", [0, 1])
def test_empty_or_single_food_profile_does_not_invent_collaborative_preferences(db, profile_size):
    user = _user(db, "requester")
    first = _group(db, "김치찌개")
    other = _group(db, "된장찌개")
    _support(db, "neighbor", [first, other])
    if profile_size:
        _meal(db, user, [first])
    assert collaborative_candidates(db, user.id, 700, now=NOW, meal_type="dinner") == []


def test_collaborative_relation_can_cross_food_families_without_name_similarity(db):
    user, first, _ = _profile(db)
    alternative = _group(db, "새우볶음밥", family="밥류", calories=500)
    _support(db, "neighbor", [first, alternative])

    got = collaborative_candidates(db, user.id, 700, now=NOW, meal_type="dinner")

    assert [candidate.group_id for candidate in got] == [alternative.id]
    assert got[0].source == "collaborative" and got[0].collaborative_support == 3
    assert 0 < got[0].collaborative < 1
    assert got[0].similarity == got[0].freq == got[0].popularity == 0


def test_many_records_and_duplicate_items_from_two_people_do_not_satisfy_support(db):
    user, first, _ = _profile(db)
    alternative = _group(db, "된장찌개")
    for number in range(2):
        neighbor = _user(db, f"neighbor{number}")
        for days in range(1, 8):
            _meal(db, neighbor, [first, alternative, alternative], days=days)
    assert collaborative_candidates(db, user.id, 700, now=NOW) == []


def test_requester_is_not_counted_as_the_third_collaborative_supporter(db):
    user, first, second = _profile(db)
    _support(db, "neighbor", [first, second], count=2)
    assert collaborative_candidates(db, user.id, 700, now=NOW) == []


@pytest.mark.parametrize("invalid", ["future", "old", "deleted", "skipped", "test"])
def test_invalid_neighbor_records_cannot_supply_the_missing_third_supporter(db, invalid):
    user, first, _ = _profile(db)
    alternative = _group(db, "새우볶음밥", family="밥류", calories=500)
    _support(db, "neighbor", [first, alternative], count=2)
    email = "internal@test.com" if invalid == "test" else "third@example.com"
    third = _user(db, "third", email=email)
    _meal(
        db, third, [first, alternative], days=-1 if invalid == "future" else 91 if invalid == "old" else 1,
        deleted=invalid == "deleted", skipped=invalid == "skipped",
    )
    assert collaborative_candidates(db, user.id, 700, now=NOW) == []


def test_future_requester_meal_does_not_complete_the_minimum_profile(db):
    user, first, second = _profile(db)
    records = db.query(MealRecord).filter(MealRecord.user_id == user.id).order_by(MealRecord.id).all()
    records[-1].eaten_at = NOW + timedelta(days=1)
    db.flush()
    _support(db, "neighbor", [first, second])
    assert collaborative_candidates(db, user.id, 700, now=NOW) == []


def test_stronger_independent_support_reduces_small_sample_shrinkage(db):
    user, first, second = _profile(db)
    small = _group(db, "가까운찌개")
    large = _group(db, "다른버거", family="버거·피자·샌드위치", calories=500)
    _support(db, "small", [first, small], count=3)
    _support(db, "large", [second, large], count=6)

    got = collaborative_candidates(db, user.id, 700, now=NOW)

    assert [candidate.group_id for candidate in got] == [large.id, small.id]
    assert got[0].collaborative_support == 6 and got[1].collaborative_support == 3
    assert got[0].collaborative > got[1].collaborative


def test_popular_food_without_shared_users_is_not_collaborative_evidence(db):
    user, first, _ = _profile(db)
    related = _group(db, "된장찌개")
    popular = _group(db, "순두부찌개")
    _support(db, "related", [first, related])
    _support(db, "unrelated", [popular], count=10)
    got = collaborative_candidates(db, user.id, 700, now=NOW)
    assert [candidate.group_id for candidate in got] == [related.id]


def test_companion_budget_is_applied_before_top_limit(db):
    user, first, _ = _profile(db)
    rice = _group(db, "쌀밥", family="밥류", role="companion", calories=300)
    excessive = _group(db, "가나다찌개", calories=800, companion=rice)
    suitable = _group(db, "된장찌개", calories=250, companion=rice)
    _support(db, "neighbor", [first, excessive, suitable])
    got = collaborative_candidates(db, user.id, 500, now=NOW, meal_type="dinner", top=1)
    assert [candidate.group_id for candidate in got] == [suitable.id]
    assert got[0].total_calories == 550


def test_other_sources_do_not_erase_collaborative_evidence_for_an_existing_food(db):
    user, first, second = _profile(db)
    _support(db, "neighbor", [first, second])
    got = collaborative_candidates(db, user.id, 700, now=NOW)
    assert {candidate.group_id for candidate in got} == {first.id, second.id}
    mine = Candidate(first.name, first.name, 320, 30, 20, 10, "personal", freq=4, group_id=first.id)
    merged = next(candidate for candidate in merge([mine], got) if candidate.key == first.name)
    assert merged.source == "personal" and merged.freq == 4
    assert merged.collaborative > 0 and merged.collaborative_support == 3


def test_explicitly_blocked_food_is_not_used_as_a_collaborative_interest_seed(db):
    user, first, _ = _profile(db)
    third = _group(db, "비빔밥", family="밥류", calories=500)
    _meal(db, user, [third])  # 차단한 음식을 빼도 관심 군 두 개는 남는다.
    alternative = _group(db, "된장찌개")
    _support(db, "neighbor", [first, alternative])
    assert collaborative_candidates(db, user.id, 700, now=NOW)
    assert collaborative_candidates(
        db, user.id, 700, now=NOW, exclude_keys=frozenset({first.name}),
    ) == []


def test_meal_role_missing_nutrients_and_explicit_exclusion_apply_to_collaborative_pool(db):
    user, first, _ = _profile(db)
    snack = _group(db, "쿠키", family="빵·과자·디저트", role="snack", calories=150)
    incomplete = _group(db, "된장찌개")
    incomplete.protein = None
    excluded = _group(db, "순두부찌개")
    _support(db, "neighbor", [first, snack, incomplete, excluded])
    got = collaborative_candidates(
        db, user.id, 700, now=NOW, meal_type="dinner", exclude_keys=frozenset({excluded.name}),
    )
    assert got == []


def test_no_group_index_cannot_create_an_unstable_name_based_collaborative_matrix(db):
    user = _user(db, "requester")
    assert collaborative_candidates(db, user.id, 700, now=NOW, index=EMPTY_INDEX) == []


def test_collaborative_reason_states_its_actual_evidence(db):
    user, first, _ = _profile(db)
    alternative = _group(db, "된장찌개")
    _support(db, "neighbor", [first, alternative])
    candidate = collaborative_candidates(db, user.id, 700, now=NOW)[0]
    budget = Budget("dinner", 2000, 0.35, "default", 700, 2000, 700, 40, "any")
    explanation = reason(Ranked(candidate, 0.5, {}), budget)
    assert "다른 이용자" in explanation
    assert "많이 기록되는" not in explanation and "자주 드시는" not in explanation
