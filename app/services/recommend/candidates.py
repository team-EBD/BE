"""개인·인기·음식 유사 후보를 넓게 모으고 같은 음식군의 근거를 합친다.

역할 및 밥 포함 영양을 먼저 확인한 뒤 후보 수를 제한한다. 실제 순위는 ranking이 정한다.
군이 없는 기존 DB는 알려진 음식 형태에 한해 역할을 보완한다.
"""
from __future__ import annotations

import math
import random
import re
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
    source: str  # personal | popular | similar | collaborative | catalog | healthy (주된 생성 경로)
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
    sodium: float | None = None  # 메인 1인분 나트륨 mg (군 대표값, 없으면 None)
    sugar: float | None = None  # 메인 1인분 당류 g
    companion_sodium: float = 0.0
    healthy: float = 0.0  # 건강식 생성기 근거 0~1 — 감량·건강 목표에서만 채운다

    @property
    def total_calories(self) -> float:
        return self.calories + self.companion_kcal

    @property
    def total_protein(self) -> float:
        return self.protein + self.companion_protein

    @property
    def total_sodium(self) -> float | None:
        return None if self.sodium is None else self.sodium + self.companion_sodium


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
        cand.companion_kcal = cand.companion_protein = cand.companion_sodium = 0.0
        cand.companion_personal = False
        return cand
    cand.companion_key = companion.key
    cand.companion_name = companion.name
    cand.companion_kcal = float(companion.calories or 0.0)
    cand.companion_protein = float(companion.protein or 0.0)
    cand.companion_sodium = float(companion.sodium or 0.0)
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
                      family=g.family, role=g.role, sodium=g.sodium, sugar=g.sugar)
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
    from .ranking import fit_score
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
        # 같은 계열은 유사도가 동점이라, 이름순이면 '가·갈' 메뉴(갈매기살·갈비)가 늘 앞에 선다. 예산 적합도로 가른다.
        queue.sort(key=lambda c: (-c.similarity, -fit_score(c.total_calories, budget_kcal), c.key))
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
    return _round_robin_by_family(pool, top)


def _round_robin_by_family(
    pool: list[Candidate], top: int, *, rotate: int = 0, rng: random.Random | None = None,
) -> list[Candidate]:
    """한 계열의 세부 메뉴가 처음 top개를 독점하면 랭킹에서 다양성을 회복할 수 없다.

    정렬된 pool 에서 계열마다 하나씩 확보하고, 남은 자리는 다음 회차에서 채운다.
    rotate > 1 이면 계열마다 상위 rotate 개의 순서를 rng 로 섞는다 (건강식 첫 카드 회전용).
    """
    queues: dict[str, deque[Candidate]] = {}
    for cand in pool:
        kind = cand.family or _profile(cand).kind or cand.key
        queues.setdefault(kind, deque()).append(cand)
    if rotate > 1 and rng is not None:
        for kind, queue in queues.items():
            items = list(queue)
            head = items[:rotate]
            rng.shuffle(head)
            queues[kind] = deque(head + items[rotate:])
    picked: list[Candidate] = []
    while len(picked) < max(0, top) and any(queues.values()):
        for queue in queues.values():
            if queue and len(picked) < top:
                picked.append(queue.popleft())
    return picked


