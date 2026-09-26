"""조건부 선택 확률, 문맥 학습, 실제 노출·지연 보상의 계약 검증."""
from datetime import UTC, datetime, timedelta
from dataclasses import replace
import random

import pytest

from app.models import MealRecord, RecommendationItem, RecommendationLog, User
from app.services.recommend.bandit import (
    FEATURE_NAMES, FEATURE_VERSION, POLICY_VERSION, RewardModel, features, learn, select_cards,
)
from app.services.recommend.candidates import Candidate
from app.services.recommend.ranking import RankContext, rank, score

NOW = datetime(2026, 9, 18, 9, tzinfo=UTC)
CTX = RankContext(budget=500, protein_gap=20)


def candidate(key, **kwargs):
    values = dict(key=key, name=key, calories=500, carbs=60, protein=20, fat=10, source="catalog")
    return Candidate(**{**values, **kwargs})


def cold():
    return RewardModel([0.0] * len(FEATURE_NAMES))


def select(candidates, **kwargs):
    return select_cards(candidates, CTX, kwargs.pop("model", cold()), meal_type="dinner", mood="any", **kwargs)


def test_only_last_card_explores_with_exact_conditional_probabilities():
    pool = [candidate(key, freq=1) for key in "abcdef"]
    picked, decision = select(pool, rng=random.Random(2), epsilon=0.1)
    assert [r.candidate.key for r in picked[:2]] == [r.candidate.key for r in rank(pool, CTX, k=2)]
    probs = {key: decision["probabilities"][key] for key in decision["action_keys"]}
    assert sum(probs.values()) == pytest.approx(1)
    assert probs[decision["greedy_key"]] == pytest.approx(0.9 + 0.1 / len(probs))
    assert all(decision["probabilities"][key] == 1 for key in decision["fixed_keys"])
    assert len({r.candidate.key for r in picked}) == 3
    assert all(len(s["features"]) == len(FEATURE_NAMES) for s in decision["candidates"])


def test_exploration_can_choose_alternative_but_never_low_quality_candidate():
    class LastDraw:
        def random(self):
            return 0.999999
    pool = [candidate(key) for key in "abcde"] + [candidate("too-heavy", calories=950)]
    picked, decision = select(pool, rng=LastDraw())
    assert picked[-1].candidate.key != decision["greedy_key"]
    assert "too-heavy" not in decision["action_keys"]
    assert decision["probabilities"].get("too-heavy", 0) == 0


@pytest.mark.parametrize("count,k", [(0, 3), (1, 3), (2, 3), (5, 0), (5, -1)])
def test_sparse_empty_and_disabled_card_count(count, k):
    picked, decision = select([candidate(str(i)) for i in range(count)], k=k)
    assert len(picked) == min(count, max(0, k))
    if picked:
        assert decision["probabilities"][picked[-1].candidate.key] == 1


def test_zero_epsilon_preserves_baseline_before_feedback():
    pool = [candidate("a", freq=2), candidate("b", family="면류"), candidate("c"), candidate("d")]
    picked, decision = select(pool, epsilon=0)
    assert [r.candidate.key for r in picked] == [r.candidate.key for r in rank(pool, CTX)]
    assert decision["model"]["samples"] == 0
    assert decision["model"]["learning_weight"] == 0


def test_home_explores_visible_first_card_and_keeps_full_decision():
    class LastDraw:
        def random(self):
            return 0.999999
    pool = [candidate(key) for key in "abcde"]
    picked, decision = select(pool, surface="home", rng=LastDraw())
    assert len(picked) == 3
    assert decision["bandit_rank"] == 1
    assert picked[0].candidate.key != decision["greedy_key"]
    assert sum(decision["action_probabilities"].values()) == pytest.approx(1)
    first = picked[0].candidate.key
    assert decision["probabilities"][first] == decision["action_probabilities"][first]
    assert all(decision["probabilities"][r.candidate.key] == 1 for r in picked[1:])
    assert decision["fixed_prefix_keys"] == []
    assert len({r.candidate.key for r in picked}) == 3
    assert decision["selected_features"][first][FEATURE_NAMES.index("surface:home")] == 1
    assert decision["selected_features"][picked[1].candidate.key][FEATURE_NAMES.index("position")] == pytest.approx(2 / 3)
    # 후보 특성은 첫 슬롯 선택 당시 상태, 후속 카드 특성은 실제 표시 위치를 별도 보존한다.
    assert all(s["features"][FEATURE_NAMES.index("position")] == pytest.approx(1 / 3) for s in decision["candidates"])


def test_home_zero_exploration_returns_same_order_as_baseline():
    pool = [candidate("a", freq=4, family="면류"), candidate("b", family="면류"),
            candidate("c", family="밥류"), candidate("d", family="국·탕·찌개류")]
    picked, _ = select(pool, surface="home", epsilon=0)
    assert [r.candidate.key for r in picked] == [r.candidate.key for r in rank(pool, CTX)]


