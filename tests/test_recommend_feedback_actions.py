"""실제 카드 노출 → 명시 행동 → 검증 식사 → 수정/삭제의 피드백 계약."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.timeutil import from_db
from app.models import RecommendationItem, RecommendationLog
from app.schemas.meal import MealCreateRequest, MealUpdateRequest
from app.services.meals import create_meal, delete_meal, update_meal
from app.services.recommend import recommend
from app.services.recommend.feedback import (
    acceptance_rates, excluded_keys, log_exposure, mark_eaten, outcome_reward,
    record_feedback, source_stats,
)
from tests.test_recommend_feedback import NOW, _meal, _rows, _user


def _card(db, user, *, name="검증음식", shown_at=NOW, created_at=NOW):
    log = RecommendationLog(user_id=user.id, created_at=created_at, meal_context={}, recommended_items=[])
    db.add(log)
    db.flush()
    row = RecommendationItem(log_id=log.id, name=name, rank=1, source="catalog", shown_at=shown_at)
    db.add(row)
    db.flush()
    return row


def _meal_body(name="검증음식", *, at=NOW + timedelta(hours=1), item_id=None):
    return MealCreateRequest(
        meal_type="dinner", eaten_at=at, recommendation_item_id=item_id,
        items=[{"food_name": name, "calories": 400, "carbs": 40, "protein": 25, "fat": 15}],
    )


def test_generated_cards_preserve_policy_but_are_not_impressions(db_factory):
    db = db_factory()
    user = _user(db, "snapshot")
    result = recommend(db, user.id, meal_type="dinner", now=NOW)
    decision = deepcopy(result.decision)
    log = log_exposure(db, user.id, result, commit=False)
    log.created_at = NOW
    rows = _rows(db, log.id)
    assert rows and log.decision == decision
    assert all(row.shown_at is None for row in rows)
    assert [row.features for row in rows] == [item.features for item in result.items]
    assert [row.selection_probability for row in rows] == [item.selection_probability for item in result.items]
    assert all(row.policy_version == decision["policy_version"] for row in rows)
    result.decision["selected_keys"].clear()
    assert log.decision == decision
    assert source_stats(db, now=NOW + timedelta(days=1)) == {}
    assert acceptance_rates(db, user.id, now=NOW + timedelta(days=1), min_exposures=1) == {}


def test_feedback_actions_are_owned_and_idempotent(db_factory):
    db = db_factory()
    user, other = _user(db, "owner"), _user(db, "other")
    row = _card(db, user, shown_at=None)
    assert not record_feedback(db, other.id, row.id, "impression", now=NOW, commit=False)
    for action, reason in (("impression", None), ("accept", None), ("reject", "not_now")):
        assert record_feedback(db, user.id, row.id, action, reason=reason, now=NOW, commit=False)
        first = (row.shown_at, row.accepted_at, row.rejected_at)
        assert record_feedback(db, user.id, row.id, action, reason=reason, now=NOW + timedelta(minutes=1), commit=False)
        assert (row.shown_at, row.accepted_at, row.rejected_at) == first
    assert row.reject_reason == "not_now"
    assert outcome_reward(row, now=NOW + timedelta(hours=5)) == 0


def test_tap_is_weak_signal_and_is_not_food_consumption(db_factory):
    db = db_factory()
    user = _user(db, "tap")
    row = _card(db, user, shown_at=None)
    record_feedback(db, user.id, row.id, "accept", now=NOW, commit=False)
    assert row.shown_at == row.accepted_at == NOW
    assert row.eaten_at is None
    assert acceptance_rates(db, user.id, now=NOW + timedelta(hours=3), min_exposures=1) == {}
    assert acceptance_rates(db, user.id, now=NOW + timedelta(hours=4), min_exposures=1) == {"검증음식": 0.2}


@pytest.mark.parametrize("case", ["no_id", "unshown", "past_meal", "future_meal", "late_meal", "wrong_food", "wrong_owner"])
def test_attribution_rejects_unrelated_or_invalid_meals(db_factory, case):
    db = db_factory()
    user, other = _user(db, "owner"), _user(db, "other")
    row = _card(db, other if case == "wrong_owner" else user, shown_at=None if case == "unshown" else NOW)
    at = {
        "past_meal": NOW - timedelta(minutes=1), "future_meal": NOW + timedelta(hours=3),
        "late_meal": NOW + timedelta(hours=5),
    }.get(case, NOW + timedelta(hours=1))
    name = "다른음식" if case == "wrong_food" else row.name
    meal = _meal(db, user, "dinner", at, [(name, 400, 40, 25, 15)])
    # 이름/군 인자를 위조해도 실제 DB 식사 항목만 사용한다.
    assert mark_eaten(
        db, user.id, meal.id, [row.name], recommendation_item_id=None if case == "no_id" else row.id,
        now=NOW + timedelta(hours=2 if case != "late_meal" else 6),
    ) == 0
    assert row.eaten_at is None and row.eaten_meal_record_id is None


def test_one_explicit_item_only_is_rewarded_and_cannot_be_reused(db_factory):
    db = db_factory()
    user = _user(db, "one_item")
    selected, duplicate = _card(db, user), _card(db, user)
    meal = _meal(db, user, "dinner", NOW + timedelta(hours=1), [(selected.name, 400, 40, 25, 15)])
    assert mark_eaten(db, user.id, meal.id, recommendation_item_id=selected.id, now=NOW + timedelta(hours=2)) == 1
    assert selected.eaten_at == from_db(meal.eaten_at)
    assert duplicate.eaten_at is None
    assert mark_eaten(db, user.id, meal.id, recommendation_item_id=selected.id, now=NOW + timedelta(hours=2)) == 0
    assert mark_eaten(db, user.id, meal.id, recommendation_item_id=duplicate.id, now=NOW + timedelta(hours=2)) == 0
    another = _meal(db, user, "dinner", NOW + timedelta(hours=2), [(selected.name, 400, 40, 25, 15)])
    assert mark_eaten(db, user.id, another.id, recommendation_item_id=selected.id, now=NOW + timedelta(hours=2)) == 0


@pytest.mark.parametrize("autoflush", [True, False])
def test_meal_edits_and_deletion_reconcile_reward(db_factory, autoflush):
    # 운영 SessionLocal은 autoflush=False이다. 테스트의 자동 flush에 의존하지 않는다.
    db = db_factory(autoflush=autoflush)
    user = _user(db, "edits")
    row = _card(db, user)
    meal = create_meal(db, user, _meal_body(item_id=row.id))
    assert outcome_reward(row, now=NOW + timedelta(hours=5)) == 1
    update_meal(db, user, meal.id, MealUpdateRequest(items=_meal_body("다른음식").items))
    assert row.eaten_at is None
    assert row.eaten_meal_record_id == meal.id  # 수정 이력의 연결은 유지
    update_meal(db, user, meal.id, MealUpdateRequest(items=_meal_body().items))
    assert outcome_reward(row, now=NOW + timedelta(hours=5)) == 1
    update_meal(db, user, meal.id, MealUpdateRequest(eaten_at=NOW - timedelta(hours=1)))
    assert row.eaten_at is None
    update_meal(db, user, meal.id, MealUpdateRequest(eaten_at=NOW + timedelta(hours=1)))
    assert row.eaten_at is not None
    update_meal(db, user, meal.id, MealUpdateRequest(is_skipped=True))
    assert row.eaten_at is None
    update_meal(db, user, meal.id, MealUpdateRequest(items=_meal_body().items))
    assert row.eaten_at is not None
    delete_meal(db, user, meal.id)
    assert row.eaten_at is None
    assert outcome_reward(row, now=NOW + timedelta(hours=5)) == 0


def test_not_now_expires_and_dislike_excludes_catalog_fallback(db_factory):
    db = db_factory()
    user = _user(db, "dislike")
    temporary = _card(db, user, name="김밥")
    persistent = _card(db, user, name="라면")
    record_feedback(db, user.id, temporary.id, "reject", reason="not_now", now=NOW, commit=False)
    record_feedback(db, user.id, persistent.id, "reject", reason="dislike", now=NOW, commit=False)
    assert excluded_keys(db, user.id, now=NOW) == {"김밥", "라면"}
    assert excluded_keys(db, user.id, now=NOW + timedelta(hours=5)) == {"라면"}
    result = recommend(db, user.id, meal_type="dinner", now=NOW)
    assert not ({c.key for c in result.candidates} & {"김밥", "라면"})
    assert result.items  # 거절을 우회하지 않고 다른 기본 메뉴로 채운다
    assert excluded_keys(db, user.id, now=NOW + timedelta(days=91)) == set()


def test_global_feedback_excludes_internal_test_accounts(db_factory):
    db = db_factory()
    user = _user(db, "real")
    tester = _user(db, "test", email="ADMIN@TEST.COM")
    real = _card(db, user)
    _card(db, tester)
    real.accepted_at = NOW
    db.flush()
    assert acceptance_rates(db, now=NOW + timedelta(hours=5), min_exposures=1) == {"검증음식": 0.2}
    assert acceptance_rates(db, tester.id, now=NOW + timedelta(hours=5), min_exposures=1) == {"검증음식": 0.0}


def test_feedback_api_and_meal_conversion_train_next_policy(client, auth_headers, db_factory, monkeypatch):
    monkeypatch.setattr(settings, "recommend_engine", "v2")
    monkeypatch.setattr(settings, "recommend_bandit_epsilon", 0)
    menu = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "dinner"}).json()
    picked = menu["recommended_menus"][-1]  # 밴딧이 선택한 마지막 카드
    item_id = picked["recommendation_item_id"]
    url = f"/v1/recommendations/items/{item_id}/feedback"
    assert client.post(url, headers=auth_headers, json={"action": "impression"}).json() == {"recorded": True}
    with db_factory() as db:
        row = db.get(RecommendationItem, item_id)
        shown = datetime.now(UTC) - timedelta(hours=5)
        row.shown_at = shown
        db.get(RecommendationLog, row.log_id).created_at = shown
        db.commit()
    payload = _meal_body(picked["name"], at=shown + timedelta(hours=1), item_id=item_id).model_dump(mode="json")
    saved = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert saved.status_code == 201, saved.text
    following = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "dinner"})
    assert following.status_code == 200, following.text
    with db_factory() as db:
        row = db.get(RecommendationItem, item_id)
        assert row.eaten_meal_record_id == saved.json()["meal_id"]
        assert from_db(row.eaten_at) == shown + timedelta(hours=1)
        decision = db.get(RecommendationLog, following.json()["recommendation_log_id"]).decision
        assert decision["model"]["samples"] == 1
    assert client.delete(f"/v1/meals/{saved.json()['meal_id']}", headers=auth_headers).status_code == 200
    with db_factory() as db:
        assert db.get(RecommendationItem, item_id).eaten_at is None


@pytest.mark.parametrize("payload", [
    {"action": "reject"}, {"action": "reject", "reason": "unknown"},
    {"action": "impression", "reason": "not_now"}, {"action": "unknown"},
])
def test_feedback_api_validates_action_and_reason(client, auth_headers, payload):
    response = client.post("/v1/recommendations/items/99999/feedback", headers=auth_headers, json=payload)
    assert response.status_code == 400


def test_feedback_api_hides_other_users_items(client, auth_headers, db_factory):
    with db_factory() as db:
        other = _user(db, "other-owner")
        row = _card(db, other, shown_at=None)
        db.commit()
        item_id = row.id
    response = client.post(f"/v1/recommendations/items/{item_id}/feedback", headers=auth_headers, json={"action": "impression"})
    assert response.status_code == 404