HEALTHY_TOP = 6
HEALTHY_ROTATE = 3  # 계열마다 상위 n개 안에서 난수로 순서를 섞어 첫 화면이 모두 같은 카드가 되지 않게
MEAL_SODIUM_MAX = 1000.0  # 동반 밥 포함 mg — 된장국·미역국 한 그릇이 800~1,000 이라 그 위만 뺀다
SNACK_SODIUM_MAX = 300.0
MEAL_FAT_MAX = 20.0  # 메인 1인분 지방 g — 드레싱 샐러드(9 g)는 두고 수육(32 g)·훈제오리(38 g)는 뺀다
SNACK_KCAL_MAX = 250.0
SNACK_SUGAR_MAX = 15.0  # 유제품·음료의 당류 g 상한 — 과일·고구마의 자연 당은 보지 않는다
_SUGAR_FAMILIES = frozenset({"유제품", "음료"})
# 건강식은 사람이 정한 메뉴 범주가 1차 기준이다. 영양 수치만으로 고르면 닭꼬치구이·도가니탕처럼 수치는
# 좋아도 아무도 건강식이라 부르지 않는 메뉴가 먼저 올라온다 (2026-11-12). 수치는 아래 안전망으로만 쓴다.
_HEALTHY_NAME = re.compile(
    r"샐러드|비빔밥|나물밥|곤드레|콩나물밥|닭가슴살|닭안심|두부|청국장"
    r"|고등어|삼치|갈치|가자미|조기|황태|북어|연어|생선"
    r"|오징어(찜|볶음|조림|덮밥)|낙지(찜|볶음|덮밥|비빔밥)|새우(구이|찜|볶음)|조개(탕|구이)|해물찜|콩나물찜"
    r"|된장국|미역국|콩나물국|무국|뭇국|달걀국|달걀탕|샤브|백숙|삼계탕"
    r"|콩국수|메밀국수|죽(?!순)|오믈렛|계란덮밥|쌈밥|월남쌈"
    r"|달걀|계란|고구마|과일|견과|요거트|요구르트|두유|오트밀|현미|잡곡|곤약"
)
# 범주에 걸려도 가공육·내장·튀김·부침(…전)·기름진 중식·견과 죽·설탕 양념·진한 국물·롤은 뺀다
_UNHEALTHY_NAME = re.compile(
    r"런천|베이컨|소시지|햄|곱창|대창|막창|껍데기|삼겹|오겹|차돌|튀김|김구이|데리야끼|곰탕|설렁탕|감자탕|부대|스팸|족발|갈비|탕수|롤"
    r"|전$|마파|땅콩죽|잣죽|들깨죽|흑임자죽|깨죽|맛탕"
)
_HEALTHY_SKIP_FAMILIES = frozenset({"버거·피자·샌드위치", "분식류", "튀김류", "빵·과자·디저트", "기타", "소스·양념", "김치·절임"})


def is_healthy(cand: Candidate) -> bool:
    """건강식 범주(_HEALTHY_NAME)에 들고 나트륨·지방·당류 안전망을 넘지 않는 군인지 판정한다."""
    if cand.family in _HEALTHY_SKIP_FAMILIES or _UNHEALTHY_NAME.search(cand.name):
        return False
    if not _HEALTHY_NAME.search(cand.name):
        return False
    sodium = cand.total_sodium
    if cand.role == ROLE_SNACK:
        sugary = cand.family in _SUGAR_FAMILIES and cand.sugar is not None and cand.sugar > SNACK_SUGAR_MAX
        return cand.calories <= SNACK_KCAL_MAX and (sodium is None or sodium <= SNACK_SODIUM_MAX) and not sugary
    return (sodium is None or sodium <= MEAL_SODIUM_MAX) and cand.fat <= MEAL_FAT_MAX


def healthy_candidates(
    db: Session, budget_kcal: int, *, index: GroupIndex | None = None,
    meal_type: str | None = None, companions: Companions | None = None,
    exclude_keys: frozenset[str] = frozenset(), top: int = HEALTHY_TOP,
    rng: random.Random | None = None,
) -> list[Candidate]:
    """감량·건강 목표와 '가볍게' 요청용 건강식 후보 — 이력과 무관하게 범주·영양 기준으로 고른다.

    풀은 유사 후보와 같은 군 풀(역할·대표값 검증 완료)이고, 예산 적합(미달은 약하게 감점) 순으로
    계열마다 하나씩 확보한다. rng 가 있으면 계열별 상위 HEALTHY_ROTATE 개 안에서 순서를 섞는다.
    근거(healthy)는 랭킹에서 인기와 개인 이력 사이의 바닥값으로 쓰인다.
    """
    index = index or load_group_index(db)
    choices = companions if companions is not None else {
        g.key: index.companion_of(g.key) for g in index.by_id.values()
    }
    pool = [c for c in _similarity_pool(db, index)
            if c.key not in exclude_keys and _eligible(c, budget_kcal, meal_type, choices) and is_healthy(c)]
    for c in pool:
        c.source, c.healthy = "healthy", 1.0
    from .ranking import DIET_UNDER_BUDGET_PENALTY, fit_score
    pool.sort(key=lambda c: (-fit_score(c.total_calories, budget_kcal, DIET_UNDER_BUDGET_PENALTY),
                             -c.total_protein, c.key))
    return _round_robin_by_family(pool, top, rotate=HEALTHY_ROTATE if rng is not None else 0, rng=rng)


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
            existing.healthy = max(existing.healthy, cand.healthy)
            if (cand.collaborative, cand.collaborative_support) > (existing.collaborative, existing.collaborative_support):
                existing.collaborative = cand.collaborative
                existing.collaborative_support = cand.collaborative_support
            if cand.similarity > existing.similarity:
                existing.similarity = cand.similarity
                existing.similar_to, existing.similar_type = cand.similar_to, cand.similar_type
    return list(merged.values())
