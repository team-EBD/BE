"""추천 엔진 진입점 — 신호 → 후보 생성 → 랭킹 → 설명을 조립한다.

라우터·AI 서버와 무관한 순수 서비스. 같은 DB 상태·같은 now 면 같은 결과를 낸다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutil import now_utc, to_kst

from .candidates import (
    Candidate,
    meal_worthy,
    merge,
    nutrient_similar,
    personal_frequent,
    popular,
)
from .explain import reason
from .feedback import acceptance_rates
from .ranking import RankContext, Ranked, rank
from .signals import (
    Budget,
    budget_label,
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
    name: str
    calories: float
    protein: float
    source: str
    budget_label: str  # fit | light | heavy
    score: float
    parts: dict[str, float]
    reason: str


@dataclass
class RecommendationResult:
    meal_type: str
    mood: str
    budget: Budget
    items: list[RecommendedItem]
    candidates: list[Candidate]  # 랭킹 전 합집합 — 미리보기·출처 로그용
    anchors: list[str]


def recommend(
    db: Session,
    user_id: int,
    *,
    meal_type: str | None = None,
    mood: str = "any",
    now: datetime | None = None,
    k: int = 3,
) -> RecommendationResult:
    now = now or now_utc()
    settings = get_settings()
    meal_type = meal_type or meal_type_for_hour(to_kst(now).hour)

    budget = meal_budget(
        db, user_id, meal_type, now=now, day_start_hour=settings.day_start_hour, mood=mood
    )

    # 신호
    personal_stats = decayed_frequency(db, user_id, meal_type, now=now)
    popular_stats = global_popularity(db, meal_type, now=now)
    anchors = [
        s
        for s in personal_stats
        if s.count >= ANCHOR_MIN_COUNT and meal_worthy(s.calories, budget.meal_budget)
    ][:ANCHOR_TOP]

    # 후보 생성 — 개인 → 인기 → 유사 순으로 합치고 키 중복 제거 (반찬·소량은 생성기에서 컷)
    personal = personal_frequent(personal_stats, budget.meal_budget)
    pop = popular(popular_stats, budget.meal_budget)
    known = frozenset(c.key for c in personal) | frozenset(c.key for c in pop)
    similar = nutrient_similar(db, anchors, budget.meal_budget, exclude_keys=known)
    candidates = merge(personal, pop, similar)

    # 랭킹
    # 채택률: 그 사용자 이력이 우선, 노출이 부족한 키는 전체 사용자 값으로 보완한다
    # (둘 다 없으면 랭킹이 중립값 0.5 를 쓴다 — 로그가 없던 초기와 동일 동작)
    rates = {**acceptance_rates(db, now=now), **acceptance_rates(db, user_id, now=now)}
    ctx = RankContext(
        budget=budget.meal_budget,
        protein_gap=budget.protein_gap,
        recency=recency_penalties(db, user_id, now=now),
        acceptance=rates,
    )
    ranked: list[Ranked] = rank(candidates, ctx, k=k)

    items = [
        RecommendedItem(
            key=r.candidate.key,
            name=r.candidate.name,
            calories=round(r.candidate.calories),
            protein=round(r.candidate.protein, 1),
            source=r.candidate.source,
            budget_label=budget_label(r.candidate.calories, budget.meal_budget),
            score=r.score,
            parts=r.parts,
            reason=reason(r, budget),
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
    )
