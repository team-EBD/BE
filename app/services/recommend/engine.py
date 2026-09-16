"""추천 엔진 진입점 — 신호 → 후보 생성 → 동반 → 랭킹 → 설명을 조립한다.

라우터·AI 서버와 무관한 순수 서비스. 같은 DB 상태·같은 now 면 같은 결과를 낸다.
군(food_groups)이 있으면 군 단위로 묶고 role·계열·동반을 쓰며, 없으면 이름 키로 폴백한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutil import now_utc, to_kst

from .candidates import (
    Candidate,
    attach_companion,
    meal_worthy,
    merge,
    nutrient_similar,
    personal_frequent,
    popular,
)
from .explain import reason
from .feedback import acceptance_rates
from .groups import ROLE_MEAL, GroupIndex, load_group_index
from .ranking import RankContext, Ranked, rank
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

# 영양 유사 생성기의 기준(anchor) — 90일 안에 2번 이상 먹은 음식 중 감쇠 점수 상위 5개.
# (점수 임계값 방식은 기록이 3주만 지나도 anchor 가 비어 유사 생성기가 꺼졌다 — 운영 미리보기)
ANCHOR_MIN_COUNT = 2
ANCHOR_TOP = 5


@dataclass
class RecommendedItem:
    key: str
    name: str  # 카드 표시명 — 사용자 이력의 상품명(personal) 또는 군명(popular·similar)
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
    companion_kcal: float = 0.0
    total_calories: float = 0.0  # 메인 + 동반


@dataclass
class RecommendationResult:
    meal_type: str
    mood: str
    budget: Budget
    items: list[RecommendedItem]
    candidates: list[Candidate]  # 랭킹 전 합집합 — 미리보기·출처 로그용
    anchors: list[str]
    groups_enabled: bool = False


def _display_name(cand: Candidate) -> str:
    """개인 이력에서 온 후보는 사용자가 적은 상품명, 그 외는 군명 (없으면 후보 이름)."""
    if cand.source == "personal":
        return cand.name
    return cand.group_name or cand.name


def _attach_companions(cands: list[Candidate], index: GroupIndex, personal: dict) -> None:
    """동반(밥) 규칙 — 개인 동시기록(50%↑)이 있으면 그것, 없으면 군의 기본 동반. 동반 없이 먹는 사람은 없음."""
    for c in cands:
        if c.role != ROLE_MEAL or c.group_id is None:
            attach_companion(c, None)
            continue
        stat = personal.get(c.key)
        if stat is not None and stat.meals >= 2:
            personal_companion = index.by_key.get(stat.companion_key) if stat.companion_key else None
            attach_companion(c, personal_companion, personal=True)
        else:
            attach_companion(c, index.companion_of(c.key))


def recommend(
    db: Session,
    user_id: int,
    *,
    meal_type: str | None = None,
    mood: str = "any",
    now: datetime | None = None,
    k: int = 3,
    index: GroupIndex | None = None,
) -> RecommendationResult:
    now = now or now_utc()
    settings = get_settings()
    meal_type = meal_type or meal_type_for_hour(to_kst(now).hour)
    index = index or load_group_index(db)

    budget = meal_budget(
        db, user_id, meal_type, now=now, day_start_hour=settings.day_start_hour, mood=mood
    )

    # 신호
    personal_stats = decayed_frequency(db, user_id, meal_type, now=now, index=index)
    popular_stats = global_popularity(db, meal_type, now=now, index=index)
    anchors = [
        s
        for s in personal_stats
        if s.count >= ANCHOR_MIN_COUNT and meal_worthy(s.calories, budget.meal_budget, s.role)
    ][:ANCHOR_TOP]

    # 후보 생성 — 개인 → 인기 → 유사 순으로 합치고 키 중복 제거 (반찬·주식·소량은 생성기에서 컷)
    personal = personal_frequent(personal_stats, budget.meal_budget)
    pop = popular(popular_stats, budget.meal_budget)
    known = frozenset(c.key for c in personal) | frozenset(c.key for c in pop)
    similar = nutrient_similar(db, anchors, budget.meal_budget, index=index, exclude_keys=known)
    candidates = merge(personal, pop, similar)

    # 동반(밥) — 예산 적합·라벨은 합산 기준
    if index.enabled:
        _attach_companions(candidates, index, companion_stats(db, user_id, now=now, index=index))

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
    ranked: list[Ranked] = rank(candidates, ctx, k=k)

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
            companion_kcal=round(r.candidate.companion_kcal),
            total_calories=round(r.candidate.total_calories),
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
    )
