"""추천 엔진 진입점 — 신호 → 동반을 반영한 후보 생성 → 랭킹 → 설명을 조립한다.

라우터·AI 서버와 무관한 서비스. 마지막 카드에는 제한된 확률 탐색을 적용한다.
군(food_groups)이 있으면 군 단위로 묶고 role·계열·동반을 쓰며, 없으면 이름 키로 폴백한다.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutil import now_utc, to_kst

from .candidates import (
    Candidate,
    catalog_fallback,
    enrich_similarity,
    merge,
    nutrient_similar,
    personal_frequent,
    popular,
)
from .explain import reason
from .feedback import acceptance_rates, excluded_keys
from .bandit import learn, select_cards
from .collaborative import collaborative_candidates
from .groups import ROLE_MEAL, GroupIndex, GroupInfo, load_group_index
from .ranking import RankContext
from .signals import (
    Budget,
    budget_label,
    companion_stats,
    decayed_frequency,
    global_popularity,
    meal_budget,
    meal_type_for_hour,
    recency_penalties,
)

# 음식 유사 생성기의 기준(anchor) — 90일 안에 2번 이상 먹은 음식 중 감쇠 점수 상위 5개.
# (점수 임계값 방식은 기록이 3주만 지나도 anchor 가 비어 유사 생성기가 꺼졌다 — 운영 미리보기)
ANCHOR_MIN_COUNT = 2
ANCHOR_TOP = 5


@dataclass
class RecommendedItem:
    key: str
    name: str  # 카드 표시명 — 사용자 이력의 상품명(personal) 또는 군명
    calories: float  # 메인 1인분
    protein: float
    source: str
    budget_label: str  # fit | light | heavy — 메인 + 동반 합산 기준
    score: float
    parts: dict[str, float]
    reason: str
    group_id: int | None = None
    group_name: str | None = None
    family: str | None = None
    companion_name: str | None = None  # "함께 드시던 쌀밥"
    companion_key: str | None = None  # 동반 군 키 — 라우터가 프리필용 상품을 찾을 때 쓴다
    companion_kcal: float = 0.0
    total_calories: float = 0.0  # 메인 + 동반
    selection_probability: float = 1.0
    features: list[float] = field(default_factory=list)


@dataclass
class RecommendationResult:
    meal_type: str
    mood: str
    budget: Budget
    items: list[RecommendedItem]
    candidates: list[Candidate]  # 랭킹 전 합집합 — 미리보기·출처 로그용
    anchors: list[str]
    groups_enabled: bool = False
    decision: dict = field(default_factory=dict)


def _display_name(cand: Candidate) -> str:
    """개인 이력에서 온 후보는 사용자가 적은 상품명, 그 외는 군명 (없으면 후보 이름)."""
    if cand.source == "personal":
        return cand.name
    return cand.group_name or cand.name


def _companion_choices(index: GroupIndex, personal: dict) -> dict[str, GroupInfo | None]:
    """후보를 자르기 전에 개인 동시기록 또는 군 기본 동반을 결정한다."""
    choices = {}
    for group in index.by_id.values():
        if group.role != ROLE_MEAL:
            continue
        stat = personal.get(group.key)
        if stat is not None and stat.meals >= 2:
            choices[group.key] = index.by_key.get(stat.companion_key) if stat.companion_key else None
        else:
            choices[group.key] = index.companion_of(group.key)
    return choices


def recommend(
    db: Session,
    user_id: int,
    *,
    meal_type: str | None = None,
    mood: str = "any",
    now: datetime | None = None,
    k: int = 3,
    index: GroupIndex | None = None,
    rng: random.Random | None = None,
    surface: str = "recommendation",
) -> RecommendationResult:
    now = now or now_utc()
    settings = get_settings()
    meal_type = meal_type or meal_type_for_hour(to_kst(now).hour)
    index = index or load_group_index(db)
    blocked = excluded_keys(db, user_id, now=now, index=index)

    budget = meal_budget(
        db, user_id, meal_type, now=now, day_start_hour=settings.day_start_hour, mood=mood
    )

    # 신호
    personal_stats = decayed_frequency(db, user_id, meal_type, now=now, index=index)
    popular_stats = global_popularity(db, meal_type, now=now, index=index, exclude_user_id=user_id)
    personal_companions = companion_stats(db, user_id, now=now, index=index) if index.enabled else {}
    companions = _companion_choices(index, personal_companions)
    options = {"meal_type": meal_type, "companions": companions}
    # 표시 수보다 넓게 모아 랭킹이 예산·최근 섭취·채택률을 비교할 기회를 남긴다.
    personal = personal_frequent(personal_stats, budget.meal_budget, top=max(24, k) + len(blocked), **options)
    # 즐겨 먹던 큰 메뉴는 현재 예산에 넘쳐도 더 가벼운 유사 메뉴의 기준이 될 수 있다.
    # anchor에는 역할·값 검증만 적용하고 열량 상한은 실제 후보에 적용한다.
    eligible_anchors = {
        c.key for c in personal_frequent(personal_stats, 0, top=len(personal_stats), **options)
    }
    anchors = [
        s
        for s in personal_stats
        if s.count >= ANCHOR_MIN_COUNT and s.key in eligible_anchors and s.key not in blocked
    ][:ANCHOR_TOP]

    # 독립 생성 → 중복 제거·근거 보존. 다른 생성기가 찾은 음식도 협업/유사 근거를 가질 수 있다.
    pop = popular(popular_stats, budget.meal_budget, top=max(15, k) + len(blocked), **options)
    similar = nutrient_similar(db, anchors, budget.meal_budget, index=index, exclude_keys=frozenset(blocked),
                               top=max(15, k), **options)
    collaborative = collaborative_candidates(
        db, user_id, budget.meal_budget, now=now, index=index,
        exclude_keys=frozenset(blocked), top=max(15, k), **options,
    )
    candidates = [c for c in merge(personal, pop, similar, collaborative) if c.key not in blocked]
    enrich_similarity(candidates, anchors)
    if len(candidates) < k or (not anchors and not pop):
        candidates = merge(candidates, catalog_fallback(
            db, budget.meal_budget, index=index, exclude_keys=frozenset(c.key for c in candidates) | frozenset(blocked),
            top=max(15, k), **options,
        ))
    for c in candidates:
        stat = personal_companions.get(c.key)
        if c.companion_key and stat is not None and stat.meals >= 2:
            c.companion_personal = True

    # 랭킹
    # 채택률: 그 사용자 이력이 우선, 노출이 부족한 키는 전체 사용자 값으로 보완한다
    # (둘 다 없으면 랭킹이 중립값 0.5 를 쓴다 — 로그가 없던 초기와 동일 동작)
    rates = {**acceptance_rates(db, now=now), **acceptance_rates(db, user_id, now=now)}
    ctx = RankContext(
        budget=budget.meal_budget,
        protein_gap=budget.protein_gap,
        recency=recency_penalties(db, user_id, now=now, index=index),
        acceptance=rates,
    )
    ranked, decision = select_cards(
        candidates, ctx, learn(db, now=now), meal_type=meal_type, mood=mood,
        k=k, epsilon=settings.recommend_bandit_epsilon, rng=rng,
        surface=surface,
    )
    decision["generator_counts"] = {
        "personal": len(personal), "popular": len(pop), "similar": len(similar),
        "collaborative": len(collaborative),
        "catalog": sum(c.source == "catalog" for c in candidates),
    }
    decision["excluded_count"] = len(blocked)

    items = [
        RecommendedItem(
            key=r.candidate.key,
            name=_display_name(r.candidate),
            calories=round(r.candidate.calories),
            protein=round(r.candidate.protein, 1),
            source=r.candidate.source,
            budget_label=budget_label(r.candidate.total_calories, budget.meal_budget),
            score=r.score,
            parts=r.parts,
            reason=reason(r, budget),
            group_id=r.candidate.group_id,
            group_name=r.candidate.group_name,
            family=r.candidate.family,
            companion_name=r.candidate.companion_name,
            companion_key=r.candidate.companion_key,
            companion_kcal=round(r.candidate.companion_kcal),
            total_calories=round(r.candidate.total_calories),
            selection_probability=decision["probabilities"][r.candidate.key],
            features=decision["selected_features"][r.candidate.key],
        )
        for r in ranked
    ]
    return RecommendationResult(
        meal_type=meal_type,
        mood=mood,
        budget=budget,
        items=items,
        candidates=candidates,
        anchors=[a.key for a in anchors],
        groups_enabled=index.enabled,
        decision=decision,
    )
