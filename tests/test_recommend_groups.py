"""추천 엔진 v2 × 음식군 — 군 테이블이 있을 때의 동작 (docs/음식군-DB-계약.md §8).

군이 없는 DB 의 동작은 test_recommend_engine.py 가 검증한다. 여기서는 같은 코드가 군을
만났을 때 (1) 역할 컷 (2) 동반(밥) 합산 (3) 계열 유사 (4) 표시명 (5) 기록 이름 통합
(6) 예산 상한이 계약대로인지 본다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import FoodGroup, FoodGroupAlias, MealItem, MealRecord, NutritionItem, User
from app.services.recommend import recommend
from app.services.recommend.groups import GroupIndex, GroupInfo, load_group_index
from app.services.recommend.signals import (
    MAX_RATIO_BY_MEAL,
    companion_stats,
    decayed_frequency,
    meal_budget,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)  # KST 18:00 → dinner
STEW_FAMILY, RICE_FAMILY, BURGER_FAMILY = "국·탕·찌개류", "밥류", "버거·피자·샌드위치"


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _group(db, name, family, role, kcal=None, carbs=None, protein=None, fat=None, companion=None):
    g = FoodGroup(
        name=name, family=family, role=role, calories=kcal, carbs=carbs, protein=protein, fat=fat,
        companion_group_id=companion.id if companion else None,
    )
    db.add(g)
    db.flush()
    return g


def _alias(db, alias, group, kind="synonym"):
    db.add(FoodGroupAlias(alias=alias, group_id=group.id, kind=kind))
    db.flush()


def _user(db, social_id, email=None):
    user = User(social_provider="google", social_id=social_id, nickname=social_id, email=email)
    db.add(user)
    db.flush()
    return user


def _days_ago(n, hour_kst=17):
    return NOW.replace(hour=hour_kst - 9) - timedelta(days=n)  # KST hour → UTC


def _meal(db, user, meal_type, eaten_at, foods):
    """foods: [(name, kcal, carbs, protein, fat[, group_id])]."""
    record = MealRecord(
        user_id=user.id, meal_type=meal_type, eaten_at=eaten_at, is_skipped=False,
        total_calories=sum(f[1] for f in foods), total_carbs=sum(f[2] for f in foods),
        total_protein=sum(f[3] for f in foods), total_fat=sum(f[4] for f in foods),
    )
    db.add(record)
    db.flush()
    for f in foods:
        name, kcal, carbs, protein, fat = f[:5]
        db.add(
            MealItem(
                meal_record_id=record.id, food_name=name, serving_amount=1.0,
                calories=kcal, carbs=carbs, protein=protein, fat=fat,
                food_group_id=f[5] if len(f) > 5 else None,
            )
        )
    db.flush()
    return record


@pytest.fixture()
def taxonomy(db):
    """쌀밥(동반) · 김치찌개→쌀밥 · 된장찌개→쌀밥 · 김치(제외) · 햄버거 · 콜라(제외) 군과 alias."""
    rice = _group(db, "쌀밥", RICE_FAMILY, "companion", 300, 66, 5.5, 0.5)
    stew = _group(db, "김치찌개", STEW_FAMILY, "meal", 320, 18, 22, 16, companion=rice)
    soy = _group(db, "된장찌개", STEW_FAMILY, "meal", 250, 14, 22, 12, companion=rice)
    kimchi = _group(db, "김치", "김치·절임", "exclude", 30, 5, 2, 0.5)
    burger = _group(db, "햄버거", BURGER_FAMILY, "meal", 480, 40, 25, 22)
    cola = _group(db, "탄산음료", "음료", "exclude", 140, 36, 0, 0)
    _alias(db, "공기밥", rice)
    _alias(db, "빅소불고기버거", burger, kind="manual")
    _alias(db, "콜라", cola)
    return {"rice": rice, "stew": stew, "soy": soy, "kimchi": kimchi, "burger": burger, "cola": cola}


# --- 색인 -------------------------------------------------------------------


def test_index_resolves_snapshot_then_alias_then_group_name(db, taxonomy):
    index = load_group_index(db)
    assert index.enabled
    # 1) 기록 스냅샷 id 가 최우선 (이름이 달라도)
    assert index.resolve("아무이름", taxonomy["stew"].id).name == "김치찌개"
    # 2) 없거나 무효한 id 는 건너뛰고 alias
    assert index.resolve("공기밥", None, 999_999).name == "쌀밥"
    assert index.resolve("빅소불고기버거").name == "햄버거"
    # 3) 군명 정확일치, 공백·대소문자 무시
    assert index.resolve("김치 찌개").name == "김치찌개"
    # 4) 어미 추정은 하지 않는다 — 모르는 이름은 None
    assert index.resolve("순두부찌개") is None
    assert index.key_for("순두부찌개") == "순두부찌개"  # 이름 키 폴백
    assert index.companion_of(index.key_for("김치찌개")).name == "쌀밥"


def test_empty_index_when_no_groups(db):
    index = load_group_index(db)
    assert not index.enabled and index.resolve("김치찌개") is None
    assert isinstance(index, GroupIndex)


# --- 역할 컷 -----------------------------------------------------------------


def test_exclude_and_companion_roles_never_become_candidates(db, taxonomy):
    u = _user(db, "u1", email="u@gmail.com")
    other = _user(db, "u2", email="o@gmail.com")
    for d in range(1, 6):
        for who in (u, other):
            _meal(db, who, "dinner", _days_ago(d), [
                ("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5),
                ("김치", 30, 5, 2, 0.5), ("콜라", 140, 36, 0, 0),
            ])
    result = recommend(db, u.id, meal_type="dinner", now=NOW)
    names = {c.group_name for c in result.candidates}
    assert "김치찌개" in names
    assert not names & {"쌀밥", "김치", "탄산음료"}, names
    assert all(c.role in ("meal", "snack") for c in result.candidates)
    # 통계 자체에는 남아 있다 (예산·동반 계산 재료) — 컷은 생성기에서만
    keys = {s.group_name for s in decayed_frequency(db, u.id, "dinner", now=NOW)}
    assert {"김치찌개", "쌀밥", "김치", "탄산음료"} <= keys


# --- 동반(밥) -----------------------------------------------------------------


def test_default_companion_attached_and_budget_uses_total(db, taxonomy):
    """이력이 없는 사용자에게 인기 김치찌개가 뜨면 군의 기본 동반(쌀밥)이 붙고, 합산으로 예산을 본다."""
    other = _user(db, "o", email="o@gmail.com")
    for d in range(1, 4):
        _meal(db, other, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16)])
    newbie = _user(db, "n")
    result = recommend(db, newbie.id, meal_type="dinner", now=NOW)
    card = next(i for i in result.items if i.group_name == "김치찌개")
    assert card.source == "popular" and card.name == "김치찌개"
    assert card.companion_name == "쌀밥" and card.companion_kcal == 300
    assert card.total_calories == 620 and card.calories == 320
    assert "보통 함께 먹는 쌀밥 300 포함" in card.reason  # 기본 동반 문구
    # 예산 700(기본 0.35×2000) 에 620 → fit. 메인 320 만 보면 light 였을 것
    assert card.budget_label == "fit"


def test_personal_companion_overrides_default(db, taxonomy):
    """혼자 먹는 사람에겐 동반을 붙이지 않고, 라면과 같이 먹는 사람에겐 개인 동시기록이 우선."""
    alone = _user(db, "alone", email="a@gmail.com")
    for d in range(1, 5):
        _meal(db, alone, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16)])
    stats = companion_stats(db, alone.id, now=NOW, index=load_group_index(db))
    assert stats["김치찌개"].companion_key is None and stats["김치찌개"].meals == 4
    card = next(i for i in recommend(db, alone.id, meal_type="dinner", now=NOW).items if i.group_name == "김치찌개")
    assert card.companion_name is None and card.total_calories == 320

    with_rice = _user(db, "rice", email="r@gmail.com")
    for d in range(1, 5):
        _meal(db, with_rice, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    stats = companion_stats(db, with_rice.id, now=NOW, index=load_group_index(db))
    assert stats["김치찌개"].companion_key == "쌀밥"
    card = next(i for i in recommend(db, with_rice.id, meal_type="dinner", now=NOW).items if i.group_name == "김치찌개")
    assert card.companion_name == "쌀밥" and card.total_calories == 620
    assert "함께 드시던 쌀밥 300 포함" in card.reason  # 개인 동시기록 문구


def test_companion_needs_two_meals_before_trusting_personal(db, taxonomy):
    """한 번만 먹어 본 메뉴는 개인 동반 판단을 믿지 않고 군 기본값을 쓴다."""
    u = _user(db, "u1", email="u@gmail.com")
    _meal(db, u, "dinner", _days_ago(1), [("김치찌개", 320, 18, 22, 16)])  # 밥 없이 1회
    card = next(i for i in recommend(db, u.id, meal_type="dinner", now=NOW).items if i.group_name == "김치찌개")
    assert card.companion_name == "쌀밥"


def test_companion_total_must_stay_within_candidate_budget_limit(db, taxonomy):
    main = _group(db, "갈비찜", "구이·볶음·조림·찜·전류", "meal", 1000, 70, 45, 60, companion=taxonomy["rice"])
    u = _user(db, "light-budget", email="light@gmail.com")
    _meal(db, u, "dinner", _days_ago(1), [(main.name, 1000, 70, 45, 60)])
    result = recommend(db, u.id, meal_type="dinner", mood="light", now=NOW)
    assert 1000 <= result.budget.meal_budget * 2 < 1300
    assert main.id not in {c.group_id for c in result.candidates}
    assert all(c.total_calories <= result.budget.meal_budget * 2 for c in result.candidates)


# --- 계열 유사 -------------------------------------------------------------------


def test_similar_generator_uses_group_pool_and_same_family(db, taxonomy):
    u = _user(db, "u1", email="u@gmail.com")
    for d in range(1, 6):
        _meal(db, u, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    result = recommend(db, u.id, meal_type="dinner", now=NOW)
    assert result.groups_enabled and result.anchors == ["김치찌개"]
    similar = [c for c in result.candidates if c.source == "similar"]
    assert [c.group_name for c in similar] == ["된장찌개"]  # 같은 계열만 — 햄버거는 아님
    assert similar[0].family == STEW_FAMILY and similar[0].similar_to == "김치찌개"
    assert similar[0].group_id == taxonomy["soy"].id
    # 유사 후보도 동반이 붙는다 (된장찌개 → 쌀밥)
    soy = next(i for i in result.items if i.group_name == "된장찌개")
    assert soy.companion_name == "쌀밥" and soy.total_calories == 550


def test_similar_pool_skips_groups_without_macros(db, taxonomy):
    _group(db, "순두부찌개", STEW_FAMILY, "meal")  # 대표값 없음 → 풀 제외
    u = _user(db, "u1", email="u@gmail.com")
    for d in range(1, 6):
        _meal(db, u, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16)])
    similar = [c.group_name for c in recommend(db, u.id, meal_type="dinner", now=NOW).candidates if c.source == "similar"]
    assert "순두부찌개" not in similar and "된장찌개" in similar


@pytest.mark.parametrize("missing", ["calories", "carbs", "protein", "fat"])
def test_similar_pool_requires_all_four_macros(db, taxonomy, missing):
    from app.services.recommend.candidates import _similarity_pool

    macros = {"kcal": 320, "carbs": 18, "protein": 22, "fat": 16}
    macros["kcal" if missing == "calories" else missing] = None
    incomplete = _group(db, "순두부찌개", STEW_FAMILY, "meal", **macros)
    assert incomplete.id not in {p.group_id for p in _similarity_pool(db, load_group_index(db))}


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0])
def test_group_macro_validation_rejects_nonfinite_and_negative_values(invalid):
    group = GroupInfo(
        id=1, name="김치찌개", key="김치찌개", family=STEW_FAMILY, role="meal", companion_id=None,
        calories=320, carbs=18, protein=invalid, fat=16,
    )
    assert not group.has_macros


# --- 표시명 · 이름 통합 ------------------------------------------------------------


def test_personal_card_keeps_user_wording_popular_card_uses_group_name(db, taxonomy):
    fan = _user(db, "fan", email="f@gmail.com")
    for d in range(1, 6):
        _meal(db, fan, "dinner", _days_ago(d), [("빅소불고기버거", 520, 42, 27, 26)])
    mine = next(i for i in recommend(db, fan.id, meal_type="dinner", now=NOW).items if i.group_name == "햄버거")
    assert mine.source == "personal" and mine.name == "빅소불고기버거"
    assert mine.key == "햄버거" and mine.calories == 480  # 키·영양값은 군 대표값

    newbie = _user(db, "n")
    theirs = next(i for i in recommend(db, newbie.id, meal_type="dinner", now=NOW).items if i.group_name == "햄버거")
    assert theirs.source == "popular" and theirs.name == "햄버거"


def test_snapshot_group_id_merges_differently_named_records(db, taxonomy):
    """meal_items.food_group_id 가 있으면 이름이 제각각이어도 한 군으로 합산된다."""
    u = _user(db, "u1", email="u@gmail.com")
    gid = taxonomy["burger"].id
    _meal(db, u, "dinner", _days_ago(1), [("와퍼 세트(콜라X)", 700, 60, 30, 35, gid)])
    _meal(db, u, "dinner", _days_ago(2), [("맘스터치 싸이버거", 600, 50, 28, 30, gid)])
    _meal(db, u, "dinner", _days_ago(3), [("빅소불고기버거", 520, 42, 27, 26)])  # alias 로 합류
    stats = decayed_frequency(db, u.id, "dinner", now=NOW)
    assert [s.key for s in stats] == ["햄버거"] and stats[0].count == 3
    assert stats[0].name == "와퍼 세트(콜라X)"  # 표시명은 가장 최근·최다 원문 중 하나


def test_matched_item_group_used_when_name_unknown(db, taxonomy):
    """상품(nutrition_items.food_group_id) 으로 군을 알 수 있으면 이름이 낯설어도 묶인다."""
    item = NutritionItem(
        name="종가집 김치찌개 500g", normalized_name="종가집김치찌개500g", base_amount=500, base_unit="g",
        calories=330, carbs=20, protein=21, fat=17, source="mfds", is_representative=True,
        food_group_id=taxonomy["stew"].id,
    )
    db.add(item)
    db.flush()
    u = _user(db, "u1", email="u@gmail.com")
    record = _meal(db, u, "dinner", _days_ago(1), [("종가집 김치찌개 500g", 330, 20, 21, 17)])
    db.query(MealItem).filter(MealItem.meal_record_id == record.id).update({"nutrition_item_id": item.id})
    db.flush()
    stats = decayed_frequency(db, u.id, "dinner", now=NOW)
    assert stats[0].key == "김치찌개" and stats[0].group_id == taxonomy["stew"].id
    assert stats[0].calories == 320  # 군 대표값 우선


# --- 예산 상한 -----------------------------------------------------------------


def test_budget_ratio_is_capped_for_single_meal_loggers(db):
    """저녁만 기록하는 사람은 개인 비율이 1.0 이 되지만 MAX_RATIO 로 막는다."""
    u = _user(db, "u1")
    for d in range(1, 8):
        _meal(db, u, "dinner", _days_ago(d), [("저녁", 1350, 120, 60, 60)])
    b = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6)
    assert b.ratio_source == "personal"
    assert b.ratio == pytest.approx(MAX_RATIO_BY_MEAL["dinner"])
    assert b.ratio_budget == round(b.goal_calories * MAX_RATIO_BY_MEAL["dinner"])
