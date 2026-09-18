"""POST /recommendations/menu — settings.recommend_engine 로 legacy(AI) ↔ v2(이력 엔진) 전환.

v2 는 AI 를 부르지 않고(ai_call_logs 0건, 일일 한도 미소모) 노출을 recommendation_items 에
남기며, 카드 탭은 POST /recommendations/{log_id}/accept 로 받는다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import recommendations as recommendation_routes
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


def test_menu_keeps_explicit_legacy_engine_compatible(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_engine", "legacy")
    res = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "lunch"})
    assert res.status_code == 200
    body = res.json()
    assert body["engine"] == "legacy" and body["ai_call_log_id"] > 0
    assert body["recommendation_log_id"] is None and body["budget"] is None
    assert body["recommended_menus"][0]["source"] is None  # v2 필드는 비어 있다


def test_legacy_menu_accepts_omitted_meal_type(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_engine", "legacy")
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
    assert len(menus) == 3, body
    # 최근 먹은 메뉴를 강제로 유지하지 않는다. 유사 음식도 개인 이력에 기반한 추천이다.
    assert any(m["source"] in ("personal", "similar") for m in menus)
    for m in menus:
        assert m["source"] in ("personal", "popular", "similar", "catalog", "collaborative")
        assert m["recommendation_item_id"] > 0
        if m["source"] == "similar":
            assert any(anchor in m["reason"] for anchor in ("김치찌개", "제육볶음"))
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
    assert [r.id for r in rows] == [m["recommendation_item_id"] for m in menus]
    assert all(r.shown_at is None for r in rows)
    log = db.get(RecommendationLog, body["recommendation_log_id"])
    assert log.user_id == uid and log.meal_context["engine"] == "v2" and log.ai_call_log_id is None


def test_v2_menu_infers_meal_type_and_logs_catalog_for_new_user(
    client, auth_headers, db_factory, v2_engine, monkeypatch,
):
    # 실제 엔진을 실행하면서 결과를 보관해, API 응답과 DB 노출 점수가 일치하는지 확인한다.
    run_engine = recommendation_routes.recommend_v2
    engine_results = []

    def capture_result(*args, **kwargs):
        now = datetime.now(UTC).replace(hour=3, minute=0, second=0, microsecond=0)  # KST 점심
        result = run_engine(*args, now=now, **kwargs)
        engine_results.append(result)
        return result

    monkeypatch.setattr(recommendation_routes, "recommend_v2", capture_result)
    res = client.post("/v1/recommendations/menu", headers=auth_headers, json={})
    assert res.status_code == 200
    body = res.json()
    assert body["engine"] == "v2" and body["ai_call_log_id"] is None
    assert body["budget"]["meal_type"] == "lunch"
    menus = body["recommended_menus"]
    assert len(menus) == 3
    assert all(menu["source"] == "catalog" for menu in menus)
    assert len({menu["name"] for menu in menus}) == 3
    with db_factory() as db:
        rows = db.scalars(
            select(RecommendationItem)
            .where(RecommendationItem.log_id == body["recommendation_log_id"])
            .order_by(RecommendationItem.rank)
        ).all()
        assert [row.name for row in rows] == [menu["name"] for menu in menus]
        assert [row.rank for row in rows] == [1, 2, 3]
        assert all(row.source == "catalog" for row in rows)
        assert [float(row.score) for row in rows] == pytest.approx(
            [item.score for item in engine_results[0].items]
        )


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
    picked = body["recommended_menus"][0]
    impression = client.post(
        f"/v1/recommendations/items/{picked['recommendation_item_id']}/feedback",
        headers=auth_headers, json={"action": "impression"},
    )
    assert impression.status_code == 200

    payload = {
        **MEAL_PAYLOAD,
        "recommendation_item_id": picked["recommendation_item_id"],
        "eaten_at": datetime.now(UTC).isoformat(),
        "items": [{
            **MEAL_PAYLOAD["items"][0],
            "nutrition_item_id": None,
            "food_name": picked["name"],
            "calories": picked["estimated_calories"],
        }],
    }
    created = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert created.status_code == 201, created.text

    db.expire_all()
    row = db.scalar(select(RecommendationItem).where(
        RecommendationItem.log_id == log_id, RecommendationItem.name == picked["name"],
    ))
    assert row.eaten_at is not None and row.eaten_meal_record_id == created.json()["meal_id"]


def test_old_frontend_can_accept_and_save_without_item_id(client, auth_headers, db_factory, v2_engine):
    """구 앱의 로그ID+이름 탭과 카드ID 없는 식사 저장을 계속 지원한다."""
    response = client.post("/v1/recommendations/menu", headers=auth_headers, json={"meal_type": "lunch"})
    assert response.status_code == 200
    menu = response.json()
    picked = menu["recommended_menus"][0]
    accepted = client.post(
        f"/v1/recommendations/{menu['recommendation_log_id']}/accept",
        headers=auth_headers, json={"name": picked["name"]},
    )
    assert accepted.status_code == 200 and accepted.json() == {"accepted": True}
    payload = {
        **MEAL_PAYLOAD, "eaten_at": datetime.now(UTC).isoformat(),
        "items": [{**MEAL_PAYLOAD["items"][0], "nutrition_item_id": None, "food_name": picked["name"]}],
    }
    assert "recommendation_item_id" not in payload
    saved = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert saved.status_code == 201, saved.text
    with db_factory() as db:
        row = db.get(RecommendationItem, picked["recommendation_item_id"])
        assert row.shown_at is not None and row.accepted_at is not None
        assert row.eaten_at is None and row.eaten_meal_record_id is None
