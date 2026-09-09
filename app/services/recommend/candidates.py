"""후보 생성 — 넓게 모으는 단계(재현율 담당). 순서는 ranking 이 정한다.

세 생성기는 서로의 구멍을 메운다:
  personal  끼니별 개인 감쇠 빈도 상위 — 익숙한 것. 신규 사용자는 비어 있다
  popular   전체 사용자의 그 끼니 인기 — 콜드스타트·탐색
  similar   자주 먹는 음식(anchor)과 같은 종류이거나 탄단지 비율이 비슷하면서 예산에 맞는 것
각 후보는 source 를 달고 나가서, 나중에 출처별 섭취율로 생성기 가치를 측정한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import NutritionItem

from .dish_type import dish_type
from .signals import FoodStat

PERSONAL_TOP = 8
POPULAR_TOP = 5
SIMILAR_TOP = 5

# 반찬·음료 컷 — 김치(20kcal)·단무지·쌈무는 매 끼 함께 기록돼 '자주 먹는 음식' 상위를
# 차지하지만 끼니 메뉴로 추천할 대상이 아니다 (운영 데이터 첫 미리보기에서 2위까지 올라옴).
# 절대 하한과 예산 비율 하한 중 큰 쪽 — 간식 예산(≈100)에서는 80kcal 하한이 작동한다.
MIN_MEAL_KCAL = 80
MIN_MEAL_BUDGET_SHARE = 0.15
# 상한 — 예산의 2배를 넘는 건 아무리 자주 먹어도 이 끼니 후보가 아니다. 간식 기록이 적어
# 전체 끼니로 폴백할 때 저녁 메뉴(김치찌개 320)가 간식 예산(72)에 올라오는 걸 막는다.
# 랭킹의 초과 감점만으로는 빈도 가중치(0.40)를 이기지 못했다 (운영 미리보기).
MAX_MEAL_BUDGET_MULT = 2.0


def meal_worthy(calories: float, budget_kcal: int) -> bool:
    """이 끼니 메뉴로 추천할 만한 크기인가 — 반찬·소량 음료(하한)와 예산 대비 과대 항목(상한)을 거른다."""
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
    similar_type: str | None = None  # anchor 와 같은 종류였으면 그 종류 (예: '찌개')
    last_eaten: datetime | None = None


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
    )


def personal_frequent(
    stats: list[FoodStat], budget_kcal: int, top: int = PERSONAL_TOP
) -> list[Candidate]:
    picked = [s for s in stats if meal_worthy(s.calories, budget_kcal)]
    return [_from_stat(s, "personal") for s in picked[:top]]


def popular(stats: list[FoodStat], budget_kcal: int, top: int = POPULAR_TOP) -> list[Candidate]:
    picked = [s for s in stats if meal_worthy(s.calories, budget_kcal)]
    return [_from_stat(s, "popular") for s in picked[:top]]


# --- 영양 유사 ---------------------------------------------------------------
#
# sim = 0.6 · 같은 종류(이름 어미: 찌개·덮밥·버거 …) + 0.4 · 탄단지 에너지 비율의 코사인
#
# 절대량(kcal·g) 벡터를 z-정규화해 비교하던 첫 버전은 "풀 평균 대비 치우친 방향"을 재는
# 것이라 김치찌개·돈까스를 먹는 사람에게 양념치킨을 "비슷하다"고 냈다 (운영 미리보기).
# 사람이 말하는 비슷한 음식은 종류가 같은 것이고, 그 안에서 비율이 가까운 순이다.

SIM_W_TYPE = 0.6
SIM_W_RATIO = 0.4

_Ratio = tuple[float, float, float]  # (탄, 단, 지) 에너지 비율


def _similarity_pool(db: Session) -> list[NutritionItem]:
    """유사 후보를 고를 풀 — 브랜드 메뉴명이 아닌 일반 이름만.

    시드(김치찌개·된장찌개 …)와 총칭 대표(gen:* — 치킨·라면 …)는 1인분 기준으로 환산돼
    있고 이름이 일반명이라 "몬스터 와퍼" 대신 "불고기버거" 수준으로 추천된다.
    """
    stmt = select(NutritionItem).where(
        NutritionItem.is_representative.is_(True),
        or_(NutritionItem.source == "seed", NutritionItem.external_id.like("gen:%")),
    )
    return list(db.scalars(stmt))


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


def nutrient_similar(
    db: Session,
    anchors: list[FoodStat],
    budget_kcal: int,
    *,
    top: int = SIMILAR_TOP,
    exclude_keys: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """자주 먹는 음식(anchor)과 같은 종류이거나 탄단지 비율이 비슷한 일반 메뉴.

    anchor 자신과 이미 있는 후보는 제외. 각 풀 항목은 anchor 들 중 가장 비슷한 것과의
    점수를 받고, **유사도 순**으로 top 개를 뽑는다. 예산은 동점일 때 가까운 쪽을 앞세우는
    데만 쓴다 — 예산 적합은 랭킹이 점수로 다루고, 여기서 예산으로 먼저 걸러내면
    "비슷한 것"이 아니라 "예산에 맞는 것"이 나온다 (첫 버전: 찌개 anchor 에 양념치킨).
    meal_worthy(예산의 0.15~2배)로 상식 밖 크기만 미리 제외한다.
    """
    if not anchors:
        return []
    skip = set(exclude_keys) | {a.key for a in anchors}
    pool = [
        p
        for p in _similarity_pool(db)
        if p.normalized_name not in skip and meal_worthy(float(p.calories), budget_kcal)
    ]
    if not pool:
        return []

    anchor_profiles = [
        (a, dish_type(a.name), macro_ratio(a.carbs, a.protein, a.fat)) for a in anchors
    ]
    scored: list[tuple[float, NutritionItem, FoodStat, bool]] = []
    for p in pool:
        p_type = dish_type(p.name)
        p_ratio = macro_ratio(float(p.carbs), float(p.protein), float(p.fat))
        best = max(
            (
                (*similarity(a_type, a_ratio, p_type, p_ratio), a)
                for a, a_type, a_ratio in anchor_profiles
            ),
            key=lambda t: t[0],
        )
        scored.append((best[0], p, best[2], best[1]))

    def budget_distance(item: NutritionItem) -> float:
        return abs(float(item.calories) - budget_kcal) if budget_kcal > 0 else 0.0

    scored.sort(key=lambda row: (-row[0], budget_distance(row[1]), row[1].normalized_name))
    tier = scored
    return [
        Candidate(
            key=p.normalized_name,
            name=p.name,
            calories=float(p.calories),
            carbs=float(p.carbs),
            protein=float(p.protein),
            fat=float(p.fat),
            source="similar",
            similarity=round(sim, 3),
            similar_to=anchor.name,
            similar_type=dish_type(anchor.name) if same_type else None,
        )
        for sim, p, anchor, same_type in tier[:top]
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
