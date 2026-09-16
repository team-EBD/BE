"""POST /recommendations/menu — settings.recommend_engine 로 legacy(AI) ↔ v2(이력 엔진) 전환.

v2 는 AI 를 부르지 않고(ai_call_logs 0건, 일일 한도 미소모) 노출을 recommendation_items 에
남기며, 카드 탭은 POST /recommendations/{log_id}/accept 로 받는다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import MealItem, MealRecord, RecommendationItem, RecommendationLog, User
from tests.test_meals import MEAL_PAYLOAD


@pytest.fixture()
def v2_engine():
    before = settings.recommend_engine
    settings.recommend_engine = "v2"
    yield
    settings.recommend_engine = before


def _history(db, user_id: int, foods, *, days=(1, 2, 3), meal_type="lunch"):
    """최근 며칠 같은 끼니에 foods 를 먹은 것으로 기록 (엔진 신호용)."""
    now = datetime.now(UTC)
    for d in days:
        record = MealRecord(
            user_id=user_id, meal_type=meal_type, eaten_at=now - timedelta(days=d), is_skipped=False,
            total_calories=sum(f[1] for f in foods), total_carbs=sum(f[2] for f in foods),
            total_protein=sum(f[3] for f in foods), total_fat=sum(f[4] for f in foods),
        )
        db.add(record)
        db.flush()
        for name, kcal, carbs, protein, fat in foods:
            db.add(MealItem(meal_record_id=record.id, food_name=name, serving_amount=1.0,
                            calories=kcal, carbs=carbs, protein=protein, fat=fat))
    db.commit()


def _user_id(db) -> int:
    return db.scalar(select(User.id).order_by(User.id))


def test_menu_defaults_to_legacy_engine(client, auth_headers):
    res = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "lunch"})
    assert res.status_code == 200
    body = res.json()
    assert body["engine"] == "legacy" and body["ai_call_log_id"] > 0
    assert body["recommendation_log_id"] is None and body["budget"] is None
    assert body["recommended_menus"][0]["source"] is None  # v2 필드는 비어 있다


def test_legacy_menu_accepts_omitted_meal_type(client, auth_headers):
    res = client.post("/v1/recommendations/menu", headers=auth_headers, json={})
    assert res.status_code == 200 and res.json()["engine"] == "legacy"


def test_v2_menu_response_shape_and_no_ai_call(client, auth_headers, db_factory, v2_engine):
    db = db_factory()
    uid = _user_id(db)
    _history(db, uid, [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    _history(db, uid, [("제육볶음", 480, 20, 30, 30)], days=(4, 5))

    res = client.post(
        "/v1/recommendations/menu", headers=auth_headers,
        json={"meal_type": "lunch", "preferred_category": "home_meal", "mood": "light"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["engine"] == "v2" and body["ai_call_log_id"] is None
    assert body["recommendation_log_id"] > 0 and body["alternative_menus"] == []
    assert body["caution_text"]
    budget = body["budget"]
    assert budget["meal_type"] == "lunch" and budget["meal_budget"] > 0 and budget["goal_calories"] > 0
    assert budget["ratio_source"] in ("personal", "default")

    menus = body["recommended_menus"]
    assert menus, body
    names = {m["name"] for m in menus}
    assert names & {"김치찌개", "제육볶음"}
    for m in menus:
        assert m["source"] in ("personal", "popular", "similar")
        assert m["budget_label"] in ("fit", "light", "heavy")
        assert m["total_calories"] >= m["estimated_calories"]
        assert m["category"] == "home_meal" and m["reason"]
        assert isinstance(m["exceed_flag"], bool)

    # AI 를 부르지 않았다 → ai_call_logs 0건, 노출 로그는 카드 수만큼
    logs = client.get("/v1/ai-call-logs", headers=auth_headers, params={"task_type": "recommend"})
    assert logs.json()["pagination"]["total"] == 0
    rows = db.scalars(
        select(RecommendationItem).where(RecommendationItem.log_id == body["recommendation_log_id"])
    ).all()
    assert [r.name for r in rows] == [m["name"] for m in menus]
    log = db.get(RecommendationLog, body["recommendation_log_id"])
    assert log.user_id == uid and log.meal_context["engine"] == "v2" and log.ai_call_log_id is None


def test_v2_menu_infers_meal_type_when_omitted(client, auth_headers, v2_engine):
    res = client.post("/v1/recommendations/menu", headers=auth_headers, json={})
    assert res.status_code == 200
    body = res.json()
    assert body["engine"] == "v2"
    assert body["budget"]["meal_type"] in ("breakfast", "lunch", "dinner", "snack")
    # 이력이 전혀 없으면 카드가 비어도 200 — FE 는 빈 목록을 안내 문구로 처리한다
    assert isinstance(body["recommended_menus"], list)


def test_v2_menu_does_not_consume_daily_limit(client, auth_headers, db_factory, v2_engine):
    db = db_factory()
    _history(db, _user_id(db), [("김치찌개", 320, 18, 22, 16)])
    before = settings.recommend_daily_limit
    settings.recommend_daily_limit = 1
    try:
        for _ in range(3):  # 한도 1 이지만 v2 는 소모하지 않는다
            assert client.post("/v1/recommendations/menu", headers=auth_headers,
                               json={"meal_type": "lunch"}).status_code == 200
    finally:
        settings.recommend_daily_limit = before


def test_accept_marks_item_and_is_idempotent(client, auth_headers, db_factory, v2_engine):
    db = db_factory()
    _history(db, _user_id(db), [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    body = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "lunch"}).json()
    log_id, name = body["recommendation_log_id"], body["recommended_menus"][0]["name"]

    res = client.post(f"/v1/recommendations/{log_id}/accept", headers=auth_headers, json={"name": name})
    assert res.status_code == 200 and res.json() == {"accepted": True}
    row = db.scalar(select(RecommendationItem).where(RecommendationItem.log_id == log_id, RecommendationItem.rank == 1))
    first = row.accepted_at
    assert first is not None

    again = client.post(f"/v1/recommendations/{log_id}/accept", headers=auth_headers, json={"name": name})
    assert again.status_code == 200
    db.expire_all()
    assert db.get(RecommendationItem, row.id).accepted_at == first

    missing = client.post(f"/v1/recommendations/{log_id}/accept", headers=auth_headers, json={"name": "없는메뉴"})
    assert missing.status_code == 404
    assert client.post("/v1/recommendations/999999/accept", headers=auth_headers, json={"name": name}).status_code == 404
    assert client.post(f"/v1/recommendations/{log_id}/accept", headers=auth_headers, json={"name": ""}).status_code == 400  # 검증 오류는 400 (app 공통)


def test_saving_recommended_food_marks_it_eaten(client, auth_headers, db_factory, v2_engine):
    """추천 → 그 메뉴로 기록 저장(create_meal) → 노출 항목에 eaten_at 이 붙는다 (행동 로그 폐루프)."""
    db = db_factory()
    _history(db, _user_id(db), [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    body = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "lunch"}).json()
    log_id = body["recommendation_log_id"]
    picked = next(m for m in body["recommended_menus"] if m["name"] == "김치찌개")

    payload = {**MEAL_PAYLOAD, "items": [{**MEAL_PAYLOAD["items"][0], "food_name": picked["name"]}]}
    created = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert created.status_code == 201, created.text

    db.expire_all()
    row = db.scalar(select(RecommendationItem).where(RecommendationItem.log_id == log_id, RecommendationItem.name == "김치찌개"))
    assert row.eaten_at is not None and row.eaten_meal_record_id == created.json()["meal_id"]
