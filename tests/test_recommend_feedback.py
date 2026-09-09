"""추천 행동 로그 — 노출·채택·섭취 기록과 채택률 산출 검증.

스키마 변경 없이 recommendation_logs 의 JSON 컬럼을 쓰므로, 그 계약(키 이름·시각 형식)이
여기서 고정된다. 전용 컬럼이 생기면 이 테스트가 이관 기준이 된다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import MealItem, MealRecord, RecommendationLog, User
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


def _meal(db, user: User, meal_type: str, eaten_at: datetime, foods):
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
    for name, kcal, carbs, protein, fat in foods:
        db.add(
            MealItem(
                meal_record_id=record.id, food_name=name, serving_amount=1.0,
                calories=kcal, carbs=carbs, protein=protein, fat=fat,
            )
        )
    db.flush()
    return record


def _history(db, user, food, days):
    for d in days:
        _meal(db, user, "dinner", NOW - timedelta(days=d, hours=-1), [food])


def _expose(db, user_id, now=NOW):
    """추천 → 노출 로그. created_at 은 server_default(실제 시각)이라 테스트에선 고정한다."""
    result = recommend(db, user_id, meal_type="dinner", now=now)
    log = log_exposure(db, user_id, result, commit=False)
    log.created_at = now
    db.flush()
    return log, result


def test_log_exposure_records_context_and_items(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    _history(db, u, ("돈까스", 720, 60, 34, 35), [2, 4])

    log, result = _expose(db, u.id)
    assert log.user_id == u.id
    assert log.meal_context["meal_type"] == "dinner"
    assert log.meal_context["engine"] == "v2"
    assert log.meal_context["meal_budget"] == result.budget.meal_budget
    assert len(log.recommended_items) == len(result.items)

    item = log.recommended_items[0]
    assert item["key"] == result.items[0].key
    assert item["source"] in ("personal", "popular", "similar")
    assert item["accepted_at"] is None and item["eaten_at"] is None
    assert "reason" in item and "score" in item


def test_mark_accepted_is_idempotent_and_owner_scoped(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    other = _user(db, "u2", email="o@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)
    key = result.items[0].key

    assert mark_accepted(db, u.id, log.id, key, now=NOW, commit=False) is True
    first = log.recommended_items[0]["accepted_at"]
    assert first is not None

    later = NOW + timedelta(minutes=5)
    assert mark_accepted(db, u.id, log.id, key, now=later, commit=False) is True
    assert log.recommended_items[0]["accepted_at"] == first  # 처음 시각 유지

    assert mark_accepted(db, other.id, log.id, key, commit=False) is False  # 남의 로그
    assert mark_accepted(db, u.id, log.id, "없는음식", commit=False) is False


def test_mark_eaten_matches_within_window_only(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)
    key = result.items[0].key
    name = result.items[0].name

    # 3시간 뒤 그 음식을 기록 → 섭취 표시
    assert mark_eaten(db, u.id, 999, [name], now=NOW + timedelta(hours=3)) == 1
    marked = next(i for i in log.recommended_items if i["key"] == key)
    assert marked["eaten_at"] is not None and marked["eaten_meal_record_id"] == 999
    # 나머지 항목은 그대로
    assert all(i["eaten_at"] is None for i in log.recommended_items if i["key"] != key)

    # 이미 표시된 항목은 다시 세지 않는다
    assert mark_eaten(db, u.id, 1000, [name], now=NOW + timedelta(hours=3)) == 0


def test_mark_eaten_ignores_old_and_unrelated(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, result = _expose(db, u.id)

    assert mark_eaten(db, u.id, 1, ["전혀다른음식"], now=NOW + timedelta(hours=1)) == 0
    assert mark_eaten(db, u.id, 2, [result.items[0].name], now=NOW + timedelta(hours=5)) == 0
    assert all(i["eaten_at"] is None for i in log.recommended_items)


def test_acceptance_rates_need_minimum_exposures(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])

    logs = [_expose(db, u.id, NOW - timedelta(days=d))[0] for d in (1, 2, 3)]
    key = logs[0].recommended_items[0]["key"]
    # 3회 노출 중 2회 섭취
    for log in logs[:2]:
        log.recommended_items[0]["eaten_at"] = NOW.isoformat()
        log.recommended_items = list(log.recommended_items)
    db.flush()

    rates = acceptance_rates(db, u.id, now=NOW)
    assert rates[key] == pytest.approx(2 / 3, abs=0.001)  # 소수 3자리 반올림
    # 노출이 기준에 못 미치는 키는 빠진다 → 랭킹이 중립값 0.5 를 쓴다
    assert acceptance_rates(db, u.id, now=NOW, min_exposures=4) == {}
    # 구간 밖(60일 전 이전) 로그는 세지 않는다
    assert acceptance_rates(db, u.id, now=NOW + timedelta(days=90)) == {}


def test_acceptance_rates_scope_by_user(db_factory):
    db = db_factory()
    a = _user(db, "a", email="a@gmail.com")
    b = _user(db, "b", email="b@gmail.com")
    for u in (a, b):
        _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    for _ in range(3):
        log, _r = _expose(db, a.id)
        log.recommended_items[0]["eaten_at"] = NOW.isoformat()
        log.recommended_items = list(log.recommended_items)
    for _ in range(3):
        _expose(db, b.id)
    db.flush()

    assert acceptance_rates(db, a.id, now=NOW)  # a 는 먹은 이력이 있다
    assert all(rate == 0.0 for rate in acceptance_rates(db, b.id, now=NOW).values())


def test_source_stats_counts_by_generator(db_factory):
    db = db_factory()
    u = _user(db, "u1", email="u@gmail.com")
    _history(db, u, ("김치찌개", 320, 18, 22, 16), [1, 3, 5])
    log, _result = _expose(db, u.id)
    log.recommended_items[0]["accepted_at"] = NOW.isoformat()
    log.recommended_items = list(log.recommended_items)
    db.flush()

    stats = source_stats(db, now=NOW)
    assert sum(s["shown"] for s in stats.values()) == len(log.recommended_items)
    assert sum(s["accepted"] for s in stats.values()) == 1


def test_meal_save_marks_recommendation_as_eaten(client, auth_headers, db_factory):
    """기록 저장 API 가 최근 추천에 섭취 표시를 남긴다 (create_meal 훅)."""
    res = client.post("/v1/foods/search", json={"query": "김치찌개"}, headers=auth_headers)
    item = res.json()["items"][0]

    db = db_factory()
    user_id = db.scalar(User.__table__.select().with_only_columns(User.id))
    log = RecommendationLog(
        user_id=user_id,
        meal_context={"meal_type": "dinner", "engine": "v2"},
        recommended_items=[
            {"key": "김치찌개", "name": "김치찌개", "source": "personal",
             "accepted_at": None, "eaten_at": None, "eaten_meal_record_id": None}
        ],
    )
    db.add(log)
    db.commit()
    log_id = log.id

    payload = {
        "meal_type": "dinner",
        "eaten_at": "2026-08-28T19:00:00+09:00",
        "items": [
            {
                "nutrition_item_id": item["nutrition_item_id"],
                "food_name": item["name"],
                "serving_amount": 1,
                "calories": 320, "carbs": 18, "protein": 22, "fat": 16,
            }
        ],
    }
    created = client.post("/v1/meals", json=payload, headers=auth_headers)
    assert created.status_code == 201, created.text

    db.expire_all()
    saved = db.get(RecommendationLog, log_id)
    assert saved.recommended_items[0]["eaten_at"] is not None
    assert saved.recommended_items[0]["eaten_meal_record_id"] == created.json()["meal_id"]
