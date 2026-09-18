"""후보 출처와 관계없이 근거·영양·다양성이 실제 추천 순위에 반영되는지 확인한다."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.recommend.candidates import Candidate
from app.services.recommend.ranking import RankContext, rank, score


def _candidate(key: str, **kwargs) -> Candidate:
    values = dict(
        key=key, name=key, calories=500.0, carbs=60.0, protein=20.0, fat=15.0,
        source="personal",
    )
    values.update(kwargs)
    return Candidate(**values)


CTX = RankContext(budget=500, protein_gap=0)


def test_stronger_similarity_improves_rank_for_otherwise_equal_candidates():
    weak = _candidate("a", source="similar", similarity=0.65)
    strong = _candidate("z", source="similar", similarity=0.95)
    assert [r.candidate.key for r in rank([weak, strong], CTX)] == ["z", "a"]
    assert score(strong, CTX).parts["similarity"] > score(weak, CTX).parts["similarity"]


def test_popularity_is_used_for_cold_start_and_remains_bounded():
    rare = _candidate("a", source="popular", popularity=1)
    common = _candidate("z", source="popular", popularity=30)
    assert rank([rare, common], CTX)[0].candidate is common
    assert score(replace(common, popularity=10**9), CTX).parts["popularity"] <= 1.0


def test_source_label_does_not_change_base_score_or_discard_other_evidence():
    mixed = _candidate("mixed", freq=0.5, similarity=0.9, popularity=12)
    assert score(mixed, CTX).score == score(replace(mixed, source="similar"), CTX).score
    assert score(mixed, CTX).score > score(replace(mixed, similarity=0.0), CTX).score
    assert score(mixed, CTX).parts["popularity"] > 0


def test_frequency_strength_is_stable_when_candidate_pool_changes():
    habitual = _candidate("habit", freq=5)
    rare = _candidate("rare", freq=0.3)
    outlier = _candidate("outlier", freq=1000, calories=1000)
    solo = score(habitual, CTX, max_freq=5)
    crowded = score(habitual, CTX, max_freq=1000)
    assert solo == crowded
    assert score(rare, CTX).parts["freq"] < solo.parts["freq"] < 1
    assert rank([habitual, rare, outlier], CTX, k=1)[0].score == solo.score


def test_similarity_can_beat_a_weak_habit_without_forced_source_replacement():
    weak_habit = _candidate("habit", freq=0.5)
    close_alternative = _candidate("similar", source="similar", similarity=0.95)
    assert rank([weak_habit, close_alternative], CTX, k=1)[0].candidate is close_alternative


def test_strong_habit_is_stronger_than_indirect_evidence_at_equal_fit():
    habitual = _candidate("habit", freq=20)
    similar = _candidate("similar", source="similar", similarity=1.0)
    popular = _candidate("popular", source="popular", popularity=1000)
    assert rank([popular, similar, habitual], CTX, k=1)[0].candidate is habitual


def test_recently_eaten_favorite_is_demoted():
    favorite = _candidate("favorite", freq=10)
    due = _candidate("due", freq=5)
    context = replace(CTX, recency={"favorite": 0.9})
    assert rank([favorite, due], context)[0].candidate is due
    assert score(favorite, CTX).score - score(favorite, context).score == pytest.approx(0.27)


def test_large_budget_excess_cannot_be_overridden_by_habit_or_protein():
    oversized = _candidate("oversized", freq=1000, calories=950, protein=70)
    suitable = _candidate("suitable", source="popular", popularity=1, protein=10)
    context = RankContext(budget=500, protein_gap=60, acceptance={"oversized": 1.0})
    assert rank([oversized, suitable], context, k=1)[0].candidate is suitable


def test_reasonably_light_meal_retains_preference_value():
    light_habit = _candidate("light", freq=10, calories=400)
    exact_without_evidence = _candidate("exact", source="popular", popularity=0)
    assert rank([light_habit, exact_without_evidence], CTX, k=1)[0].candidate is light_habit


def test_companion_protein_is_included_consistently_with_total_calories():
    with_rice = _candidate(
        "with_rice", calories=200, protein=15, companion_kcal=300, companion_protein=5,
    )
    standalone = _candidate("standalone", calories=500, protein=20)
    context = RankContext(budget=500, protein_gap=20)
    rice_score = score(with_rice, context)
    standalone_score = score(standalone, context)
    assert rice_score.parts["protein"] == standalone_score.parts["protein"] == 1.0
    assert rice_score.score == standalone_score.score


def test_source_diversity_does_not_force_a_poor_novel_candidate():
    habits = [_candidate(key, freq=freq) for key, freq in (("a", 12), ("b", 10), ("c", 8))]
    poor_novel = _candidate("novel", source="similar", similarity=0.9, calories=950)
    assert {r.candidate.key for r in rank([*habits, poor_novel], CTX)} == {"a", "b", "c"}


def test_close_quality_alternative_can_reduce_personal_source_repetition():
    habits = [_candidate(key, freq=8) for key in ("a", "b", "c")]
    novel = _candidate("novel", source="similar", similarity=1.0)
    ranked = rank([*habits, novel], CTX)
    assert [r.candidate.key for r in ranked] == ["a", "b", "novel"]


def test_near_equal_candidates_get_food_family_diversity():
    noodles = [_candidate(key, source="popular", popularity=10, family="면류") for key in ("a", "b", "c")]
    rice = _candidate("z", source="popular", popularity=8, family="밥류")
    ranked = rank([*noodles, rice], CTX, k=2)
    assert [r.candidate.family for r in ranked] == ["면류", "밥류"]


def test_diversity_does_not_promote_poor_food_only_to_change_family():
    noodles = [_candidate(key, freq=15, family="면류") for key in ("a", "b", "c")]
    poor_rice = _candidate("z", source="popular", popularity=1, family="밥류", calories=900)
    assert all(r.candidate.family == "면류" for r in rank([*noodles, poor_rice], CTX))


def test_unknown_food_categories_are_not_treated_as_one_repeated_category():
    candidates = [_candidate(key, source="popular", popularity=10) for key in ("a", "b", "c")]
    assert all(r.parts["diversity"] == 0 for r in rank(candidates, CTX))


def test_ranking_is_deterministic_has_consistent_scores_and_handles_k_edges():
    candidates = [
        _candidate("b", source="popular", popularity=8, family="면류"),
        _candidate("a", source="popular", popularity=8, family="면류"),
        _candidate("c", source="popular", popularity=7, family="밥류"),
    ]
    ranked = rank(candidates, CTX, k=20)
    assert rank(list(reversed(candidates)), CTX, k=20) == ranked
    assert len(ranked) == 3
    assert [r.score for r in ranked] == sorted([r.score for r in ranked], reverse=True)
    assert rank(candidates, CTX, k=0) == []
    assert rank(candidates, CTX, k=-1) == []
    assert rank([], CTX) == []


def test_repeated_keys_do_not_produce_duplicate_cards_or_crash_large_k():
    candidate = _candidate("a", freq=4)
    assert len(rank([candidate, candidate], CTX, k=3)) == 1


def test_external_normalized_signals_are_clamped():
    candidate = _candidate("a", similarity=1.5, freq=-2, popularity=-1)
    parts = score(candidate, replace(CTX, recency={"a": -1}, acceptance={"a": 2})).parts
    assert all(0 <= value <= 1 for value in parts.values())