def test_features_include_current_context_and_independent_evidence():
    item = score(candidate("a", collaborative=0.6, similarity=0.8), CTX)
    dinner = features(item, "dinner", "any", 3)
    breakfast = features(item, "breakfast", "light", 3)
    assert dinner != breakfast
    assert dinner[FEATURE_NAMES.index("collaborative")] == 0.6
    assert dinner[FEATURE_NAMES.index("similarity")] == 0.8


@pytest.fixture()
def db(db_factory):
    with db_factory() as session:
        yield session


def user(db, name="bandit", email=None):
    row = User(social_provider="google", social_id=name, nickname=name, email=email)
    db.add(row)
    db.flush()
    return row


def observation(db, owner, cand, *, age=5, shown=True, accepted=False, eaten=False, rejected=False, policy=POLICY_VERSION, rank_=3):
    at = NOW - timedelta(hours=age)
    log = RecommendationLog(user_id=owner.id, created_at=at, decision={
        "feature_version": FEATURE_VERSION, "bandit_rank": 3,
    })
    db.add(log)
    db.flush()
    meal = None
    if eaten:
        meal = MealRecord(user_id=owner.id, meal_type="dinner", eaten_at=at + timedelta(minutes=30),
                          total_calories=500, total_carbs=60, total_protein=20, total_fat=10)
        db.add(meal)
        db.flush()
    row = RecommendationItem(
        log_id=log.id, name=cand.name, source=cand.source, rank=rank_,
        created_at=at, shown_at=at if shown else None,
        features=features(score(cand, CTX), "dinner", "any", rank_),
        selection_probability=0.1, policy_version=policy,
        accepted_at=at + timedelta(minutes=1) if accepted else None,
        rejected_at=at + timedelta(minutes=2) if rejected else None,
        eaten_at=meal.eaten_at if meal else None, eaten_meal_record_id=meal.id if meal else None,
    )
    db.add(row)
    db.flush()
    return row


def test_learning_excludes_unseen_pending_future_legacy_and_fixed_slots(db):
    owner = user(db)
    cand = candidate("menu")
    observation(db, owner, cand, shown=False)
    observation(db, owner, cand, age=1, accepted=True)
    observation(db, owner, cand, age=-1)
    observation(db, owner, cand, policy="legacy")
    observation(db, owner, cand, rank_=1)
    assert learn(db, now=NOW).samples == 0
    observation(db, owner, cand)
    observation(db, user(db, "test", "robot@test.com"), cand, eaten=True)
    assert learn(db, now=NOW).samples == 1


def test_real_feedback_changes_future_choices_and_reward_correction_retrains(db):
    owner = user(db)
    disliked = candidate("a", family="면류")
    liked = candidate("z", family="밥류")
    converted = []
    for _ in range(12):
        observation(db, owner, disliked, rejected=True)
        converted.append(observation(db, owner, liked, accepted=True, eaten=True))
    model = learn(db, now=NOW)
    assert model.samples == 20  # 한 사용자 반복은 제한한다
    assert model.users == 1
    picked, _ = select([disliked, liked], model=model, k=1, epsilon=0)
    assert picked[0].candidate.key == "z"
    old_prediction = model.predict(features(score(liked, CTX), "dinner", "any", 3))
    for row in converted:
        row.eaten_at = None  # 식사 수정/삭제로 보상 정정됨
    db.flush()
    corrected = learn(db, now=NOW)
    assert corrected.samples == 20  # 클릭+섭취를 두 관측으로 세지 않는다
    assert corrected.revision != model.revision
    assert corrected.predict(features(score(liked, CTX), "dinner", "any", 3)) < old_prediction
    assert learn(db, now=NOW).weights == corrected.weights


def test_invalid_feature_snapshot_is_not_learned(db):
    owner = user(db)
    row = observation(db, owner, candidate("a"))
    row.features = [1.0]
    db.flush()
    assert learn(db, now=NOW).samples == 0


def test_visible_home_first_card_can_train_without_other_cards_being_seen(db):
    owner = user(db)
    cand = candidate("home", family="밥류")
    row = observation(db, owner, cand, rank_=1, accepted=True)
    log = db.get(RecommendationLog, row.log_id)
    log.decision = {"feature_version": FEATURE_VERSION, "bandit_rank": 1, "surface": "home"}
    row.features = features(score(cand, CTX), "dinner", "any", 1, "home")
    observation(db, owner, candidate("not-seen"), shown=False)
    db.flush()
    model = learn(db, now=NOW)
    assert model.samples == 1 and model.users == 1


def test_collaborative_evidence_improves_rank_without_source_bonus():
    base = candidate("a")
    cf = replace(base, collaborative=0.7, collaborative_support=5)
    assert score(cf, CTX).score > score(base, CTX).score
    assert score(replace(cf, source="collaborative"), CTX).score == score(cf, CTX).score
