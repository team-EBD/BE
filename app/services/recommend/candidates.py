"""후보 생성 — 넓게 모으는 단계(재현율 담당). 순서는 ranking 이 정한다.

세 생성기는 서로의 구멍을 메운다:
  personal  끼니별 개인 감쇠 빈도 상위 — 익숙한 것. 신규 사용자는 비어 있다
  popular   전체 사용자의 그 끼니 인기 — 콜드스타트·탐색
  similar   자주 먹는 음식(anchor)과 같은 계열이거나 탄단지 비율이 비슷한 군 — 비슷하지만 새로운 것
각 후보는 source 를 달고 나가서, 나중에 출처별 섭취율로 생성기 가치를 측정한다.

군(food_groups)이 있으면 role 로 반찬·주식을 거르고 계열(family)로 종류를 판정한다.
군이 없는 DB 에서는 칼로리 휴리스틱·어미(dish_type)로 폴백한다 — 두 경로가 같은 코드다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import NutritionItem

from .dish_type import dish_type
from .groups import (
    RECOMMENDABLE_ROLES,
    ROLE_COMPANION,
    ROLE_EXCLUDE,
    GroupIndex,
    GroupInfo,
    load_group_index,
)
from .signals import FoodStat

PERSONAL_TOP = 8
POPULAR_TOP = 5
SIMILAR_TOP = 5

# 칼로리 컷 — 군 role 이 1차 판정이고 이건 2차 안전망이다 (군이 없는 이름·미분류 기록용).
# 김치(20kcal)·단무지·쌈무는 매 끼 함께 기록돼 '자주 먹는 음식' 상위를 차지하지만 메뉴가 아니다.
# 절대 하한과 예산 비율 하한 중 큰 쪽 — 간식 예산(≈100)에서는 80kcal 하한이 작동한다.
MIN_MEAL_KCAL = 80
MIN_MEAL_BUDGET_SHARE = 0.15
# 상한 — 예산의 2배를 넘는 건 아무리 자주 먹어도 이 끼니 후보가 아니다. 간식 기록이 적어
# 전체 끼니로 폴백할 때 저녁 메뉴(김치찌개 320)가 간식 예산(72)에 올라오는 걸 막는다.
MAX_MEAL_BUDGET_MULT = 2.0


def meal_worthy(calories: float, budget_kcal: int, role: str | None = None) -> bool:
    """이 끼니 메뉴로 추천할 만한가 — role(반찬·주식 제외) → 칼로리 상·하한."""
    if role in (ROLE_EXCLUDE, ROLE_COMPANION):
        return False
    low = max(MIN_MEAL_KCAL, budget_kcal * MIN_MEAL_BUDGET_SHARE)
    high = budget_kcal * MAX_MEAL_BUDGET_MULT if budget_kcal > 0 else float("inf")
    return low <= calories <= high


@dataclass
class Candidate:
    key: str
    name: str
    calories: float  # 1인분
    carbs: float
    protein: float
    fat: float
    source: str  # personal | popular | similar
    freq: float = 0.0  # 개인 감쇠 빈도 (personal 만)
    popularity: int = 0  # 기록 건수
    similarity: float = 0.0  # anchor 와의 유사도 0~1 (similar 만)
    similar_to: str | None = None  # 가장 비슷했던 anchor 표시명 (similar 만)
    similar_type: str | None = None  # anchor 와 같은 계열/종류였으면 그 이름 (예: '국·탕·찌개류')
    last_eaten: datetime | None = None
    group_id: int | None = None
    group_name: str | None = None
    family: str | None = None
    role: str | None = None
    # 동반(밥) — 엔진이 붙인다. 예산 적합·라벨은 메인 + 동반 합산으로 계산한다
    companion_key: str | None = None
    companion_name: str | None = None
    companion_kcal: float = 0.0
    companion_personal: bool = False  # True 면 사용자의 동시기록에서 온 동반, False 면 군 기본값

    @property
    def total_calories(self) -> float:
        return self.calories + self.companion_kcal


def _from_stat(stat: FoodStat, source: str) -> Candidate:
    return Candidate(
        key=stat.key,
        name=stat.name,
        calories=round(stat.calories, 1),
        carbs=round(stat.carbs, 1),
        protein=round(stat.protein, 1),
        fat=round(stat.fat, 1),
        source=source,
        freq=stat.score if source == "personal" else 0.0,
        popularity=stat.count,
        last_eaten=stat.last_eaten,
        group_id=stat.group_id,
        group_name=stat.group_name,
        family=stat.family,
        role=stat.role,
    )


def personal_frequent(
    stats: list[FoodStat], budget_kcal: int, top: int = PERSONAL_TOP
) -> list[Candidate]:
    picked = [s for s in stats if meal_worthy(s.calories, budget_kcal, s.role)]
    return [_from_stat(s, "personal") for s in picked[:top]]


def popular(stats: list[FoodStat], budget_kcal: int, top: int = POPULAR_TOP) -> list[Candidate]:
    picked = [s for s in stats if meal_worthy(s.calories, budget_kcal, s.role)]
    return [_from_stat(s, "popular") for s in picked[:top]]


# --- 영양 유사 ---------------------------------------------------------------
#
# sim = 0.6 · 같은 종류 + 0.4 · 탄단지 에너지 비율의 코사인
#   같은 종류 = 군이 있으면 **같은 계열(family)**, 없으면 이름 어미(dish_type) 일치
#
# 절대량(kcal·g) 벡터를 z-정규화해 비교하던 첫 버전은 "풀 평균 대비 치우친 방향"을 재는
# 것이라 김치찌개·돈까스를 먹는 사람에게 양념치킨을 "비슷하다"고 냈다 (운영 미리보기).
# 어미 60개만 쓰던 두 번째 버전은 종류 일치 5/20 — 칼국수(국수)와 비빔냉면(냉면)이 달랐다.
# 계열은 그 둘을 '면류'로 묶는다. 종류가 다르면 후보로 내지 않는다 (억지로 채운 5개는 소음).

SIM_W_TYPE = 0.6
SIM_W_RATIO = 0.4

_Ratio = tuple[float, float, float]  # (탄, 단, 지) 에너지 비율


@dataclass(frozen=True)
class PoolItem:
    key: str
    name: str
    calories: float
    carbs: float
    protein: float
    fat: float
    family: str | None
    group_id: int | None
    role: str | None


def _similarity_pool(db: Session, index: GroupIndex) -> list[PoolItem]:
    """유사 후보 풀 — 군이 있으면 추천 가능 군(대표값 있는 것) 전체, 없으면 시드+총칭 대표 항목."""
    if index.enabled:
        return [
            PoolItem(g.key, g.name, g.calories, g.carbs or 0.0, g.protein or 0.0, g.fat or 0.0,
                     g.family, g.id, g.role)  # type: ignore[arg-type]
            for g in index.by_id.values()
            if g.has_macros and g.role in RECOMMENDABLE_ROLES
        ]
    stmt = select(NutritionItem).where(
        NutritionItem.is_representative.is_(True),
        or_(NutritionItem.source == "seed", NutritionItem.external_id.like("gen:%")),
    )
    return [
        PoolItem(p.normalized_name, p.name, float(p.calories), float(p.carbs), float(p.protein),
                 float(p.fat), None, None, None)
        for p in db.scalars(stmt)
    ]


def macro_ratio(carbs: float, protein: float, fat: float) -> _Ratio:
    """탄·단·지가 열량에서 차지하는 비율 (4·4·9 kcal/g). 열량 0이면 균등."""
    energy = (max(carbs, 0.0) * 4, max(protein, 0.0) * 4, max(fat, 0.0) * 9)
    total = sum(energy)
    if total <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return tuple(e / total for e in energy)  # type: ignore[return-value]


def _cosine(a: _Ratio, b: _Ratio) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def similarity(
    anchor_type: str | None, anchor_ratio: _Ratio, item_type: str | None, item_ratio: _Ratio
) -> tuple[float, bool]:
    """(유사도 0~1, 같은 종류였는가)."""
    same_type = anchor_type is not None and anchor_type == item_type
    return SIM_W_TYPE * float(same_type) + SIM_W_RATIO * _cosine(anchor_ratio, item_ratio), same_type


def _kind(family: str | None, name: str) -> str | None:
    """종류 판정 키 — 계열이 있으면 계열, 없으면 어미."""
    return family or dish_type(name)


def nutrient_similar(
    db: Session,
    anchors: list[FoodStat],
    budget_kcal: int,
    *,
    index: GroupIndex | None = None,
    top: int = SIMILAR_TOP,
    exclude_keys: frozenset[str] = frozenset(),
    require_same_kind: bool = True,
) -> list[Candidate]:
    """자주 먹는 음식(anchor)과 같은 계열(종류)이면서 탄단지 비율이 비슷한 군.

    anchor 자신과 이미 있는 후보는 제외. 각 풀 항목은 anchor 들 중 가장 비슷한 것과의
    점수를 받고 **유사도 순**으로 top 개를 뽑는다. 예산은 동점일 때 가까운 쪽을 앞세우는
    데만 쓴다 — 예산 적합은 랭킹이 점수로 다룬다. require_same_kind 면 종류가 다른 항목은
    후보로 내지 않는다 (탄단지 비율만으로는 한식 대부분이 0.98~1.0 이라 변별력이 없다).
    """
    if not anchors:
        return []
    index = index or load_group_index(db)
    skip = set(exclude_keys) | {a.key for a in anchors}
    pool = [
        p for p in _similarity_pool(db, index)
        if p.key not in skip and meal_worthy(p.calories, budget_kcal, p.role)
    ]
    if not pool:
        return []

    anchor_profiles = [
        (a, _kind(a.family, a.name), macro_ratio(a.carbs, a.protein, a.fat)) for a in anchors
    ]
    scored: list[tuple[float, PoolItem, FoodStat, bool]] = []
    for p in pool:
        p_kind = _kind(p.family, p.name)
        p_ratio = macro_ratio(p.carbs, p.protein, p.fat)
        best = max(
            ((*similarity(a_kind, a_ratio, p_kind, p_ratio), a) for a, a_kind, a_ratio in anchor_profiles),
            key=lambda t: t[0],
        )
        if require_same_kind and not best[1]:
            continue
        scored.append((best[0], p, best[2], best[1]))

    def budget_distance(item: PoolItem) -> float:
        return abs(item.calories - budget_kcal) if budget_kcal > 0 else 0.0

    scored.sort(key=lambda row: (-row[0], budget_distance(row[1]), row[1].key))
    return [
        Candidate(
            key=p.key,
            name=p.name,
            calories=p.calories,
            carbs=p.carbs,
            protein=p.protein,
            fat=p.fat,
            source="similar",
            similarity=round(sim, 3),
            similar_to=anchor.name,
            similar_type=_kind(anchor.family, anchor.name) if same_kind else None,
            group_id=p.group_id,
            group_name=p.name if p.group_id else None,
            family=p.family,
            role=p.role,
        )
        for sim, p, anchor, same_kind in scored[:top]
    ]


def merge(*groups: list[Candidate]) -> list[Candidate]:
    """합집합 — 같은 키는 먼저 등장한 것(personal → popular → similar 순)만 남긴다."""
    seen: set[str] = set()
    merged: list[Candidate] = []
    for group in groups:
        for cand in group:
            if cand.key in seen:
                continue
            seen.add(cand.key)
            merged.append(cand)
    return merged


def attach_companion(
    cand: Candidate, companion: GroupInfo | None, *, personal: bool = False
) -> Candidate:
    """동반(밥) 붙이기 — 대표값이 있는 companion 군만. personal 은 문구('함께 드시던')에만 쓴다."""
    if companion is None or not companion.has_macros:
        cand.companion_key = cand.companion_name = None
        cand.companion_kcal = 0.0
        cand.companion_personal = False
        return cand
    cand.companion_key = companion.key
    cand.companion_name = companion.name
    cand.companion_kcal = float(companion.calories or 0.0)
    cand.companion_personal = personal
    return cand
