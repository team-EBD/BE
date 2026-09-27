"""동반·끼니 역할·음식 형태를 적용한 뒤 후보 수를 제한하는지 검증한다."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import FoodGroup, MealItem, MealRecord, User
from app.services.recommend import candidates as generators
from app.services.recommend import engine as recommend_engine
from app.services.recommend import recommend
from app.services.recommend.candidates import Candidate, meal_worthy, merge
from app.services.recommend.groups import GroupIndex, GroupInfo, load_group_index
from app.services.recommend.signals import Budget, FoodStat

NOW = datetime(2026, 9, 16, 9, tzinfo=UTC)
STEW_FAMILY = "국·탕·찌개류"
BURGER_FAMILY = "버거·피자·샌드위치"


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _info(gid, name, *, family=STEW_FAMILY, role="meal", calories=320):
    return GroupInfo(
        id=gid, name=name, key=name.replace(" ", ""), family=family, role=role,
        calories=calories, carbs=30, protein=20, fat=10, companion_id=None,
    )


def _stat(group, *, name=None, score=3):
    return FoodStat(
        key=group.key, name=name or group.name, score=score, count=3,
        last_eaten=NOW - timedelta(days=1), calories=group.calories,
        carbs=group.carbs, protein=group.protein, fat=group.fat,
        group_id=group.id, group_name=group.name, family=group.family, role=group.role,
    )


def _candidate(source, **kwargs):
    return Candidate(
        key="김치찌개", name="우리집 김치찌개", calories=320, carbs=30, protein=20,
        fat=10, source=source, **kwargs,
    )


def _group(db, name, family=STEW_FAMILY, role="meal", calories=320, companion=None):
    group = FoodGroup(
        name=name, family=family, role=role, calories=calories, carbs=30, protein=20,
        fat=10, companion_group_id=companion.id if companion else None,
    )
    db.add(group)
    db.flush()
    return group


def test_merge_keeps_first_source_but_preserves_other_generators_evidence():
    merged = merge(
        [_candidate("personal", freq=3)],
        [_candidate("popular", popularity=7)],
        [_candidate("similar", similarity=0.91, similar_to="된장찌개", similar_type="찌개")],
    )
    assert len(merged) == 1
    assert merged[0].source == "personal" and merged[0].name == "우리집 김치찌개"
    assert merged[0].freq == 3 and merged[0].popularity == 7
    assert merged[0].similarity == 0.91 and merged[0].similar_to == "된장찌개"


@pytest.mark.parametrize("generator", [generators.personal_frequent, generators.popular])
def test_companion_is_counted_before_lower_upper_cuts_and_top_limit(generator):
    rice = _info(1, "쌀밥", family="밥류", role="companion", calories=300)
    oversized = _info(2, "갈비탕", calories=800)  # 밥과 합치면 예산 2배를 초과
    light = _info(3, "맑은국", calories=60)  # 단독 하한 미달이지만 밥과 먹으면 360kcal
    ordinary = _info(4, "김치찌개", calories=320)
    companions = {group.key: rice for group in (oversized, light, ordinary)}
    got = generator(
        [_stat(group) for group in (oversized, light, ordinary)], 500, top=1,
        meal_type="dinner", companions=companions,
    )
    assert [candidate.key for candidate in got] == [light.key]
    assert got[0].total_calories == 360


def test_known_meal_and_snack_roles_do_not_cross_meal_context():
    assert meal_worthy(150, 300, "meal", meal_type="dinner")
    assert not meal_worthy(150, 300, "snack", meal_type="dinner")
    assert meal_worthy(150, 150, "snack", meal_type="snack")
    assert not meal_worthy(150, 150, "meal", meal_type="snack")
    assert not meal_worthy(150, 300, "companion", meal_type="dinner")
    assert not meal_worthy(150, 300, "exclude", meal_type="dinner")


@pytest.mark.parametrize("generator", [generators.personal_frequent, generators.popular])
def test_generators_filter_meal_role_before_top_limit(generator):
    snack = _info(1, "초코쿠키", family="빵·과자·디저트", role="snack", calories=150)
    meal = _info(2, "김치찌개", calories=150)
    stats = [_stat(snack), _stat(meal)]
    dinner = generator(stats, 500, top=1, meal_type="dinner")
    assert [candidate.key for candidate in dinner] == [meal.key]
    snack_time = generator(list(reversed(stats)), 150, top=1, meal_type="snack")
    assert [candidate.key for candidate in snack_time] == [snack.key]


def test_empty_history_returns_catalog_with_honest_reason_and_correct_roles(db):
    user = User(social_provider="google", social_id="new-catalog-user", nickname="새 사용자")
    db.add(user)
    db.flush()
    rice = _group(db, "쌀밥", "밥류", "companion", 300)
    _group(db, "김치찌개", companion=rice)
    _group(db, "치즈버거", BURGER_FAMILY, calories=620)
    _group(db, "새우볶음밥", "밥류", calories=620)
    _group(db, "초코쿠키", "빵·과자·디저트", "snack", 150)
    _group(db, "김치", "김치·절임", "exclude", 30)

    result = recommend(db, user.id, meal_type="dinner", now=NOW)

    assert len(result.items) == 3
    assert {item.source for item in result.items} == {"catalog"}
    assert {item.name for item in result.items} == {"김치찌개", "치즈버거", "새우볶음밥"}
    assert all("자주" not in item.reason and "많이 기록" not in item.reason for item in result.items)
    assert all(item.reason for item in result.items)
    assert all(candidate.role == "meal" for candidate in result.candidates)
    stew = next(item for item in result.items if item.name == "김치찌개")
    assert stew.total_calories == 620 and stew.companion_name == "쌀밥"


def test_catalog_respects_exclusions_missing_macros_and_snack_context(db):
    wanted = _group(db, "초코쿠키", "빵·과자·디저트", "snack", 150)
    excluded = _group(db, "버터쿠키", "빵·과자·디저트", "snack", 150)
    incomplete = _group(db, "견과쿠키", "빵·과자·디저트", "snack", 150)
    incomplete.protein = None
    _group(db, "김치찌개", calories=150)
    _group(db, "단무지", "김치·절임", "exclude", 100)
    db.flush()

    got = generators.catalog_fallback(
        db, 150, index=load_group_index(db), meal_type="snack",
        exclude_keys=frozenset({excluded.name}), top=5,
    )

    assert [candidate.group_id for candidate in got] == [wanted.id]
    assert got[0].source == "catalog"
    assert got[0].freq == got[0].popularity == got[0].similarity == 0


def test_multiple_anchors_contribute_similar_candidates_before_one_form_fills_top(db):
    stew = _info(1, "김치찌개")
    burger = _info(2, "불고기버거", family=BURGER_FAMILY)
    alternatives = [
        _info(3, "가나다찌개"), _info(4, "가라다찌개"), _info(5, "가마바찌개"),
        _info(6, "치즈버거", family=BURGER_FAMILY),
    ]
    index = GroupIndex([stew, burger, *alternatives], {})
    got = generators.nutrient_similar(
        db, [_stat(stew), _stat(burger)], 500, index=index, top=2, meal_type="dinner",
    )
    assert len(got) == 2
    assert {candidate.similar_to for candidate in got} == {stew.name, burger.name}
    assert "치즈버거" in {candidate.name for candidate in got}


def test_similar_generator_uses_canonical_form_not_composite_family_or_product_name(db):
    anchor = _info(1, "불고기버거", family=BURGER_FAMILY)
    cheese = _info(2, "치즈버거", family=BURGER_FAMILY)
    pizza = _info(3, "불고기피자", family=BURGER_FAMILY)
    sandwich = _info(4, "불고기샌드위치", family=BURGER_FAMILY)
    index = GroupIndex([anchor, cheese, pizza, sandwich], {})

    got = generators.nutrient_similar(
        db, [_stat(anchor, name="와퍼 세트")], 500, index=index, meal_type="dinner",
    )

    assert [candidate.name for candidate in got] == [cheese.name]
    assert got[0].similar_to == anchor.name and got[0].similar_type == "버거"


def test_similar_generator_applies_companion_limit_before_top_selection(db):
    anchor = _info(1, "김치찌개")
    excessive = _info(2, "가나다찌개", calories=800)
    acceptable = _info(3, "된장찌개", calories=250)
    rice = _info(4, "쌀밥", family="밥류", role="companion", calories=300)
    got = generators.nutrient_similar(
        db, [_stat(anchor)], 500, index=GroupIndex([anchor, excessive, acceptable, rice], {}),
        top=1, meal_type="dinner", companions={excessive.key: rice, acceptable.key: rice},
    )
    assert [candidate.name for candidate in got] == [acceptable.name]
    assert got[0].total_calories == 550


def test_explicit_no_companion_preference_preserves_standalone_portion():
    light = _info(1, "맑은국", calories=60)
    ordinary = _info(2, "김치찌개", calories=320)
    got = generators.personal_frequent(
        [_stat(light), _stat(ordinary)], 500, top=1, meal_type="dinner",
        companions={light.key: None, ordinary.key: None},
    )
    assert [candidate.key for candidate in got] == [ordinary.key]
    assert got[0].companion_name is None and got[0].total_calories == 320


def test_over_budget_favorite_remains_anchor_for_a_lighter_similar_menu(db, monkeypatch):
    user = User(social_provider="google", social_id="burger-fan", nickname="버거 선호")
    db.add(user)
    db.flush()
    favorite = _group(db, "더블치즈버거", BURGER_FAMILY, calories=1000)
    lighter = _group(db, "치즈버거", BURGER_FAMILY, calories=450)
    for days_ago in (1, 3):
        record = MealRecord(
            user_id=user.id, meal_type="dinner", eaten_at=NOW - timedelta(days=days_ago),
            is_skipped=False, total_calories=1000, total_carbs=30, total_protein=20, total_fat=10,
        )
        db.add(record)
        db.flush()
        db.add(MealItem(
            meal_record_id=record.id, food_name=favorite.name, serving_amount=1,
            calories=1000, carbs=30, protein=20, fat=10, food_group_id=favorite.id,
        ))
    db.flush()
    budget = Budget(
        meal_type="dinner", goal_calories=2000, ratio=0.35, ratio_source="default",
        ratio_budget=700, remaining_today=400, meal_budget=400, protein_gap=40, mood="any",
    )
    monkeypatch.setattr(recommend_engine, "meal_budget", lambda *args, **kwargs: budget)

    result = recommend(db, user.id, meal_type="dinner", now=NOW, k=1)

    assert result.anchors == [favorite.name]
    assert favorite.id not in {candidate.group_id for candidate in result.candidates}
    assert [candidate.group_id for candidate in result.candidates] == [lighter.id]
    assert result.items[0].source == "similar"
    assert "더블치즈버거" in result.items[0].reason


@pytest.mark.parametrize("name", ["보리밥", "검정콩밥", "멸치볶음", "연근조림", "애호박볶음"])
def test_unclassified_plain_rice_and_side_dishes_are_not_main_candidates(name):
    """음식군이 없는 기존 기록에서도 열량만으로 밥·반찬을 단독 메뉴로 올리지 않는다."""
    stat = FoodStat(name, name, 3, 3, NOW, 200, 30, 10, 5)
    assert generators.personal_frequent([stat], 700, meal_type="dinner") == []


def test_catalog_keeps_another_family_when_many_near_identical_menus_fill_pool(db):
    for number in range(18):
        _group(db, f"찌개{number}찌개", calories=500)
    _group(db, "치즈버거", BURGER_FAMILY, calories=499)
    got = generators.catalog_fallback(db, 500, meal_type="dinner", top=3)
    assert len(got) == 3
    assert "치즈버거" in {candidate.name for candidate in got}


def test_sparse_one_time_history_can_discover_catalog_without_frequent_anchor(db):
    user = User(social_provider="google", social_id="sparse-user", nickname="초기 사용자")
    db.add(user)
    db.flush()
    for name in ("치즈버거", "새우버거", "불고기버거"):
        group = _group(db, name, BURGER_FAMILY, calories=600)
        record = MealRecord(
            user_id=user.id, meal_type="dinner", eaten_at=NOW - timedelta(days=1),
            is_skipped=False, total_calories=600, total_carbs=30, total_protein=20, total_fat=10,
        )
        db.add(record)
        db.flush()
        db.add(MealItem(
            meal_record_id=record.id, food_name=name, food_group_id=group.id,
            serving_amount=1, calories=600, carbs=30, protein=20, fat=10,
        ))
    _group(db, "새우볶음밥", "밥류", calories=600)
    db.flush()
    result = recommend(db, user.id, meal_type="dinner", now=NOW)
    assert result.anchors == []
    assert any(candidate.source == "catalog" for candidate in result.candidates)
    assert any(item.name == "새우볶음밥" for item in result.items)
