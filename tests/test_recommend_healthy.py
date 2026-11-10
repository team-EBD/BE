"""감량·건강 목표의 건강식 추천 — 필터, 생성기, 점수 바닥값, 한 장 보장, 목표 게이팅을 확인한다."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.models import FoodGroup, MealItem, MealRecord, NutritionItem, User, UserProfile
from app.services.recommend import engine as recommend_engine
from app.services.recommend.bandit import FEATURE_NAMES, RewardModel, select_cards
from app.services.recommend.candidates import Candidate, healthy_candidates, is_healthy
from app.services.recommend.groups import GroupIndex, GroupInfo
from app.services.recommend.ranking import (
    DIET_UNDER_BUDGET_PENALTY, HEALTHY_CONFIDENCE, POPULARITY_CONFIDENCE, UNDER_BUDGET_PENALTY,
    RankContext, fit_score, rank, score,
)

NOW = datetime(2026, 11, 10, 3, tzinfo=UTC)  # KST 12:00 — 점심
GRILL = "구이·볶음·조림·찜·전류"


def _cand(name, *, family=GRILL, role="meal", calories=300, protein=20, fat=8, sodium=400, sugar=None,
          companion_kcal=0.0, companion_protein=0.0, companion_sodium=0.0, **kw):
    return Candidate(key=name, name=name, calories=calories, carbs=30, protein=protein, fat=fat,
                     source="catalog", family=family, role=role, sodium=sodium, sugar=sugar,
                     companion_kcal=companion_kcal, companion_protein=companion_protein,
                     companion_sodium=companion_sodium, **kw)


def test_is_healthy_checks_sodium_protein_fat_and_skips_processed_families_and_names():
    fish = _cand("고등어구이", calories=278, protein=20, fat=10, sodium=362,
                 companion_kcal=300, companion_protein=5.5, companion_sodium=5)
    assert is_healthy(fish)
    assert not is_healthy(_cand("건새우볶음", sodium=1334))  # 나트륨 초과
    assert not is_healthy(_cand("삼겹살구이", calories=660, protein=34, fat=50, sodium=500))  # 이름 제외
    assert not is_healthy(_cand("라면", family="면류", sodium=1330))  # 인스턴트 계열 제외
    assert not is_healthy(_cand("버섯볶음", calories=142, protein=4, fat=5, sodium=300))  # 단백질 부족
    assert not is_healthy(_cand("닭구이", calories=250, protein=21, fat=12, sodium=300))  # 지방 열량 43%
    # 나트륨 대표값이 없으면 원재료 위주 계열만 허용한다
    assert is_healthy(_cand("닭가슴살", family="육류·수산물·달걀", calories=165, protein=35, fat=3, sodium=None))
    assert not is_healthy(_cand("소고기볶음", sodium=None))


def test_is_healthy_snack_limits_calories_sodium_sugar_and_skips_drinks():
    assert is_healthy(_cand("그릭요거트", family="유제품", role="snack", calories=120, protein=10, fat=3, sodium=50, sugar=8))
    assert is_healthy(_cand("달걀", family="육류·수산물·달걀", role="snack", calories=79, protein=7, fat=5, sodium=None))
    assert not is_healthy(_cand("요구르트(액상)", family="유제품", role="snack", calories=123, protein=3, fat=1, sodium=59, sugar=17))
    assert not is_healthy(_cand("레몬홍차", family="음료", role="snack", calories=88, protein=22, fat=0, sodium=4))
    assert not is_healthy(_cand("옥수수", family="샐러드·채소·나물", role="snack", calories=278, protein=10, fat=3, sodium=73))


def _info(gid, name, *, family=GRILL, role="meal", calories=300, protein=20, fat=8, sodium=400, sugar=None,
          companion_id=None):
    return GroupInfo(id=gid, name=name, key=name.replace(" ", ""), family=family, role=role,
                     companion_id=companion_id, calories=calories, carbs=30, protein=protein, fat=fat,
                     sodium=sodium, sugar=sugar)


def _index():
    rice = GroupInfo(id=1, name="쌀밥", key="쌀밥", family="밥류", role="companion", companion_id=None,
                     calories=300, carbs=66, protein=5.5, fat=0.5, sodium=5)
    return GroupIndex([
        rice,
        _info(2, "고등어구이", calories=278, sodium=362, companion_id=1),
        _info(3, "삼치구이", calories=250, protein=18, sodium=251, companion_id=1),
        _info(4, "닭가슴살 샐러드", family="샐러드·채소·나물", calories=320, protein=35, fat=9, sodium=33),
        _info(5, "된장국", family="국·탕·찌개류", calories=83, protein=6, fat=2, sodium=1105, companion_id=1),
        _info(6, "라면", family="면류", calories=420, protein=10, fat=15, sodium=1330),
        _info(7, "그릭요거트", family="유제품", role="snack", calories=120, protein=10, fat=3, sodium=50, sugar=8),
        _info(8, "김구이", calories=705, protein=31, fat=40, sodium=6017, companion_id=1),
    ], {})


def test_healthy_candidates_filters_the_group_pool_and_spreads_families():
    picked = healthy_candidates(None, 600, index=_index(), meal_type="lunch")
    names = [c.name for c in picked]
    assert set(names) == {"닭가슴살 샐러드", "고등어구이", "삼치구이"}
    assert names[0] == "닭가슴살 샐러드"  # 단백질 밀도가 가장 높다
    assert all(c.source == "healthy" and c.healthy == 1.0 for c in picked)
    fish = next(c for c in picked if c.name == "고등어구이")
    assert fish.companion_name == "쌀밥" and fish.total_sodium == pytest.approx(367)
    # 계열마다 하나씩 먼저 확보한다 — top=2 면 생선구이 둘이 아니라 샐러드 + 생선구이
    two = {c.family for c in healthy_candidates(None, 600, index=_index(), meal_type="lunch", top=2)}
    assert two == {"샐러드·채소·나물", GRILL}
    assert "닭가슴살샐러드" not in {c.key for c in healthy_candidates(
        None, 600, index=_index(), meal_type="lunch", exclude_keys=frozenset({"닭가슴살샐러드"}))}


def test_healthy_candidates_snack_path_uses_snack_roles_only():
    assert [c.name for c in healthy_candidates(None, 200, index=_index(), meal_type="snack")] == ["그릭요거트"]


def test_diet_under_budget_penalty_keeps_light_meals_competitive():
    assert fit_score(122, 600) == pytest.approx(1 - 0.5 * 478 / 600)
    assert fit_score(122, 600, DIET_UNDER_BUDGET_PENALTY) == pytest.approx(1 - 0.2 * 478 / 600)
    assert RankContext(budget=600, protein_gap=0).under_penalty == UNDER_BUDGET_PENALTY
    diet = RankContext(budget=600, protein_gap=0, under_penalty=DIET_UNDER_BUDGET_PENALTY)
    assert score(_cand("샐러드", calories=122), diet).parts["fit"] == pytest.approx(round(1 - 0.2 * 478 / 600, 3))


def test_healthy_evidence_is_a_floor_between_popularity_and_repeated_personal_history():
    ctx = RankContext(budget=500, protein_gap=0)
    healthy = _cand("고등어구이", calories=500, healthy=1.0)
    popular = _cand("짬뽕", calories=500, popularity=5)  # 5명 → 0.55 × 0.5
    personal = _cand("김치찌개", calories=500, freq=4)  # 반복 이력 → 4 / (4 + 2)
    assert score(healthy, ctx).parts["relevance"] == pytest.approx(HEALTHY_CONFIDENCE)
    assert score(popular, ctx).parts["relevance"] == pytest.approx(POPULARITY_CONFIDENCE * 0.5)
    assert score(popular, ctx).parts["healthy"] == 0.0
    assert [r.candidate.key for r in rank([popular, healthy, personal], ctx)] == ["김치찌개", "고등어구이", "짬뽕"]


def _select(cands, **kw):
    return select_cards(cands, RankContext(budget=500, protein_gap=0), RewardModel([0.0] * len(FEATURE_NAMES)),
                        meal_type="lunch", mood="any", rng=random.Random(1), **kw)


@pytest.mark.parametrize("surface", ["recommendation", "home"])
def test_require_healthy_guarantees_one_card_without_touching_the_explore_slot(surface):
    strong = [_cand(f"인기{i}", calories=500, popularity=50, family=f"계열{i}") for i in range(3)]
    weak = _cand("닭가슴살 샐러드", calories=320, healthy=1.0, family="샐러드·채소·나물")
    before, plain = _select(strong + [weak], surface=surface)
    assert not any(r.candidate.healthy for r in before) and plain["forced_key"] is None
    after, decision = _select(strong + [weak], surface=surface, require_healthy=True)
    keys = [r.candidate.key for r in after]
    assert "닭가슴살 샐러드" in keys and decision["forced_key"] == "닭가슴살 샐러드"
    explore = decision["bandit_rank"] - 1
    assert after[explore].candidate.key == before[explore].candidate.key  # 탐색 슬롯은 그대로
    assert decision["selected_keys"] == keys and set(decision["selected_features"]) == set(keys)
    assert decision["probabilities"]["닭가슴살 샐러드"] == 1.0
    # 이미 건강식이 들어 있으면 아무것도 바꾸지 않는다
    again, d2 = _select([weak] + strong[:1], surface=surface, require_healthy=True)
    assert d2["forced_key"] is None and any(r.candidate.healthy for r in again)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _seed(db, goal: str) -> int:
    user = User(social_provider="google", social_id=f"healthy-{goal}", nickname=f"healthy-{goal}")
    db.add(user)
    db.flush()
    db.add(UserProfile(user_id=user.id, goal_calories=1500, goal_carbs=190, goal_protein=94, goal_fat=42,
                       primary_goal=goal))
    rice = FoodGroup(name="쌀밥", family="밥류", role="companion", calories=300, carbs=66, protein=5.5, fat=0.5)
    db.add(rice)
    db.flush()
    fish = FoodGroup(name="고등어구이", family=GRILL, role="meal", calories=278, carbs=2, protein=20, fat=10,
                     companion_group_id=rice.id)
    noodle = FoodGroup(name="라면", family="면류", role="meal", calories=420, carbs=60, protein=10, fat=15)
    db.add_all([fish, noodle])
    db.flush()
    db.add(NutritionItem(name="고등어구이", normalized_name="고등어구이", base_amount=150, base_unit="g",
                         calories=278, carbs=2, protein=20, fat=10, sodium=362, source="seed",
                         is_representative=True, serving_basis="per_serving", food_group_id=fish.id))
    for d in (1, 2, 3):  # 라면을 사흘 먹은 이력 → 개인 후보
        record = MealRecord(user_id=user.id, meal_type="lunch", eaten_at=NOW - timedelta(days=d), is_skipped=False,
                            total_calories=420, total_carbs=60, total_protein=10, total_fat=15)
        db.add(record)
        db.flush()
        db.add(MealItem(meal_record_id=record.id, food_name="라면", serving_amount=1.0, calories=420, carbs=60,
                        protein=10, fat=15, food_group_id=noodle.id))
    db.commit()
    return user.id


@pytest.mark.parametrize("goal, expect_healthy", [("lose_weight", True), ("eat_healthy", True), ("maintain", False)])
def test_engine_adds_healthy_cards_only_for_diet_and_health_goals(db, goal, expect_healthy):
    uid = _seed(db, goal)
    result = recommend_engine.recommend(db, uid, meal_type="lunch", now=NOW, rng=random.Random(3))
    assert result.decision["goal"] == goal
    assert ("healthy" in [i.source for i in result.items]) is expect_healthy
    assert result.decision["generator_counts"]["healthy"] == (1 if expect_healthy else 0)
    if expect_healthy:
        card = next(i for i in result.items if i.source == "healthy")
        assert card.name == "고등어구이" and card.companion_name == "쌀밥"
        assert card.reason.startswith("점심에 가볍고 균형 잡힌 메뉴예요")


def test_healthy_mode_can_be_switched_off_by_setting(db, monkeypatch):
    uid = _seed(db, "lose_weight")
    monkeypatch.setattr(get_settings(), "recommend_healthy_enabled", False)
    result = recommend_engine.recommend(db, uid, meal_type="lunch", now=NOW, rng=random.Random(3))
    assert "healthy" not in [i.source for i in result.items]
    assert result.decision["generator_counts"]["healthy"] == 0
