"""분석 로그(정답지) 필드 — 입력 방식·AI 호출 ID 저장, 후보 is_selected 갱신,
슬라이더 양 조정 correction_logs (2026-09-11)."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models import AiCallLog, CorrectionLog, FoodCandidate, MealItem, MealRecord
from tests.conftest import login
from tests.test_meals import MEAL_PAYLOAD, create_meal


def _me(client, headers) -> int:
    return client.get("/v1/users/me", headers=headers).json()["id"]


def _seed_draft(db_factory, user_id: int, n: int = 2) -> tuple[int, list[int]]:
    """AI 호출 1건 + 후보 n개 (rank 1..n, 전부 미선택)."""
    with db_factory() as db:
        log = AiCallLog(
            user_id=user_id, task_type="image_analysis", provider="mock",
            model_name="mock", status="success", latency_ms=10,
        )
        db.add(log)
        db.flush()
        ids = []
        for rank in range(1, n + 1):
            cand = FoodCandidate(
                ai_call_log_id=log.id, nutrition_item_id=1, food_name=f"음식{rank}",
                normalized_name=f"음식{rank}", confidence_score=0.9 - rank * 0.1,
                estimated_serving=1.0, rank=rank,
            )
            db.add(cand)
            db.flush()
            ids.append(cand.id)
        db.commit()
        return log.id, ids


def _item(**over):
    base = {**MEAL_PAYLOAD["items"][1]}  # 공기밥, 보정 칩 없음
    base.update(over)
    return base


def test_entry_method_and_ai_call_log_saved(client, auth_headers, db_factory):
    log_id, _ = _seed_draft(db_factory, _me(client, auth_headers))
    meal_id = create_meal(
        client, auth_headers,
        {**MEAL_PAYLOAD, "entry_method": "photo", "ai_call_log_id": log_id},
    )["meal_id"]
    with db_factory() as db:
        meal = db.get(MealRecord, meal_id)
        assert meal.entry_method == "photo"
        assert meal.ai_call_log_id == log_id


def test_fields_are_optional_for_old_clients(client, auth_headers, db_factory):
    meal_id = create_meal(client, auth_headers)["meal_id"]
    with db_factory() as db:
        meal = db.get(MealRecord, meal_id)
        assert meal.entry_method is None
        assert meal.ai_call_log_id is None


def test_other_users_ai_call_log_is_nulled_not_rejected(client, auth_headers, db_factory):
    other = {"Authorization": f"Bearer {login(client, 'someone-else')['access_token']}"}
    log_id, _ = _seed_draft(db_factory, _me(client, other))
    meal_id = create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "ai_call_log_id": log_id}
    )["meal_id"]
    with db_factory() as db:
        assert db.get(MealRecord, meal_id).ai_call_log_id is None


def test_unknown_ai_call_log_is_nulled(client, auth_headers, db_factory):
    meal_id = create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "ai_call_log_id": 999_999}
    )["meal_id"]
    with db_factory() as db:
        assert db.get(MealRecord, meal_id).ai_call_log_id is None


def test_food_candidate_marked_selected(client, auth_headers, db_factory):
    log_id, (c1, c2) = _seed_draft(db_factory, _me(client, auth_headers))
    create_meal(
        client, auth_headers,
        {**MEAL_PAYLOAD, "ai_call_log_id": log_id,
         "items": [_item(food_candidate_id=c2)]},
    )
    with db_factory() as db:
        assert db.get(FoodCandidate, c2).is_selected is True
        assert db.get(FoodCandidate, c1).is_selected is False


def test_reselect_moves_is_selected_within_same_call(client, auth_headers, db_factory):
    """같은 AI 호출의 초안을 다시 저장하면 마지막 선택만 True 로 남는다."""
    log_id, (c1, c2) = _seed_draft(db_factory, _me(client, auth_headers))
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(food_candidate_id=c1)]})
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(food_candidate_id=c2)]})
    with db_factory() as db:
        assert db.get(FoodCandidate, c1).is_selected is False
        assert db.get(FoodCandidate, c2).is_selected is True


def test_other_users_candidate_is_ignored(client, auth_headers, db_factory):
    other = {"Authorization": f"Bearer {login(client, 'someone-else')['access_token']}"}
    _, (c1, _) = _seed_draft(db_factory, _me(client, other))
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(food_candidate_id=c1)]})
    with db_factory() as db:
        assert db.get(FoodCandidate, c1).is_selected is False


def test_update_without_candidate_ids_keeps_selection(client, auth_headers, db_factory):
    """기록 수정 화면은 후보 ID 를 모른다 — 안 보내면 is_selected 를 건드리지 않는다."""
    _, (c1, _) = _seed_draft(db_factory, _me(client, auth_headers))
    meal_id = create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(food_candidate_id=c1)]}
    )["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}", headers=auth_headers,
        json={"items": [_item(serving_amount=0.5, calories=150)]},
    )
    assert res.status_code == 200, res.text
    with db_factory() as db:
        assert db.get(FoodCandidate, c1).is_selected is True


def test_serving_adjustment_logged_when_differs_from_estimate(client, auth_headers, db_factory):
    meal_id = create_meal(
        client, auth_headers,
        {**MEAL_PAYLOAD, "items": [_item(serving_amount=0.5, estimated_serving=1.0)]},
    )["meal_id"]
    with db_factory() as db:
        logs = list(db.scalars(
            select(CorrectionLog).where(CorrectionLog.meal_record_id == meal_id)
        ))
        assert len(logs) == 1
        assert logs[0].correction_type == "serving_adjusted"
        assert logs[0].before_data == {"serving_amount": 1.0}
        assert logs[0].after_data == {"serving_amount": 0.5}


def test_serving_adjustment_not_logged_when_equal_or_missing(client, auth_headers, db_factory):
    a = create_meal(
        client, auth_headers,
        {**MEAL_PAYLOAD, "items": [_item(serving_amount=1.0, estimated_serving=1.0)]},
    )["meal_id"]
    b = create_meal(client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(serving_amount=2.0)]})["meal_id"]
    with db_factory() as db:
        for meal_id in (a, b):
            assert db.scalars(
                select(CorrectionLog).where(CorrectionLog.meal_record_id == meal_id)
            ).first() is None


def test_serving_adjustment_does_not_leak_into_detail_correction_type(client, auth_headers):
    """상세 응답의 correction_type 은 칩 라벨용 — 양 조정 로그가 덮어쓰면 안 된다."""
    payload = {
        **MEAL_PAYLOAD,
        "items": [{**MEAL_PAYLOAD["items"][0], "serving_amount": 0.5, "estimated_serving": 1.0}],
    }
    meal_id = create_meal(client, auth_headers, payload)["meal_id"]
    body = client.get(f"/v1/meals/{meal_id}", headers=auth_headers).json()
    assert body["items"][0]["correction_type"] == "no_soup"


# --- 앱 1.9.0~1.12.3 의 직접 검색 음식: 화면용 임시 키가 후보 ID 로 온다 (2026-09-22) ---

def test_local_candidate_key_does_not_block_saving(client, auth_headers, db_factory):
    """초안에 직접 검색으로 추가한 음식 — food_candidate_id 가 "manual-1" 이어도 저장된다."""
    log_id, ids = _seed_draft(db_factory, _me(client, auth_headers))
    payload = {
        **MEAL_PAYLOAD, "entry_method": "photo", "ai_call_log_id": log_id,
        "items": [_item(food_candidate_id=ids[0]), _item(food_name="공기밥", food_candidate_id="manual-1")],
    }
    res = client.post("/v1/meals", headers=auth_headers, json=payload)
    assert res.status_code == 201, res.text
    with db_factory() as db:
        # AI 후보에서 온 항목의 정답지 표시는 그대로 동작한다
        assert db.get(FoodCandidate, ids[0]).is_selected is True
        assert db.scalar(select(func.count(MealItem.id)).where(MealItem.meal_record_id == res.json()["meal_id"])) == 2


def test_local_candidate_key_leaves_no_fake_serving_log(client, auth_headers, db_factory):
    """직접 고른 음식은 AI 추정량이 없다 — 앱이 기본값 1 을 보내도 양 조정 로그를 남기지 않는다."""
    meal_id = create_meal(
        client, auth_headers,
        {**MEAL_PAYLOAD, "items": [_item(serving_amount=0.5, estimated_serving=1, food_candidate_id="manual-3")]},
    )["meal_id"]
    with db_factory() as db:
        assert list(db.scalars(select(CorrectionLog).where(CorrectionLog.meal_record_id == meal_id))) == []


def test_numeric_string_candidate_id_still_accepted(client, auth_headers, db_factory):
    _, ids = _seed_draft(db_factory, _me(client, auth_headers))
    create_meal(client, auth_headers, {**MEAL_PAYLOAD, "items": [_item(food_candidate_id=str(ids[1]))]})
    with db_factory() as db:
        assert db.get(FoodCandidate, ids[1]).is_selected is True


def test_update_also_tolerates_local_candidate_key(client, auth_headers):
    meal_id = create_meal(client, auth_headers, MEAL_PAYLOAD)["meal_id"]
    res = client.patch(
        f"/v1/meals/{meal_id}", headers=auth_headers,
        json={"items": [_item(food_candidate_id="manual-2")]},
    )
    assert res.status_code == 200, res.text
