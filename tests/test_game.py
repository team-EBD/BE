"""게이미피케이션 v3 — 기본 지급·펫 유대·보상 멱등성·상점·무대 (핸드오프 §10)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.core.config import settings
from app.core.timeutil import KST, kst_date_of, now_utc
from app.services import game_catalog as catalog
from tests.conftest import login
from tests.test_meals import MEAL_PAYLOAD, create_meal


def _logical_today() -> date:
    return kst_date_of(now_utc(), settings.day_start_hour)


def meal_on(day: date, meal_type: str = "lunch", **extra) -> dict:
    """그 논리 날짜에 확실히 들어가는 KST 12:40 기록."""
    eaten = datetime.combine(day, datetime.min.time(), tzinfo=KST).replace(hour=12, minute=40)
    return {**MEAL_PAYLOAD, "meal_type": meal_type, "eaten_at": eaten.isoformat(), **extra}


# 음식 태그 해금 테스트용 nutrition_item_id (seed/game_food_tags.json 기준)
VEGETABLE = 43  # 샐러드 → vegetable
SOUP = 1  # 김치찌개 → soup
PLAIN = 8  # 공기밥 → 태그 없음


def food_item(nutrition_item_id: int | None, name: str = "음식") -> dict:
    return {
        "nutrition_item_id": nutrition_item_id,
        "food_name": name,
        "serving_amount": 1.0,
        "calories": 100,
        "carbs": 10.0,
        "protein": 5.0,
        "fat": 1.0,
    }


def meal_with(day: date, nutrition_item_ids: list, meal_type: str = "lunch") -> dict:
    """그 논리 날짜에 지정한 nutrition item 만 담은 기록 payload."""
    return meal_on(
        day,
        meal_type,
        items=[food_item(nid, f"음식{nid}") for nid in nutrition_item_ids],
    )


def collection_item(client, headers, code: str) -> dict:
    items = client.get("/v1/game/collection", headers=headers).json()["items"]
    return next(i for i in items if i["code"] == code)


def home(client, headers) -> dict:
    res = client.get("/v1/game/home", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


# --- 기본 지급 ---

def test_home_grants_default_pet_and_background_once(client, auth_headers):
    first = home(client, auth_headers)
    assert first["active_pet"]["item_code"] == "pet_cat"
    assert first["active_pet"]["display_name"] == "모카"
    assert first["active_pet"]["bond_level"] == 1
    assert first["active_pet"]["bond_days"] == 0
    slots = {(p["slot_type"], p["item_code"]) for p in first["stage"]["placements"]}
    assert ("pet", "pet_cat") in slots
    assert ("background", "bg_sunny_kitchen") in slots

    # 여러 번 호출해도 중복 행이 생기지 않는다
    for _ in range(3):
        again = home(client, auth_headers)
    assert len(again["stage"]["placements"]) == len(first["stage"]["placements"])

    collection = client.get("/v1/game/collection", headers=auth_headers).json()
    owned = [i for i in collection["items"] if i["owned"]]
    assert sorted(i["code"] for i in owned) == ["bg_sunny_kitchen", "pet_cat"]
    assert collection["slot_limits"] == {
        "pet": 1, "background": 1, "food": 5, "blaster": 3, "event_prop": 2
    }


def test_new_user_starts_with_zero_points(client, auth_headers):
    assert home(client, auth_headers)["profile"]["points"] == 0


def test_each_user_has_independent_profile(client):
    a = {"Authorization": f"Bearer {login(client, 'a')['access_token']}"}
    b = {"Authorization": f"Bearer {login(client, 'b')['access_token']}"}
    create_meal(client, a, meal_on(_logical_today()))
    assert home(client, a)["profile"]["points"] > 0
    assert home(client, b)["profile"]["points"] == 0


# --- 기록 보상 ---

def test_meal_create_returns_rewards(client, auth_headers):
    body = create_meal(client, auth_headers, meal_on(_logical_today()))
    rewards = body["rewards"]
    assert rewards["xp"] == catalog.XP_PER_MEAL
    assert rewards["points"] == catalog.POINTS_PER_MEAL
    assert rewards["current_streak"] == 1
    assert rewards["pet_growth"]["pet_code"] == "pet_cat"
    assert rewards["pet_growth"]["bond_day_added"] is True
    assert rewards["pet_growth"]["bond_days"] == 1
    assert home(client, auth_headers)["profile"]["points"] == catalog.POINTS_PER_MEAL


def test_photo_meal_gives_more_xp(client, auth_headers):
    image = client.post(
        "/v1/meals/images",
        headers=auth_headers,
        files={"image": ("a.jpg", b"\xff\xd8\xff", "image/jpeg")},
        data={"source": "camera"},
    ).json()
    payload = meal_on(_logical_today(), meal_image_id=image["meal_image_id"])
    assert create_meal(client, auth_headers, payload)["rewards"]["xp"] == (
        catalog.XP_PER_PHOTO_MEAL
    )


def test_skipped_meal_gives_no_reward(client, auth_headers):
    payload = {
        "meal_type": "dinner",
        "eaten_at": meal_on(_logical_today())["eaten_at"],
        "is_skipped": True,
        "items": [],
    }
    assert create_meal(client, auth_headers, payload)["rewards"] is None
    profile = home(client, auth_headers)["profile"]
    assert profile["points"] == 0
    assert home(client, auth_headers)["active_pet"]["bond_days"] == 0


def test_daily_reward_cap_is_four_meals(client, auth_headers):
    today = _logical_today()
    for _ in range(catalog.MAX_REWARDED_MEALS_PER_DAY):
        assert create_meal(client, auth_headers, meal_on(today, "snack"))["rewards"]["points"] > 0
    fifth = create_meal(client, auth_headers, meal_on(today, "snack"))["rewards"]
    assert fifth["xp"] == 0
    assert fifth["points"] == 0


def test_full_day_bonus_once(client, auth_headers):
    today = _logical_today()
    create_meal(client, auth_headers, meal_on(today, "breakfast"))
    create_meal(client, auth_headers, meal_on(today, "lunch"))
    third = create_meal(client, auth_headers, meal_on(today, "dinner"))["rewards"]
    assert third["points"] == catalog.POINTS_PER_MEAL + catalog.POINTS_DAILY_COMPLETE
    # 같은 날 한 번 더 저녁을 기록해도 완주 보너스는 다시 주지 않는다
    fourth = create_meal(client, auth_headers, meal_on(today, "dinner"))["rewards"]
    assert fourth["points"] == catalog.POINTS_PER_MEAL


def test_deleted_meal_keeps_rewards_and_cannot_farm(client, auth_headers):
    today = _logical_today()
    created = create_meal(client, auth_headers, meal_on(today))
    before = home(client, auth_headers)["profile"]["points"]
    client.delete(f"/v1/meals/{created['meal_id']}", headers=auth_headers)
    # 회수하지 않는다
    assert home(client, auth_headers)["profile"]["points"] == before
    # 재생성해도 하루 상한 안에서만 지급된다
    for _ in range(catalog.MAX_REWARDED_MEALS_PER_DAY):
        create_meal(client, auth_headers, meal_on(today, "snack"))
    capped = home(client, auth_headers)["profile"]["points"]
    assert capped == catalog.POINTS_PER_MEAL * catalog.MAX_REWARDED_MEALS_PER_DAY


# --- 펫 유대 ---

def test_bond_rises_once_per_logical_date(client, auth_headers):
    today = _logical_today()
    create_meal(client, auth_headers, meal_on(today, "breakfast"))
    second = create_meal(client, auth_headers, meal_on(today, "lunch"))["rewards"]
    assert second["pet_growth"]["bond_day_added"] is False
    assert second["pet_growth"]["bond_days"] == 1


def test_bond_levels_up_and_unlocks_skill_at_seven_days(client, auth_headers):
    today = _logical_today()
    levels = []
    for offset in range(6, -1, -1):
        rewards = create_meal(client, auth_headers, meal_on(today - timedelta(days=offset)))["rewards"]
        levels.append(rewards["pet_growth"])

    assert levels[2]["bond_level_after"] == 2  # 3일
    assert "name_tag" in levels[2]["unlocked_growth_layers"]
    assert levels[6]["bond_days"] == 7
    assert levels[6]["bond_level_after"] == 3
    assert levels[6]["unlocked_skill"] == "streak_pause"

    detail = client.get("/v1/game/pets/pet_cat", headers=auth_headers).json()
    assert [step["days"] for step in detail["growth"]] == [0, 3, 7, 14, 30]
    assert detail["growth"][2]["reached"] is True
    assert detail["growth"][3]["reached"] is False

    skills = client.get("/v1/game/skills", headers=auth_headers).json()
    assert [s["code"] for s in skills["skills"]] == ["streak_pause"]
    # 첫 스킬은 자동 장착된다
    assert skills["equipped_skill_code"] == "streak_pause"
    assert home(client, auth_headers)["equipped_skill"]["code"] == "streak_pause"


def test_bond_days_never_decrease_after_a_gap(client, auth_headers):
    today = _logical_today()
    create_meal(client, auth_headers, meal_on(today - timedelta(days=20)))
    create_meal(client, auth_headers, meal_on(today - timedelta(days=19)))
    assert home(client, auth_headers)["active_pet"]["bond_days"] == 2
    # 긴 공백 뒤에 다시 기록해도 유대는 줄지 않는다 (스트릭만 끊긴다)
    rewards = create_meal(client, auth_headers, meal_on(today))["rewards"]
    assert rewards["pet_growth"]["bond_days"] == 3
    assert rewards["current_streak"] == 1


def test_pet_nickname(client, auth_headers):
    res = client.patch(
        "/v1/game/pets/pet_cat", headers=auth_headers, json={"nickname": "나비"}
    )
    assert res.status_code == 200
    assert home(client, auth_headers)["active_pet"]["display_name"] == "나비"
    client.patch("/v1/game/pets/pet_cat", headers=auth_headers, json={"nickname": ""})
    assert home(client, auth_headers)["active_pet"]["display_name"] == "모카"


def test_rename_unowned_pet_rejected(client, auth_headers):
    res = client.patch(
        "/v1/game/pets/pet_lion", headers=auth_headers, json={"nickname": "레오"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "ITEM_NOT_OWNED"


# --- 스트릭과 논리 날짜 경계 ---

def test_streak_counts_consecutive_logical_days(client, auth_headers):
    today = _logical_today()
    for offset in (2, 1, 0):
        rewards = create_meal(client, auth_headers, meal_on(today - timedelta(days=offset)))["rewards"]
    assert rewards["current_streak"] == 3


def test_day_boundary_is_kst_six_am(client, auth_headers):
    """05:59 와 06:00 은 서로 다른 논리 날짜다."""
    today = _logical_today()
    base = datetime.combine(today, datetime.min.time(), tzinfo=KST) + timedelta(days=1)
    before = create_meal(
        client, auth_headers, {**MEAL_PAYLOAD, "eaten_at": base.replace(hour=5, minute=59).isoformat()}
    )["rewards"]
    after = create_meal(
        client,
        auth_headers,
        {**MEAL_PAYLOAD, "meal_type": "breakfast", "eaten_at": base.replace(hour=6, minute=0).isoformat()},
    )["rewards"]
    assert before["pet_growth"]["bond_day_added"] is True
    assert after["pet_growth"]["bond_day_added"] is True
    assert after["pet_growth"]["bond_days"] == 2


# --- 해금 ---

def test_seven_day_streak_unlocks_food(client, auth_headers):
    today = _logical_today()
    unlocked = []
    for offset in range(6, -1, -1):
        unlocked = create_meal(
            client, auth_headers, meal_on(today - timedelta(days=offset))
        )["rewards"]["unlocked_items"]
    assert "food_apple" in unlocked
    collection = client.get("/v1/game/collection", headers=auth_headers).json()
    assert any(i["code"] == "food_apple" and i["owned"] for i in collection["items"])


def test_home_shows_next_unlock_goal(client, auth_headers):
    """음식 목표보다 스트릭이 가까우면 스트릭 목표를 보여 준다."""
    today = _logical_today()
    for offset in (2, 1, 0):
        # 태그가 없는 음식만 기록 — 음식 목표는 5일 그대로 남는다
        create_meal(client, auth_headers, meal_with(today - timedelta(days=offset), [PLAIN]))
    goal = home(client, auth_headers)["next_unlock"]
    assert goal["item_code"] == "food_apple"
    assert goal["target"] == 7
    assert goal["current"] == 3
    assert goal["label"] == "4일 더 기록하면 빨간 사과"


# --- 첫 친구 선택 ---

def test_first_friend_requires_three_record_days_and_is_idempotent(client, auth_headers):
    today = _logical_today()
    assert home(client, auth_headers)["first_friend"]["available"] is False
    early = client.post(
        "/v1/game/adoption/first-choice", headers=auth_headers, json={"item_code": "pet_dog"}
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "FIRST_FRIEND_NOT_READY"

    for offset in (2, 1, 0):
        create_meal(client, auth_headers, meal_on(today - timedelta(days=offset)))
    offer = home(client, auth_headers)["first_friend"]
    assert offer["available"] is True
    assert offer["choices"] == ["pet_dog", "pet_bunny", "pet_fox"]

    first = client.post(
        "/v1/game/adoption/first-choice", headers=auth_headers, json={"item_code": "pet_dog"}
    )
    assert first.status_code == 200
    assert first.json()["already_claimed"] is False
    again = client.post(
        "/v1/game/adoption/first-choice", headers=auth_headers, json={"item_code": "pet_fox"}
    )
    assert again.json()["already_claimed"] is True

    collection = client.get("/v1/game/collection", headers=auth_headers).json()
    owned = {i["code"] for i in collection["items"] if i["owned"]}
    assert "pet_dog" in owned and "pet_fox" not in owned


def test_first_friend_rejects_non_choice_pet(client, auth_headers):
    res = client.post(
        "/v1/game/adoption/first-choice", headers=auth_headers, json={"item_code": "pet_lion"}
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "GAME_ITEM_NOT_FOUND"


# --- 상점 ---

def _give_points(client, headers, amount: int, db_factory=None) -> None:
    """가격 테스트용 코인 충전 — 기록으로 벌기엔 느리므로 직접 넣는다."""
    from sqlalchemy import select

    from app.models import GameProfile

    home(client, headers)  # 프로필 보장
    db = db_factory()
    try:
        profile = db.scalar(select(GameProfile))
        profile.points = amount
        db.commit()
    finally:
        db.close()


def test_purchase_is_idempotent_and_deducts_once(client, auth_headers, db_factory):
    _give_points(client, auth_headers, 500, db_factory)
    body = {"item_code": "pet_dog", "idempotency_key": "client-uuid-1"}
    first = client.post("/v1/game/shop/purchase", headers=auth_headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["points"] == 500 - 120  # v3 가격 인하 반영

    retry = client.post("/v1/game/shop/purchase", headers=auth_headers, json=body)
    assert retry.status_code == 200
    assert retry.json()["already_purchased"] is True
    assert retry.json()["points"] == 380


def test_purchase_rejects_insufficient_points(client, auth_headers, db_factory):
    _give_points(client, auth_headers, 10, db_factory)
    res = client.post(
        "/v1/game/shop/purchase",
        headers=auth_headers,
        json={"item_code": "pet_dog", "idempotency_key": "k"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "INSUFFICIENT_POINTS"


def test_purchase_rejects_already_owned(client, auth_headers, db_factory):
    _give_points(client, auth_headers, 5000, db_factory)
    res = client.post(
        "/v1/game/shop/purchase",
        headers=auth_headers,
        json={"item_code": "bg_sunny_kitchen", "idempotency_key": "k"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "GAME_ITEM_ALREADY_OWNED"


def test_purchase_respects_account_level_lock(client, auth_headers, db_factory):
    _give_points(client, auth_headers, 5000, db_factory)
    res = client.post(
        "/v1/game/shop/purchase",
        headers=auth_headers,
        json={"item_code": "pet_lion", "idempotency_key": "k"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "LEVEL_LOCKED"


def test_shop_hides_owned_and_non_sale_items(client, auth_headers):
    shop = client.get("/v1/game/shop", headers=auth_headers).json()
    codes = {i["code"] for i in shop["items"]}
    assert "pet_cat" not in codes  # 기본 지급
    assert "food_apple" not in codes  # 스트릭 전용
    assert "event_chest" not in codes  # 이벤트 전용
    assert {"pet_dog", "bg_sunset_rooftop", "blaster_a"} <= codes


# --- 무대 배치 ---

def _stage_body(client, headers, placements):
    revision = home(client, headers)["stage"]["revision"]
    return {"revision": revision, "placements": placements}


def test_stage_save_bumps_revision_and_conflicts(client, auth_headers):
    body = _stage_body(
        client,
        auth_headers,
        [
            {"slot_type": "background", "slot_index": 0, "item_code": "bg_sunny_kitchen"},
            {
                "slot_type": "pet",
                "slot_index": 0,
                "item_code": "pet_cat",
                "transform": {"x": 0.5, "y": 0.6, "scale": 1},
            },
        ],
    )
    first = client.put("/v1/game/stage", headers=auth_headers, json=body)
    assert first.status_code == 200
    assert first.json()["revision"] == body["revision"] + 1

    stale = client.put("/v1/game/stage", headers=auth_headers, json=body)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STAGE_REVISION_CONFLICT"


def test_stage_rejects_unowned_wrong_category_and_over_limit(client, auth_headers):
    def put(placements):
        return client.put(
            "/v1/game/stage", headers=auth_headers, json=_stage_body(client, auth_headers, placements)
        )

    not_owned = put([{"slot_type": "pet", "slot_index": 0, "item_code": "pet_lion"}])
    assert not_owned.status_code == 400
    assert not_owned.json()["error"]["code"] == "ITEM_NOT_OWNED"

    mismatch = put([{"slot_type": "pet", "slot_index": 0, "item_code": "bg_sunny_kitchen"}])
    assert mismatch.json()["error"]["code"] == "ITEM_CATEGORY_MISMATCH"

    over = put([{"slot_type": "pet", "slot_index": 1, "item_code": "pet_cat"}])
    assert over.json()["error"]["code"] == "STAGE_LIMIT_EXCEEDED"

    missing = put([{"slot_type": "pet", "slot_index": 0, "item_code": "pet_nope"}])
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "GAME_ITEM_NOT_FOUND"


def test_stage_pet_becomes_active_pet_for_rewards(client, auth_headers, db_factory):
    _give_points(client, auth_headers, 500, db_factory)
    client.post(
        "/v1/game/shop/purchase",
        headers=auth_headers,
        json={"item_code": "pet_dog", "idempotency_key": "k"},
    )
    body = _stage_body(
        client,
        auth_headers,
        [{"slot_type": "pet", "slot_index": 0, "item_code": "pet_dog"}],
    )
    assert client.put("/v1/game/stage", headers=auth_headers, json=body).status_code == 200
    assert home(client, auth_headers)["active_pet"]["item_code"] == "pet_dog"

    rewards = create_meal(client, auth_headers, meal_on(_logical_today()))["rewards"]
    assert rewards["pet_growth"]["pet_code"] == "pet_dog"


# --- 지원 스킬 ---

def test_equip_requires_unlocked_skill(client, auth_headers):
    res = client.put(
        "/v1/game/skills/equipped", headers=auth_headers, json={"skill_code": "daily_xp_nudge"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "SKILL_NOT_UNLOCKED"


def test_only_one_skill_equipped_and_can_unequip(client, auth_headers):
    today = _logical_today()
    for offset in range(6, -1, -1):
        create_meal(client, auth_headers, meal_on(today - timedelta(days=offset)))
    assert home(client, auth_headers)["equipped_skill"]["code"] == "streak_pause"

    res = client.put("/v1/game/skills/equipped", headers=auth_headers, json={"skill_code": None})
    assert res.status_code == 200
    assert res.json()["equipped_skill_code"] is None
    assert home(client, auth_headers)["equipped_skill"] is None


def test_streak_pause_protects_one_missed_day(client, auth_headers):
    """유대 7일로 쉼표 지킴이를 얻은 뒤 하루를 건너뛰어도 스트릭이 이어진다."""
    today = _logical_today()
    for offset in range(8, 1, -1):  # 8일 전 ~ 2일 전 (7일 연속)
        create_meal(client, auth_headers, meal_on(today - timedelta(days=offset)))
    assert home(client, auth_headers)["equipped_skill"]["code"] == "streak_pause"

    # 하루(1일 전)를 건너뛰고 오늘 기록 → 보호 발동
    rewards = create_meal(client, auth_headers, meal_on(today))["rewards"]
    assert rewards["current_streak"] == 9
    assert rewards["skill_effect"]["skill_code"] == "streak_pause"
    assert rewards["skill_effect"]["charges"] == 0
    assert home(client, auth_headers)["equipped_skill"]["charges"] == 0


# --- 기존 사용자 백필 ---

def test_existing_user_backfill_caps_at_seven_bond_days(client, auth_headers):
    """게임 도메인이 생기기 전 기록이 쌓인 사용자가 처음 홈에 들어온 경우."""
    today = _logical_today()
    for offset in range(14, -1, -1):
        create_meal_without_game(client, auth_headers, meal_on(today - timedelta(days=offset)))

    first = home(client, auth_headers)
    assert first["active_pet"]["bond_days"] == 7  # 최대 7일
    assert first["profile"]["total_record_days"] == 15
    assert first["profile"]["current_streak"] == 15
    assert first["profile"]["points"] == 0  # 소급 코인 없음
    assert first["first_friend"]["available"] is True

    # 다시 호출해도 백필이 반복되지 않는다
    assert home(client, auth_headers)["active_pet"]["bond_days"] == 7


def create_meal_without_game(client, headers, payload):
    """게임 도메인을 통째로 건너뛰고 기록만 남긴다 (게이미피케이션 도입 이전 상태).

    프로필 보장까지 막아야 '기록은 있는데 game_profiles 행이 없는' 기존 사용자가
    재현된다.
    """
    import app.services.meals as meals_service

    noop = lambda *_args, **_kwargs: None  # noqa: E731
    originals = (meals_service.apply_meal_rewards, meals_service.ensure_game_profile)
    meals_service.apply_meal_rewards = noop
    meals_service.ensure_game_profile = noop
    try:
        return create_meal(client, headers, payload)
    finally:
        meals_service.apply_meal_rewards, meals_service.ensure_game_profile = originals


# --- 레벨 곡선 ---

def test_level_curve_is_monotonic_and_matches_anchors():
    assert catalog.xp_to_next_level(1) == 50
    assert catalog.xp_to_next_level(2) == 70
    assert catalog.xp_to_next_level(3) == 95
    assert catalog.xp_to_next_level(10) == 420
    assert catalog.xp_to_next_level(50) == 2800
    values = [catalog.xp_to_next_level(lv) for lv in range(1, 61)]
    assert values == sorted(values)


def test_apply_xp_levels_up_and_keeps_remainder():
    level, xp, ups = catalog.apply_xp(1, 0, 130)
    assert (level, xp, ups) == (3, 10, 2)  # 50 + 70 소모, 10 남음


def test_bond_level_boundaries():
    assert [catalog.bond_level_for(d) for d in (0, 2, 3, 6, 7, 13, 14, 29, 30, 99)] == [
        1, 1, 2, 2, 3, 3, 4, 4, 5, 5
    ]


# --- 음식 태그 해금 (curated 매핑 + unlock_progress) ---

def test_vegetable_meals_on_five_days_unlock_broccoli(client, auth_headers):
    today = _logical_today()
    rewards = None
    for offset in range(4, -1, -1):
        rewards = create_meal(
            client, auth_headers, meal_with(today - timedelta(days=offset), [VEGETABLE])
        )["rewards"]

    progress = {p["item_code"]: p for p in rewards["progress_updates"]}
    assert progress["food_broccoli"] == {
        "item_code": "food_broccoli", "current": 5, "target": 5, "unlocked": True
    }
    assert "food_broccoli" in rewards["unlocked_items"]
    assert collection_item(client, auth_headers, "food_broccoli")["owned"] is True


def test_same_day_vegetable_twice_counts_one_day(client, auth_headers):
    today = _logical_today()
    first = create_meal(client, auth_headers, meal_with(today, [VEGETABLE], "lunch"))["rewards"]
    second = create_meal(client, auth_headers, meal_with(today, [VEGETABLE], "dinner"))["rewards"]

    assert {p["item_code"]: p["current"] for p in first["progress_updates"]}["food_broccoli"] == 1
    # 같은 논리 날짜는 다시 세지 않는다 → 진행도 변화가 없어 응답에도 실리지 않는다
    assert "food_broccoli" not in {p["item_code"] for p in second["progress_updates"]}
    assert collection_item(client, auth_headers, "food_broccoli")["progress"] == {
        "current": 1, "target": 5, "unit": "day"
    }


def test_free_text_item_does_not_move_progress(client, auth_headers):
    """nutrition_item_id 가 없는 자유 입력은 '확정한 음식'이 아니다."""
    today = _logical_today()
    rewards = create_meal(
        client, auth_headers, meal_on(today, "lunch", items=[food_item(None, "집밥")])
    )["rewards"]
    assert rewards["progress_updates"] == []
    assert collection_item(client, auth_headers, "food_taco")["progress"]["current"] == 0
    assert collection_item(client, auth_headers, "food_broccoli")["progress"]["current"] == 0


def test_five_breakfast_days_unlock_pancakes(client, auth_headers):
    today = _logical_today()
    rewards = None
    for offset in range(4, -1, -1):
        rewards = create_meal(
            client,
            auth_headers,
            meal_with(today - timedelta(days=offset), [PLAIN], "breakfast"),
        )["rewards"]
    assert "food_pancakes" in rewards["unlocked_items"]
    assert collection_item(client, auth_headers, "food_pancakes")["owned"] is True
    # 아침을 5일 기록했다고 채소 목표가 오르지는 않는다
    assert collection_item(client, auth_headers, "food_broccoli")["progress"]["current"] == 0


def test_eight_distinct_menus_unlock_taco(client, auth_headers):
    today = _logical_today()
    menus = [5, 6, 8, 10, 11, 15, 16]  # 태그가 없는 서로 다른 메뉴 7종
    for offset, nid in zip(range(9, 2, -1), menus):
        create_meal(client, auth_headers, meal_with(today - timedelta(days=offset), [nid]))
    assert collection_item(client, auth_headers, "food_taco")["progress"] == {
        "current": 7, "target": 8, "unit": "menu"
    }

    # 같은 메뉴를 다시 먹어도 가짓수는 늘지 않는다
    repeat = create_meal(
        client, auth_headers, meal_with(today - timedelta(days=1), [menus[0]])
    )["rewards"]
    assert "food_taco" not in {p["item_code"] for p in repeat["progress_updates"]}

    eighth = create_meal(client, auth_headers, meal_with(today, [20]))["rewards"]
    assert "food_taco" in eighth["unlocked_items"]
    assert {p["item_code"]: p for p in eighth["progress_updates"]}["food_taco"]["current"] == 8


def test_unlocked_food_is_not_granted_again(client, auth_headers):
    today = _logical_today()
    for offset in range(4, -1, -1):
        create_meal(client, auth_headers, meal_with(today - timedelta(days=offset), [VEGETABLE]))
    owned_before = {
        i["code"] for i in client.get("/v1/game/collection", headers=auth_headers).json()["items"]
        if i["owned"]
    }

    later = create_meal(
        client, auth_headers, meal_with(today + timedelta(days=1), [VEGETABLE])
    )["rewards"]
    assert "food_broccoli" not in later["unlocked_items"]
    assert "food_broccoli" not in {p["item_code"] for p in later["progress_updates"]}

    item = collection_item(client, auth_headers, "food_broccoli")
    assert item["owned"] is True
    assert item["progress"] is None  # 이미 보유한 아이템은 진행도를 내리지 않는다
    owned_after = {
        i["code"] for i in client.get("/v1/game/collection", headers=auth_headers).json()["items"]
        if i["owned"]
    }
    assert owned_after == owned_before


def test_next_unlock_prefers_closer_food_goal(client, auth_headers):
    """스트릭(7일)보다 가까운 음식 목표가 있으면 그쪽을 보여 준다."""
    today = _logical_today()
    for offset in (1, 0):
        create_meal(client, auth_headers, meal_with(today - timedelta(days=offset), [SOUP]))
    goal = home(client, auth_headers)["next_unlock"]
    assert goal["item_code"] == "food_soup"
    assert (goal["current"], goal["target"]) == (2, 5)
    assert goal["label"] == "국물 요리가 든 식사를 3일 더 기록하면 따뜻한 수프"


def test_meal_response_carries_progress_updates(client, auth_headers):
    rewards = create_meal(
        client, auth_headers, meal_with(_logical_today(), [SOUP])
    )["rewards"]
    by_code = {p["item_code"]: p for p in rewards["progress_updates"]}
    assert by_code["food_soup"] == {
        "item_code": "food_soup", "current": 1, "target": 5, "unlocked": False
    }
    # 같은 기록의 메뉴 가짓수도 함께 오른다
    assert by_code["food_taco"]["current"] == 1
    assert by_code["food_taco"]["target"] == 8
    assert "food_broccoli" not in by_code  # 채소는 없었다


def test_collection_shows_food_progress_units(client, auth_headers):
    create_meal(client, auth_headers, meal_with(_logical_today(), [SOUP]))
    assert collection_item(client, auth_headers, "food_soup")["progress"] == {
        "current": 1, "target": 5, "unit": "day"
    }
    assert collection_item(client, auth_headers, "food_taco")["progress"]["unit"] == "menu"
    # 스트릭 해금과 펫에는 진행도를 붙이지 않는다 (필드는 항상 존재한다)
    assert collection_item(client, auth_headers, "food_apple")["progress"] is None
    assert collection_item(client, auth_headers, "pet_cat")["progress"] is None


# --- 보상 실패 rollback (핸드오프 §6-B G) ---

def test_meal_is_saved_when_rewards_fail(client, auth_headers, db_factory, monkeypatch):
    """보상 계산이 터져도 ① 식단은 저장되고 ② rewards 는 null ③ 원장은 비어 있다."""
    from sqlalchemy import func, select

    import app.services.meals as meals_service
    from app.models import RewardLedger, UnlockProgress

    def boom(*_args, **_kwargs):
        raise RuntimeError("보상 계산 실패")

    monkeypatch.setattr(meals_service, "apply_meal_rewards", boom)

    res = client.post(
        "/v1/meals", headers=auth_headers, json=meal_with(_logical_today(), [SOUP])
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["rewards"] is None

    detail = client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers)
    assert detail.status_code == 200
    assert [i["food_name"] for i in detail.json()["items"]] == ["음식1"]

    db = db_factory()
    try:
        assert db.scalar(select(func.count(RewardLedger.id))) == 0
        assert db.scalar(select(func.count(UnlockProgress.id))) == 0
    finally:
        db.close()


# --- 음식 태그 2계층 (curated → 이름 키워드 폴백) ---

def _add_nutrition_item(db_factory, name: str) -> int:
    """curated 46종 밖의 영양 항목 (식약처 OpenAPI 적재분과 같은 상황)."""
    from app.models import NutritionItem

    db = db_factory()
    try:
        row = NutritionItem(
            name=name,
            normalized_name=name.replace(" ", ""),
            base_amount=100,
            base_unit="g",
            calories=200,
            carbs=10,
            protein=20,
            fat=8,
            category="외식",
            source="public",
        )
        db.add(row)
        db.commit()
        assert row.id > 46  # 시드 46종 밖이어야 폴백을 탄다
        return row.id
    finally:
        db.close()


def test_curated_tags_beat_keyword_fallback():
    """curated 의 빈 배열은 '태그 없음'이라는 판단이므로 키워드로 덮이지 않는다."""
    # 8 = 공기밥 (curated 빈 배열) — 이름에 키워드가 있어도 무태그로 남는다
    assert catalog.food_tags_for(8, "채소 듬뿍 샐러드") == frozenset()
    assert catalog.food_tags_for(25, "탕수육") == frozenset()  # 25 = 탕수육
    # curated 에 값이 있으면 그 값만 쓴다 (이름이 더 많은 태그를 암시해도)
    assert catalog.food_tags_for(1, "김치찌개") == frozenset({"soup"})
    assert catalog.food_tags_for(43, "샐러드") == frozenset({"vegetable"})


def test_keyword_fallback_tags_unknown_nutrition_item():
    """curated 에 없는 id 는 이름으로 태깅한다."""
    assert catalog.food_tags_for(9001, "훈제 연어 샐러드") == frozenset({"fish", "vegetable"})
    assert catalog.food_tags_for(9002, "황태해장국") == frozenset({"fish", "soup"})
    assert catalog.food_tags_for(9003, "시금치나물") == frozenset({"vegetable"})
    assert catalog.food_tags_for(9004, "블루베리") == frozenset({"fruit"})
    assert catalog.food_tags_for(9005, "순대국밥") == frozenset({"soup"})


def test_keyword_exclusions_block_false_positives():
    """가공품·동음이의에 태그가 붙으면 '안 먹었는데 진행도가 올랐다'가 된다."""
    for name in ("사과주스", "사과차", "딸기우유", "포도당", "수박바", "망고빙수"):
        assert "fruit" not in catalog.food_tags_from_name(name), name
    for name in ("유부초밥", "멸치육수", "참치액", "게맛살"):
        assert "fish" not in catalog.food_tags_from_name(name), name
    for name in ("보쌈", "야채빵", "배추김치", "과일샐러드", "옥수수샐러드", "당근케이크"):
        assert "vegetable" not in catalog.food_tags_from_name(name), name
    for name in ("탕수육", "그라탕", "설탕", "탕후루", "라면스프", "볶음라면", "스프링롤"):
        assert "soup" not in catalog.food_tags_from_name(name), name


def test_keyword_fallback_ignores_missing_name():
    assert catalog.food_tags_for(9001, None) == frozenset()
    assert catalog.food_tags_for(9001, "") == frozenset()
    assert catalog.food_tags_for(9001, "   ") == frozenset()
    assert catalog.food_tags_for(9001, "돈까스") == frozenset()
    # 자유 입력(nutrition_item_id 없음)은 이름이 무엇이든 진행도를 올리지 않는다
    assert catalog.food_tags_for(None, "연어 스테이크") == frozenset()


def test_unknown_nutrition_item_progresses_by_name(client, auth_headers, db_factory):
    """식약처 적재분처럼 curated 밖의 음식도 해금이 열린다."""
    salmon = _add_nutrition_item(db_factory, "연어 스테이크")
    juice = _add_nutrition_item(db_factory, "사과주스")
    today = _logical_today()

    rewards = None
    for offset in range(4, -1, -1):
        rewards = create_meal(
            client, auth_headers, meal_with(today - timedelta(days=offset), [salmon])
        )["rewards"]
    assert "food_sushi_salmon" in rewards["unlocked_items"]
    assert collection_item(client, auth_headers, "food_sushi_salmon")["owned"] is True

    # 가공 음료는 과일 진행도를 올리지 않는다
    create_meal(client, auth_headers, meal_with(today, [juice], "snack"))
    assert collection_item(client, auth_headers, "food_strawberry")["progress"]["current"] == 0
