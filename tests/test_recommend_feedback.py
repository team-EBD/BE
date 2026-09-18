"""추천 행동 로그 — recommendation_items 행으로 노출·채택·섭취를 기록하고 채택률을 산출한다.

docs/음식군-DB-계약.md §2.5 · §3 J. 군이 없는 DB(테스트 기본)에서는 이름 키로, 군이 있으면 군 키로 맞춘다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    FoodGroup,
    FoodGroupAlias,
    MealItem,
    MealRecord,
    RecommendationItem,
    RecommendationLog,
    User,
)
from app.services.recommend import recommend
from app.services.recommend.feedback import (
    acceptance_rates,
    log_exposure,
    mark_accepted,
    mark_eaten,
    source_stats,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)  # KST 18:00 → dinner


def _user(db, social_id: str, email: str | None = None) -> User:
    user = User(social_provider="google", social_id=social_id, nickname=social_id, email=email)
    db.add(user)
    db.flush()
    return user


def _meal(db, user: User, meal_type: str, eaten_at: datetime, foods, *, group_ids=None):
    record = MealRecord(
        user_id=user.id,
        meal_type=meal_type,
        eaten_at=eaten_at,
        is_skipped=False,
        total_calories=sum(f[1] for f in foods),
        total_carbs=sum(f[2] for f in foods),
        total_protein=sum(f[3] for f in foods),
        total_fat=sum(f[4] for f in foods),
    )
    db.add(record)
    db.flush()
    for pos, (name, kcal, carbs, protein, fat) in enumerate(foods):
        db.add(
            MealItem(
                meal_record_id=record.id, food_name=name, serving_amount=1.0,
                calories=kcal, carbs=carbs, protein=protein, fat=fat,
                food_group_id=group_ids[pos] if group_ids else None,
            )
        )
    db.flush()
    return record


def _history(db, user, food, days):
    for d in days:
        _meal(db, user, "dinner", NOW - timedelta(days=d, hours=-1), [food])


def _expose(db, user_id, now=NOW, *, shown=True):
    """추천 → 노출 로그. created_at 은 server_default(실제 시각)이라 테스트에선 고정한다."""
    result = recommend(db, user_id, meal_type="dinner", now=now)
    log = log_exposure(db, user_id, result, commit=False)
    log.created_at = now
    if shown:
        for row in _rows(db, log.id):
            row.shown_at = now
    db.flush()
    return log, result


def _rows(db, log_id):
    return db.scalars(
        select(RecommendationItem).where(RecommendationItem.log_id == log_id).order_by(RecommendationItem.rank)
    ).all()


def test_log_exposure_writes_context_and_item_rows(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    _history(db, u, ("돈까스", 720, 60, 34, 35), [2, 4])

    log, result = _expose(db, u.id, shown=False)
    assert log.user_id == u.id
    assert log.meal_context["meal_type"] == "dinner" and log.meal_context["engine"] == "v2"
    assert log.meal_context["meal_budget"] == result.budget.meal_budget
    assert [i["name"] for i in log.recommended_items] == [i.name for i in result.items]

    rows = _rows(db, log.id)
    assert [r.rank for r in rows] == list(range(1, len(result.items) + 1))
    assert [r.name for r in rows] == [i.name for i in result.items]
    assert all(r.source in ("personal", "popular", "similar", "catalog", "collaborative") for r in rows)
    assert all(r.shown_at is None for r in rows)
    assert all(r.accepted_at is None and r.eaten_at is None for r in rows)


def test_mark_accepted_is_idempotent_and_owner_scoped(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    other = _user(db, "u2", email="o@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)
    name = result.items[0].name

    assert mark_accepted(db, u.id, log.id, name, now=NOW, commit=False) is True
    first = _rows(db, log.id)[0].accepted_at
    assert first is not None
    assert mark_accepted(db, u.id, log.id, name, now=NOW + timedelta(minutes=5), commit=False) is True
    assert _rows(db, log.id)[0].accepted_at == first  # 처음 시각 유지

    assert mark_accepted(db, other.id, log.id, name, commit=False) is False  # 남의 로그
    assert mark_accepted(db, u.id, log.id, "없는음식", commit=False) is False


def test_mark_eaten_matches_within_window_only(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)
    name = result.items[0].name
    food = (name, 320, 18, 22, 16)
    item_id = _rows(db, log.id)[0].id

    r1 = _meal(db, u, "dinner", NOW + timedelta(hours=3), [food])
    assert mark_eaten(db, u.id, r1.id, recommendation_item_id=item_id, now=NOW + timedelta(hours=3)) == 1
    rows = _rows(db, log.id)
    assert rows[0].eaten_at is not None and rows[0].eaten_meal_record_id == r1.id
    assert all(r.eaten_at is None for r in rows[1:])
    # 이미 표시된 항목은 다시 세지 않는다
    r2 = _meal(db, u, "dinner", NOW + timedelta(hours=3), [food])
    assert mark_eaten(db, u.id, r2.id, recommendation_item_id=item_id, now=NOW + timedelta(hours=3)) == 0
    # 무관한 음식은 표시하지 않는다
    r3 = _meal(db, u, "dinner", NOW + timedelta(hours=1), [("전혀다른음식", 100, 1, 1, 1)])
    assert mark_eaten(db, u.id, r3.id, recommendation_item_id=item_id, now=NOW + timedelta(hours=1)) == 0
    # 창(4시간) 밖의 기록은 표시하지 않는다
    log2, result2 = _expose(db, u.id, NOW + timedelta(days=1))
    late = NOW + timedelta(days=1, hours=5)
    r4 = _meal(db, u, "dinner", late, [(result2.items[0].name, 320, 18, 22, 16)])
    assert mark_eaten(db, u.id, r4.id, recommendation_item_id=_rows(db, log2.id)[0].id, now=late) == 0
    assert all(r.eaten_at is None for r in _rows(db, log2.id))


def test_mark_eaten_matches_by_group_id_when_groups_exist(db_factory):
    """군이 있으면 이름이 달라도 같은 군이면 섭취로 본다 (햄버거 추천 → '빅소불고기버거' 기록)."""
    db = db_factory()
    g = FoodGroup(name="햄버거", family="버거·피자·샌드위치", role="meal", calories=480, carbs=40, protein=25, fat=22)
    db.add(g)
    db.flush()
    u = _user(db, "u1", email="u@gmail.com")
    log = RecommendationLog(user_id=u.id, meal_context={"engine": "v2"}, recommended_items=[])
    db.add(log)
    db.flush()
    log.created_at = NOW
    db.add(RecommendationItem(log_id=log.id, food_group_id=g.id, name="햄버거", source="popular", rank=1, shown_at=NOW))
    db.flush()

    record = _meal(db, u, "lunch", NOW + timedelta(hours=1), [("빅소불고기버거", 500, 42, 26, 24)], group_ids=[g.id])
    assert mark_eaten(db, u.id, record.id, recommendation_item_id=_rows(db, log.id)[0].id, now=NOW + timedelta(hours=1)) == 1
    assert _rows(db, log.id)[0].eaten_meal_record_id == record.id


def test_feedback_uses_snapshot_group_before_name_alias(db_factory):
    """저장 스냅샷이 확정한 군만 섭취로 센다 — 같은 이름의 alias 군을 이중 채택하지 않는다."""
    db = db_factory()
    groups = [
        FoodGroup(name="햄버거", family="버거·피자·샌드위치", role="meal"),
        FoodGroup(name="피자", family="버거·피자·샌드위치", role="meal"),
    ]
    db.add_all(groups)
    db.flush()
    db.add(FoodGroupAlias(alias="내메뉴", group_id=groups[0].id, kind="manual"))
    u = _user(db, "snapshot-first")
    log = RecommendationLog(user_id=u.id, created_at=NOW, meal_context={}, recommended_items=[])
    db.add(log)
    db.flush()
    for rank, group in enumerate(groups, 1):
        db.add(RecommendationItem(log_id=log.id, food_group_id=group.id, name=group.name, source="popular", rank=rank, shown_at=NOW))
    record = _meal(db, u, "lunch", NOW + timedelta(hours=1), [("내메뉴", 500, 40, 25, 22)], group_ids=[groups[1].id])
    assert mark_eaten(db, u.id, record.id, recommendation_item_id=_rows(db, log.id)[0].id, now=NOW + timedelta(hours=1)) == 0
    assert mark_eaten(db, u.id, record.id, recommendation_item_id=_rows(db, log.id)[1].id, now=NOW + timedelta(hours=1)) == 1
    rows = _rows(db, log.id)
    assert rows[0].eaten_at is None
    assert rows[1].eaten_meal_record_id == record.id


def test_feedback_resolves_alias_for_exposure_without_group_snapshot(db_factory):
    db = db_factory()
    group = FoodGroup(name="햄버거", family="버거·피자·샌드위치", role="meal")
    db.add(group)
    db.flush()
    db.add(FoodGroupAlias(alias="버거", group_id=group.id, kind="synonym"))
    u = _user(db, "old-exposure")
    log = RecommendationLog(user_id=u.id, created_at=NOW, meal_context={}, recommended_items=[])
    db.add(log)
    db.flush()
    db.add(RecommendationItem(log_id=log.id, name="버거", source="personal", rank=1, shown_at=NOW))
    record = _meal(db, u, "lunch", NOW + timedelta(hours=1), [("햄버거", 500, 40, 25, 22)])
    assert mark_eaten(db, u.id, record.id, recommendation_item_id=_rows(db, log.id)[0].id, now=NOW + timedelta(hours=1)) == 1


def test_acceptance_rates_need_minimum_exposures(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    # 날짜별 추천 순위와 무관하게 같은 음식의 노출 3회를 검증한다.
    for day in (1, 2, 3):
        log = RecommendationLog(
            user_id=u.id, created_at=NOW - timedelta(days=day),
            meal_context={}, recommended_items=[],
        )
        db.add(log)
        db.flush()
        meal = _meal(db, u, "dinner", log.created_at + timedelta(hours=1), [("김치찌개", 320, 18, 22, 16)]) if day <= 2 else None
        db.add(RecommendationItem(
            log_id=log.id, name="김치찌개", source="personal", rank=1,
            eaten_at=meal.eaten_at if meal else None,
            eaten_meal_record_id=meal.id if meal else None,
            shown_at=log.created_at,
        ))
    db.flush()

    rates = acceptance_rates(db, u.id, now=NOW)
    assert rates == {"김치찌개": pytest.approx(2 / 3, abs=0.001)}
    assert acceptance_rates(db, u.id, now=NOW, min_exposures=4) == {}
    assert acceptance_rates(db, u.id, now=NOW + timedelta(days=90)) == {}  # 구간 밖


def test_acceptance_rates_scope_by_user(db_factory):
    db = db_factory()
    a = _user(db, "a", email="a@gmail.com")
    b = _user(db, "b", email="b@gmail.com")
    for u in (a, b):
        _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    for _ in range(3):
        log, _r = _expose(db, a.id, NOW - timedelta(hours=5))
        _rows(db, log.id)[0].accepted_at = NOW - timedelta(hours=4)
    for _ in range(3):
        _expose(db, b.id, NOW - timedelta(hours=5))
    db.flush()
    assert any(rate > 0 for rate in acceptance_rates(db, a.id, now=NOW).values())
    b_rates = acceptance_rates(db, b.id, now=NOW)
    assert b_rates and all(rate == 0.0 for rate in b_rates.values())


def test_source_stats_counts_by_generator(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)
    _rows(db, log.id)[0].accepted_at = NOW
    db.flush()
    stats = source_stats(db, now=NOW)
    assert sum(s["shown"] for s in stats.values()) == len(result.items)
    assert sum(s["accepted"] for s in stats.values()) == 1


def test_meal_save_fills_group_and_marks_recommendation_eaten(client, auth_headers, db_factory):
    """기록 저장 API 가 (1) meal_items.food_group_id 를 채우고 (2) 최근 추천에 섭취 표시를 남긴다."""
    db = db_factory()
    stew = FoodGroup(name="김치찌개", family="국·탕·찌개류", role="meal", calories=320, carbs=18, protein=22, fat=16)
    db.add(stew)
    db.commit()

    res = client.post("/v1/foods/search", json={"query": "김치찌개"}, headers=auth_headers)
    item = res.json()["items"][0]
    user_id = db.scalar(select(User.id))
    log = RecommendationLog(user_id=user_id, meal_context={"meal_type": "dinner", "engine": "v2"}, recommended_items=[])
    db.add(log)
    db.flush()
    exposure = RecommendationItem(log_id=log.id, food_group_id=stew.id, name="김치찌개", source="personal", rank=1,
                                  shown_at=datetime.now(UTC) - timedelta(minutes=2))
    db.add(exposure)
    db.commit()
    log_id, stew_id = log.id, stew.id

    payload = {
        "meal_type": "dinner",
        "eaten_at": datetime.now(UTC).isoformat(),
        "recommendation_item_id": exposure.id,
        "items": [{
            "nutrition_item_id": item["nutrition_item_id"], "food_name": item["name"],
            "serving_amount": 1, "calories": 320, "carbs": 18, "protein": 22, "fat": 16,
        }],
    }
    created = client.post("/v1/meals", json=payload, headers=auth_headers)
    assert created.status_code == 201, created.text
    meal_id = created.json()["meal_id"]

    db.expire_all()
    saved_item = db.scalar(select(MealItem).where(MealItem.meal_record_id == meal_id))
    assert saved_item.food_group_id == stew_id  # 이름 '김치찌개' → 군명 정확일치
    row = db.scalar(select(RecommendationItem).where(RecommendationItem.log_id == log_id))
    assert row.eaten_at is not None and row.eaten_meal_record_id == meal_id
