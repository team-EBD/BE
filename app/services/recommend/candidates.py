"""개인·인기·음식 유사 후보를 넓게 모으고 같은 음식군의 근거를 합친다.

역할 및 밥 포함 영양을 먼저 확인한 뒤 후보 수를 제한한다. 실제 순위는 ranking이 정한다.
군이 없는 기존 DB는 알려진 음식 형태에 한해 역할을 보완한다.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import NutritionItem
from app.food_taxonomy import COMPANION_GROUPS, SIDE_DISH_GROUPS
from app.services.matching import normalize_name, per_serving_representatives

from .groups import (
    RECOMMENDABLE_ROLES, ROLE_COMPANION, ROLE_EXCLUDE, ROLE_MEAL, ROLE_SNACK,
    GroupIndex, GroupInfo, load_group_index,
)
from .signals import FoodStat
from .similarity import FoodProfile, compare_foods, food_profile

PERSONAL_TOP = 8
POPULAR_TOP = 5
SIMILAR_TOP = 5
MIN_MEAL_KCAL = 80
MIN_MEAL_BUDGET_SHARE = 0.15
MAX_MEAL_BUDGET_MULT = 2.0
_PLAIN_RICE = {normalize_name(name) for name in COMPANION_GROUPS} | {"공기밥", "흰쌀밥", "흰밥"}
_SIDE_DISHES = {normalize_name(name) for name in SIDE_DISH_GROUPS}


def meal_worthy(
    calories: float, budget_kcal: int, role: str | None = None, *, meal_type: str | None = None,
) -> bool:
    """역할과 끼니가 맞고, 동반을 포함한 1인분 열량이 허용 범위인지 확인한다."""
    if not math.isfinite(calories) or role in (ROLE_EXCLUDE, ROLE_COMPANION):
        return False
    if meal_type and role in RECOMMENDABLE_ROLES:
        if (meal_type == "snack") != (role == ROLE_SNACK):
            return False
    low = max(MIN_MEAL_KCAL, budget_kcal * MIN_MEAL_BUDGET_SHARE)
    high = budget_kcal * MAX_MEAL_BUDGET_MULT if budget_kcal > 0 else float("inf")
    return low <= calories <= high


@dataclass
class Candidate:
    key: str
    name: str
    calories: float  # 메인 1인분
    carbs: float
    protein: float
    fat: float
    source: str  # personal | popular | similar | collaborative | catalog (주된 생성 경로)
    freq: float = 0.0
    popularity: float = 0.0  # 서로 다른 사용자의 시간 감쇠 지지 수
    similarity: float = 0.0
    similar_to: str | None = None
    similar_type: str | None = None
    last_eaten: datetime | None = None
    group_id: int | None = None
    group_name: str | None = None
    family: str | None = None
    role: str | None = None
    companion_key: str | None = None
    companion_name: str | None = None
    companion_kcal: float = 0.0
    companion_protein: float = 0.0
    companion_personal: bool = False
    collaborative: float = 0.0  # 음식군 공동 섭취에 기반한 협업 점수 0~1
    collaborative_support: int = 0  # 해당 협업 관계를 뒷받침한 다른 사용자 수

    @property
    def total_calories(self) -> float:
        return self.calories + self.companion_kcal

    @property
    def total_protein(self) -> float:
        return self.protein + self.companion_protein


Companions = Mapping[str, GroupInfo | None]


def _profile(item: Candidate | FoodStat) -> FoodProfile:
    return food_profile(
        item.name, carbs=item.carbs, protein=item.protein, fat=item.fat,
        group_name=item.group_name, family=item.family, role=item.role,
    )


def _legacy_role(name: str) -> str | None:
    """군 미구축 경로의 보수적 보완. 임의 상품명의 역할은 추측하지 않는다."""
    key = normalize_name(name)
    if key in _PLAIN_RICE:
        return ROLE_COMPANION
    if key in _SIDE_DISHES:
        return ROLE_EXCLUDE
    if key in {"김치", "배추김치", "깍두기", "단무지", "쌈무", "콜라", "아메리카노"}:
        return ROLE_EXCLUDE
    if key in {"바나나", "사과", "삶은계란", "삶은달걀", "시리얼"}:
        return ROLE_SNACK
    kind = food_profile(name, carbs=0, protein=0, fat=0).kind
    if kind in {"김치", "나물", "차", "커피", "에이드", "주스"}:
        return ROLE_EXCLUDE
    if kind in {"빵", "케이크", "도넛", "쿠키", "과자", "초콜릿", "아이스크림", "요거트",
                "떡", "라떼", "우유", "두유", "스무디"}:
        return ROLE_SNACK
    return ROLE_MEAL if kind and kind not in {"밥", "두부"} else None


def attach_companion(
    cand: Candidate, companion: GroupInfo | None, *, personal: bool = False,
) -> Candidate:
    """대표값이 완비된 동반 역할만 붙인다. 합산 열량과 단백질은 랭킹에도 사용한다."""
    if companion is None or companion.role != ROLE_COMPANION or not companion.has_macros:
        cand.companion_key = cand.companion_name = None
        cand.companion_kcal = cand.companion_protein = 0.0
        cand.companion_personal = False
        return cand
    cand.companion_key = companion.key
    cand.companion_name = companion.name
    cand.companion_kcal = float(companion.calories or 0.0)
    cand.companion_protein = float(companion.protein or 0.0)
    cand.companion_personal = personal
    return cand


def _eligible(cand: Candidate, budget: int, meal_type: str | None, companions: Companions) -> bool:
    if cand.role is None:
        cand.role = _legacy_role(cand.group_name or cand.name)
    if cand.role == ROLE_MEAL:
        attach_companion(cand, companions.get(cand.key))
    return all(math.isfinite(v) and v >= 0 for v in (cand.carbs, cand.protein, cand.fat)) and meal_worthy(
        cand.total_calories, budget, cand.role, meal_type=meal_type,
    )


def _from_stat(stat: FoodStat, source: str) -> Candidate:
    return Candidate(
        key=stat.key, name=stat.name, calories=round(stat.calories, 1),
        carbs=round(stat.carbs, 1), protein=round(stat.protein, 1), fat=round(stat.fat, 1),
        source=source, freq=stat.score if source == "personal" else 0.0,
        popularity=stat.score if source == "popular" else 0.0,
        last_eaten=stat.last_eaten if source == "personal" else None,
        group_id=stat.group_id, group_name=stat.group_name, family=stat.family, role=stat.role,
    )


def _from_stats(
    stats: list[FoodStat], source: str, budget: int, top: int,
    meal_type: str | None, companions: Companions,
) -> list[Candidate]:
    picked: list[Candidate] = []
    for stat in stats:
        cand = _from_stat(stat, source)
        if _eligible(cand, budget, meal_type, companions):
            picked.append(cand)
    return picked[:max(0, top)]


def personal_frequent(
    stats: list[FoodStat], budget_kcal: int, top: int = PERSONAL_TOP, *,
    meal_type: str | None = None, companions: Companions | None = None,
) -> list[Candidate]:
    return _from_stats(stats, "personal", budget_kcal, top, meal_type, companions or {})


def popular(
    stats: list[FoodStat], budget_kcal: int, top: int = POPULAR_TOP, *,
    meal_type: str | None = None, companions: Companions | None = None,
) -> list[Candidate]:
    return _from_stats(stats, "popular", budget_kcal, top, meal_type, companions or {})


def _similarity_pool(db: Session, index: GroupIndex) -> list[Candidate]:
    if index.enabled:
        return [
            Candidate(g.key, g.name, float(g.calories), float(g.carbs), float(g.protein),
                      float(g.fat), "catalog", group_id=g.id, group_name=g.name,
                      family=g.family, role=g.role)
            for g in sorted(index.by_id.values(), key=lambda g: g.key)
            if g.has_macros and g.role in RECOMMENDABLE_ROLES
        ]
    stmt = select(NutritionItem).where(
        per_serving_representatives(),
        or_(NutritionItem.source == "seed", NutritionItem.external_id.like("gen:%")),
    ).order_by(NutritionItem.id)
    # 같은 정규화 이름의 시드·총칭이 함께 존재해도 후보 슬롯은 하나만 쓴다.
    return merge([
        Candidate(p.normalized_name, p.name, float(p.calories), float(p.carbs),
                  float(p.protein), float(p.fat), "catalog", role=_legacy_role(p.name))
        for p in db.scalars(stmt)
    ])


def enrich_similarity(candidates: list[Candidate], anchors: list[FoodStat]) -> None:
    """다른 생성기가 먼저 찾은 메뉴도 자기 자신을 제외한 유사도 근거를 유지한다."""
    profiles = [(anchor, _profile(anchor)) for anchor in anchors]
    for cand in candidates:
        profile = _profile(cand)
        for anchor, anchor_profile in profiles:
            if anchor.key == cand.key:
                continue
            score, kind = compare_foods(anchor_profile, profile)
            if kind and score > cand.similarity:
                cand.similarity = score
                cand.similar_to = anchor.group_name or anchor.name
                cand.similar_type = kind


def nutrient_similar(
    db: Session, anchors: list[FoodStat], budget_kcal: int, *,
    index: GroupIndex | None = None, top: int = SIMILAR_TOP,
    exclude_keys: frozenset[str] = frozenset(), meal_type: str | None = None,
    companions: Companions | None = None,
) -> list[Candidate]:
    """음식 형태·명시된 재료·조리 방식을 비교하고, anchor별로 교대로 후보를 확보한다.

    한 종류의 세부 변형이 다른 선호 메뉴의 후보를 모두 밀어내지 않도록 한다.
    예산은 밥을 포함해 자격만 확인한다. 최종 영양 적합도와 개인 선호는 랭킹이 정한다.
    """
    if not anchors or top <= 0:
        return []
    index = index or load_group_index(db)
    choices = companions if companions is not None else {
        g.key: index.companion_of(g.key) for g in index.by_id.values()
    }
    skip = set(exclude_keys) | {a.key for a in anchors}
    pool = [c for c in _similarity_pool(db, index)
            if c.key not in skip and _eligible(c, budget_kcal, meal_type, choices)]
    profiles = [(c, _profile(c)) for c in pool]
    queues: list[list[Candidate]] = []
    for anchor in anchors:
        anchor_profile = _profile(anchor)
        queue = []
        for cand, profile in profiles:
            sim, kind = compare_foods(anchor_profile, profile)
            if kind:
                queue.append(replace(cand, source="similar", similarity=sim,
                                     similar_to=anchor.group_name or anchor.name, similar_type=kind))
        queue.sort(key=lambda c: (-c.similarity, c.key))
        queues.append(queue)
    picked: list[Candidate] = []
    seen: set[str] = set()
    while len(picked) < top:
        added = False
        for queue in queues:
            while queue and queue[0].key in seen:
                queue.pop(0)
            if queue and len(picked) < top:
                cand = queue.pop(0)
                seen.add(cand.key)
                picked.append(cand)
                added = True
        if not added:
            break
    enrich_similarity(picked, anchors)  # 탐색 순서와 무관하게 설명·점수는 가장 가까운 anchor
    return picked


def catalog_fallback(
    db: Session, budget_kcal: int, *, index: GroupIndex | None = None,
    meal_type: str | None = None, companions: Companions | None = None,
    exclude_keys: frozenset[str] = frozenset(), top: int = 15,
) -> list[Candidate]:
    """이력 근거가 부족할 때 사용할 분류·1인분 값이 있는 기본 메뉴. 인기를 만들지 않는다."""
    index = index or load_group_index(db)
    choices = companions if companions is not None else {
        g.key: index.companion_of(g.key) for g in index.by_id.values()
    }
    pool = [c for c in _similarity_pool(db, index)
            if c.key not in exclude_keys and c.role in RECOMMENDABLE_ROLES
            and _eligible(c, budget_kcal, meal_type, choices)]
    # 실제 랭킹과 같은 비대칭 열량 적합도를 사용하되 개인 근거를 꾸미지 않는다.
    from .ranking import fit_score
    pool.sort(key=lambda c: (-fit_score(c.total_calories, budget_kcal), c.key))
    # 한 계열의 세부 메뉴가 처음 top개를 독점하면 랭킹에서 다양성을 회복할 수 없다.
    # 적합도가 높은 계열부터 하나씩 확보하고, 남은 자리는 다음 회차에서 채운다.
    queues: dict[str, deque[Candidate]] = {}
    for cand in pool:
        kind = cand.family or _profile(cand).kind or cand.key
        queues.setdefault(kind, deque()).append(cand)
    picked = []
    while len(picked) < max(0, top) and any(queues.values()):
        for queue in queues.values():
            if queue and len(picked) < top:
                picked.append(queue.popleft())
    return picked


def merge(*groups: list[Candidate]) -> list[Candidate]:
    """주된 출처·표시는 첫 후보를 유지하고, 여러 생성기의 독립적인 근거는 보존한다."""
    merged: dict[str, Candidate] = {}
    for group in groups:
        for cand in group:
            if cand.key not in merged:
                merged[cand.key] = replace(cand)
                continue
            existing = merged[cand.key]
            existing.freq = max(existing.freq, cand.freq)
            existing.popularity = max(existing.popularity, cand.popularity)
            if (cand.collaborative, cand.collaborative_support) > (existing.collaborative, existing.collaborative_support):
                existing.collaborative = cand.collaborative
                existing.collaborative_support = cand.collaborative_support
            if cand.similarity > existing.similarity:
                existing.similarity = cand.similarity
                existing.similar_to, existing.similar_type = cand.similar_to, cand.similar_type
    return list(merged.values())
