"""추천 신호 계산 — 끼니별 개인 빈도(시간 감쇠) · 전체 인기 · 질림 · 끼니 예산 · 밥 동반.

기록 수가 사용자당 수백 건 수준이라 행을 가져와 파이썬에서 집계한다 — SQLite 테스트와
Postgres 운영에서 같은 코드가 돈다.

음식 묶음 키는 **군**(food_groups)이다 — 기록의 food_group_id → 매칭 상품의 food_group_id →
이름 alias 순으로 찾고, 군이 없으면 normalize_name(기록 이름)으로 폴백한다 (groups.GroupIndex).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.timeutil import from_db, kst_date_of
from app.models import MealItem, MealRecord, NutritionItem, User
from app.services.matching import normalize_name
from app.services.summary import aggregate_day, get_goals

from .groups import ROLE_COMPANION, ROLE_MEAL, GroupIndex, GroupInfo, load_group_index

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# 끼니 예산 기본 비율 — 사용자 이력이 충분치 않을 때
DEFAULT_MEAL_RATIOS = {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.35, "snack": 0.05}
# 개인 비율을 그대로 쓰면 극단으로 간다 — 아침을 거의 안 기록하는 사용자는 아침 예산이
# 100kcal 이 되고(반찬만 추천됨), 저녁만 기록하는 사용자는 1062kcal 이 됐다(운영 미리보기).
# 기본 비율과 반씩 섞고 끼니별 하한을 둔다.
PERSONAL_RATIO_WEIGHT = 0.5
MIN_RATIO_BY_MEAL = {"breakfast": 0.15, "lunch": 0.15, "dinner": 0.15, "snack": 0.05}
# 상한 — 저녁만 기록하는 사용자는 개인 비율 1.0 → 혼합 0.675 → 예산 1,350 이 된다.
# 한 끼가 하루 목표의 절반을 넘게 잡히지 않도록 막는다.
MAX_RATIO_BY_MEAL = {"breakfast": 0.4, "lunch": 0.5, "dinner": 0.5, "snack": 0.15}

# 개인 빈도 = 습관 강도. 반감기 45일의 완만한 감쇠 — 석 달 전에 끊긴 습관은 서서히
# 빠지되, 최근에 먹었다고 점수가 튀지는 않는다. "최근에 먹은 건 오히려 안 먹는다"는
# 별도 신호(recency_penalties)가 감점으로 다룬다.
HALF_LIFE_DAYS = 45
FREQ_WINDOW_DAYS = 90
# 그 끼니의 기록 항목이 이보다 적으면 전체 끼니로 확장해서 센다 (콜드스타트)
MIN_MEAL_TYPE_ITEMS = 5
# 단 간식은 확장하지 않는다 — 식사에 곁들여 기록한 소스·반찬이 "간식에 자주 먹는 음식"
# 으로 올라온다 (운영 미리보기: 스위트칠리소스·크림치즈가 간식 추천 1·2위).
NO_FALLBACK_MEAL_TYPES = frozenset({"snack"})

# 오기록 방어 — 기록의 1인분 열량이 대표값(군 대표값 → 상품 대표값)과 이만큼 벌어지면 집계에서 뺀다.
# 운영에 "김치 320kcal"(0.1인분 32kcal 로 저장된 걸 1인분으로 되돌린 값 — 기준이 된
# AI 추정치가 틀렸다), "김 480kcal" 같은 기록이 있고 그대로 추천·anchor 로 나갔다.
# 범위를 넓게 잡은 이유: 어긋난 값은 아래 '대표값으로 대체'가 먼저 바로잡고,
# 이 컷은 대체로도 설명이 안 되는 명백한 파손만 걸러내면 된다.
MISRECORD_MIN_RATIO = 0.25
MISRECORD_MAX_RATIO = 4.0

POPULARITY_WINDOW_DAYS = 60

# 질림(최근 섭취) 감점 — 그 음식의 재섭취 주기 대비 얼마나 지났나.
# 3번 이상 먹었으면 개인 평균 간격을, 아니면 기본 5일을 주기로 본다.
# 어제 먹었으면 0.8, 주기의 절반이 지나면 0.5, 주기를 넘기면 0.
DEFAULT_REEAT_INTERVAL_DAYS = 5.0
MIN_EATS_FOR_INTERVAL = 3
MIN_REEAT_INTERVAL_DAYS = 1.0

# 끼니 예산 — 최근 2주 기록으로 끼니별 비율을 만들고, 7건 미만이면 기본 비율
RATIO_WINDOW_DAYS = 14
MIN_RATIO_RECORDS = 7
# 남은 하루 칼로리가 끼니 비율 예산보다 작을 때, 예산이 0 근처로 떨어지지 않게 하는 하한
BUDGET_FLOOR = 0.3
FIT_TOLERANCE = 0.2  # ±20% 안이면 '적정'
MOOD_FACTOR = {"any": 1.0, "light": 0.8, "hearty": 1.2}

# 밥 동반 — 개인 동시기록이 이 비율 이상이면 그 동반을 쓰고, 미만이면 "동반 없이 먹는 사람"
COMPANION_MIN_SHARE = 0.5
COMPANION_MIN_MEALS = 2  # 그 메뉴를 이만큼은 먹어 봐야 개인 동반 판단을 신뢰한다

# 전체 인기 계산에서 제외할 계정 (내부 테스트 계정)
EXCLUDED_EMAIL_SUFFIXES = ("@test.com",)


def group_key(food_name: str) -> str:
    """군을 못 찾았을 때의 폴백 키 — 온도·사이즈 표기와 공백을 벗긴 이름."""
    return normalize_name(food_name)


def meal_type_for_hour(hour: int) -> str:
    """KST 시각 → 끼니. FE utils/mealTime.mealTypeForHour 와 같은 경계(11/16/21)."""
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 21:
        return "dinner"
    return "snack"


@dataclass
class FoodStat:
    """묶음 키 하나의 집계. 영양값은 1인분 기준(군 대표값 우선, 없으면 기록값 평균)."""

    key: str
    name: str  # 표시용 — 기록에서 가장 많이 쓰인 원문 이름 (사용자 어휘)
    score: float  # 감쇠 합(개인) 또는 건수(인기)
    count: int
    last_eaten: datetime
    calories: float
    carbs: float
    protein: float
    fat: float
    group_id: int | None = None
    group_name: str | None = None
    family: str | None = None
    role: str | None = None


@dataclass
class _Row:
    name: str
    eaten_at: datetime
    calories: float
    carbs: float
    protein: float
    fat: float
    group: GroupInfo | None
    record_id: int


_Macros = tuple[float, float, float, float]  # (kcal, 탄, 단, 지) 1인분


def _plausible(recorded_per_serving: float, db_calories: float) -> bool:
    """기록된 1인분 열량이 대표값과 상식 범위 안인가."""
    if db_calories <= 0:
        return True
    ratio = recorded_per_serving / db_calories
    return MISRECORD_MIN_RATIO <= ratio <= MISRECORD_MAX_RATIO


def _reference_by_name(db: Session, keys: set[str]) -> dict[str, _Macros]:
    """이름(정규화)으로 찾은 영양 DB 대표값 — 군도 매칭 id 도 없는 기록의 마지막 대조 기준."""
    if not keys:
        return {}
    rows = db.execute(
        select(
            NutritionItem.normalized_name,
            NutritionItem.calories,
            NutritionItem.carbs,
            NutritionItem.protein,
            NutritionItem.fat,
        )
        .where(
            NutritionItem.is_representative.is_(True),
            NutritionItem.normalized_name.in_(keys),
        )
        .order_by(NutritionItem.normalized_name, NutritionItem.id)
    )
    found: dict[str, _Macros] = {}
    for name, cal, carbs, protein, fat in rows:
        found.setdefault(name, (float(cal), float(carbs), float(protein), float(fat)))
    return found


def _eaten_rows(
    db: Session,
    *,
    since: datetime,
    index: GroupIndex,
    user_id: int | None = None,
    meal_type: str | None = None,
    exclude_test_users: bool = False,
) -> list[_Row]:
    """실제로 먹은 기록 항목(삭제·생략 제외)을 군·1인분 영양값으로 정규화해 반환.

    영양값 우선순위: ① 군 대표값 ② 매칭된 대표 상품값 ③ 이름으로 찾은 대표 상품값 ④ 기록값.
    기록값은 AI 추정치를 사용자가 슬라이더로 조절한 결과라 1인분으로 되돌리면 기준 오차가
    증폭된다 (운영: 0.1인분 32kcal 로 저장된 김치 → 1인분 320kcal). 대표값과 대조해 설명이
    안 되는 기록은 아예 뺀다(MISRECORD_*).
    """
    stmt = (
        select(
            MealItem.food_name,
            MealRecord.eaten_at,
            MealRecord.id,
            MealItem.calories,
            MealItem.carbs,
            MealItem.protein,
            MealItem.fat,
            MealItem.serving_amount,
            MealItem.food_group_id,
            NutritionItem.food_group_id,
            NutritionItem.is_representative,
            NutritionItem.calories,
            NutritionItem.carbs,
            NutritionItem.protein,
            NutritionItem.fat,
        )
        .join(MealRecord, MealItem.meal_record_id == MealRecord.id)
        .outerjoin(NutritionItem, MealItem.nutrition_item_id == NutritionItem.id)
        .where(
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= since,
        )
    )
    if user_id is not None:
        stmt = stmt.where(MealRecord.user_id == user_id)
    if meal_type is not None:
        stmt = stmt.where(MealRecord.meal_type == meal_type)
    if exclude_test_users:
        stmt = stmt.join(User, User.id == MealRecord.user_id)
        for suffix in EXCLUDED_EMAIL_SUFFIXES:
            # email 이 NULL 인 소셜 계정은 제외 대상이 아니다 (NOT LIKE 가 NULL 이 되지 않게)
            stmt = stmt.where(or_(User.email.is_(None), ~User.email.like(f"%{suffix}")))

    raw: list[tuple[str, datetime, int, _Macros, _Macros | None, GroupInfo | None]] = []
    for (
        name, eaten_at, record_id, cal, carbs, protein, fat, serving,
        meal_gid, item_gid, item_rep, db_cal, db_carbs, db_protein, db_fat,
    ) in db.execute(stmt):
        divisor = float(serving or 0) or 1.0  # meal_items 값은 섭취량 반영값 → 1인분으로 되돌림
        recorded = (
            float(cal) / divisor,
            float(carbs) / divisor,
            float(protein) / divisor,
            float(fat) / divisor,
        )
        group = index.resolve(name, meal_gid, item_gid)
        reference: _Macros | None = None
        if group is not None and group.has_macros:
            reference = (group.calories, group.carbs or 0.0, group.protein or 0.0, group.fat or 0.0)  # type: ignore[arg-type]
        elif item_rep and db_cal is not None:
            reference = (float(db_cal), float(db_carbs), float(db_protein), float(db_fat))
        raw.append((name, from_db(eaten_at), record_id, recorded, reference, group))

    # 군도 매칭도 없는 기록은 이름으로 대표값을 한 번 더 찾는다
    by_name = _reference_by_name(
        db, {group_key(name) for name, _, _, _, ref, _ in raw if ref is None}
    )

    rows: list[_Row] = []
    for name, eaten_at, record_id, recorded, reference, group in raw:
        reference = reference or by_name.get(group_key(name))
        if reference is not None and not _plausible(recorded[0], reference[0]):
            continue
        calories, carbs, protein, fat = reference or recorded
        rows.append(
            _Row(
                name=name, eaten_at=eaten_at, calories=calories, carbs=carbs,
                protein=protein, fat=fat, group=group, record_id=record_id,
            )
        )
    return rows


def _key_of(row: _Row) -> str:
    return row.group.key if row.group else group_key(row.name)


def _aggregate(rows: list[_Row], weight_of) -> list[FoodStat]:
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        key = _key_of(row)
        if key:
            groups[key].append(row)

    stats: list[FoodStat] = []
    for key, group in groups.items():
        n = len(group)
        info = next((r.group for r in group if r.group), None)
        stats.append(
            FoodStat(
                key=key,
                name=Counter(r.name for r in group).most_common(1)[0][0],
                score=round(sum(weight_of(r) for r in group), 4),
                count=n,
                last_eaten=max(r.eaten_at for r in group),
                calories=sum(r.calories for r in group) / n,
                carbs=sum(r.carbs for r in group) / n,
                protein=sum(r.protein for r in group) / n,
                fat=sum(r.fat for r in group) / n,
                group_id=info.id if info else None,
                group_name=info.name if info else None,
                family=info.family if info else None,
                role=info.role if info else None,
            )
        )
    # 점수 내림차순, 동점은 키 순 — 같은 입력이면 같은 순서
    stats.sort(key=lambda s: (-s.score, s.key))
    return stats


def decayed_frequency(
    db: Session,
    user_id: int,
    meal_type: str | None,
    *,
    now: datetime,
    index: GroupIndex | None = None,
    half_life_days: float = HALF_LIFE_DAYS,
    window_days: int = FREQ_WINDOW_DAYS,
    min_items: int = MIN_MEAL_TYPE_ITEMS,
) -> list[FoodStat]:
    """끼니별 '자주 먹는 음식' — 습관 강도.

    score = Σ 0.5 ** (지난 일수 / half_life_days). 반감기 45일이면 오늘 1.0, 한 달 반 전 0.5,
    석 달 전 0.25 — 반복 횟수가 주로 결정하고, 끊긴 습관만 서서히 빠진다.
    최근에 먹었는지는 여기서 가산하지 않는다 (recency_penalties 가 감점으로 처리).
    그 끼니의 항목이 min_items 미만이면 끼니 구분 없이 다시 센다 — 간식은 예외.
    """
    index = index or load_group_index(db)
    since = now - timedelta(days=window_days)
    rows = _eaten_rows(db, since=since, index=index, user_id=user_id, meal_type=meal_type)
    can_fall_back = meal_type is not None and meal_type not in NO_FALLBACK_MEAL_TYPES
    if can_fall_back and len(rows) < min_items:
        rows = _eaten_rows(db, since=since, index=index, user_id=user_id)

    def weight(row: _Row) -> float:
        days = max((now - row.eaten_at).total_seconds() / 86400, 0.0)
        return 0.5 ** (days / half_life_days)

    return _aggregate(rows, weight)


def global_popularity(
    db: Session,
    meal_type: str | None,
    *,
    now: datetime,
    index: GroupIndex | None = None,
    window_days: int = POPULARITY_WINDOW_DAYS,
) -> list[FoodStat]:
    """전체 사용자의 그 끼니 기록 건수 순 (내부 테스트 계정 제외). 콜드스타트·탐색용."""
    index = index or load_group_index(db)
    rows = _eaten_rows(
        db, since=now - timedelta(days=window_days), index=index, meal_type=meal_type,
        exclude_test_users=True,
    )
    return _aggregate(rows, lambda _row: 1.0)


def recency_penalties(
    db: Session,
    user_id: int,
    *,
    now: datetime,
    index: GroupIndex | None = None,
    window_days: int = FREQ_WINDOW_DAYS,
    default_interval_days: float = DEFAULT_REEAT_INTERVAL_DAYS,
) -> dict[str, float]:
    """음식 키 → 질림 감점(0~1). 최근에 먹었을수록, 그 음식의 재섭취 주기가 길수록 크다.

    penalty = max(0, 1 − 마지막 섭취 후 일수 / 재섭취 주기)
    재섭취 주기 = 3번 이상 먹은 음식이면 먹은 날들 사이의 평균 간격, 아니면 기본 5일.
    주기를 넘긴 음식은 0 — 다시 먹을 때가 됐다.
    """
    index = index or load_group_index(db)
    rows = _eaten_rows(db, since=now - timedelta(days=window_days), index=index, user_id=user_id)
    eaten_days: dict[str, set] = defaultdict(set)
    for row in rows:
        key = _key_of(row)
        if key:
            eaten_days[key].add(row.eaten_at.date())

    penalties: dict[str, float] = {}
    for key, days in eaten_days.items():
        ordered = sorted(days)
        if len(ordered) >= MIN_EATS_FOR_INTERVAL:
            gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
            interval = max(sum(gaps) / len(gaps), MIN_REEAT_INTERVAL_DAYS)
        else:
            interval = default_interval_days
        since_last = max((now.date() - ordered[-1]).days, 0)
        penalty = max(0.0, 1.0 - since_last / interval)
        if penalty > 0:
            penalties[key] = round(penalty, 3)
    return penalties


@dataclass
class CompanionStat:
    """메인 메뉴 키 → 사용자가 함께 먹는 동반(밥) 판단."""

    companion_key: str | None  # 가장 자주 함께 기록된 companion 군. None = 동반 없이 먹음
    share: float  # 그 메뉴 기록 중 동반이 함께 있던 비율
    meals: int  # 그 메뉴를 먹은 기록 수


def companion_stats(
    db: Session,
    user_id: int,
    *,
    now: datetime,
    index: GroupIndex | None = None,
    window_days: int = FREQ_WINDOW_DAYS,
) -> dict[str, CompanionStat]:
    """개인 동시기록 — 같은 끼니 기록 안에 메인(meal)과 동반(companion) 군이 함께 있었나.

    김치찌개 기록 5번 중 4번에 쌀밥이 있었으면 share 0.8 → 기본 동반 대신 이걸 쓴다.
    5번 중 1번뿐이면 share 0.2 → "밥 없이 먹는 사람" → 동반을 붙이지 않는다.
    군이 없는 DB 에서는 빈 dict (엔진은 기본 동반도 없으므로 동반 표기가 꺼진다).
    """
    index = index or load_group_index(db)
    if not index.enabled:
        return {}
    rows = _eaten_rows(db, since=now - timedelta(days=window_days), index=index, user_id=user_id)
    by_record: dict[int, list[_Row]] = defaultdict(list)
    for row in rows:
        by_record[row.record_id].append(row)

    meals_of: Counter = Counter()
    with_companion: Counter = Counter()
    companion_votes: dict[str, Counter] = defaultdict(Counter)
    for items in by_record.values():
        mains = {_key_of(r) for r in items if r.group and r.group.role == ROLE_MEAL}
        comps = {_key_of(r) for r in items if r.group and r.group.role == ROLE_COMPANION}
        for m in mains:
            meals_of[m] += 1
            if comps:
                with_companion[m] += 1
                for c in comps:
                    companion_votes[m][c] += 1

    out: dict[str, CompanionStat] = {}
    for m, n in meals_of.items():
        share = with_companion[m] / n
        best = companion_votes[m].most_common(1)[0][0] if companion_votes[m] else None
        out[m] = CompanionStat(companion_key=best if share >= COMPANION_MIN_SHARE else None,
                               share=round(share, 2), meals=n)
    return out


@dataclass
class Budget:
    meal_type: str
    goal_calories: int
    ratio: float
    ratio_source: str  # personal | default
    ratio_budget: int  # 끼니 비율 × 하루 목표
    remaining_today: int  # 하루 목표 − 오늘 섭취
    meal_budget: int  # 최종 예산 (mood 반영)
    protein_gap: float  # 하루 단백질 목표 − 오늘 섭취 (양수면 부족)
    mood: str


def _meal_ratios(db: Session, user_id: int, *, now: datetime) -> tuple[dict[str, float], str]:
    since = now - timedelta(days=RATIO_WINDOW_DAYS)
    rows = db.execute(
        select(MealRecord.meal_type, MealRecord.total_calories).where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= since,
        )
    ).all()
    total = sum(float(cal) for _, cal in rows)
    if len(rows) < MIN_RATIO_RECORDS or total <= 0:
        return dict(DEFAULT_MEAL_RATIOS), "default"
    by_type: dict[str, float] = defaultdict(float)
    for meal_type, cal in rows:
        by_type[meal_type] += float(cal)
    # 개인 비율과 기본 비율을 반씩 섞고 끼니별 하한을 적용한다 (극단값 방지)
    blended = {}
    for mt in MEAL_TYPES:
        personal = by_type.get(mt, 0.0) / total
        mixed = PERSONAL_RATIO_WEIGHT * personal + (1 - PERSONAL_RATIO_WEIGHT) * DEFAULT_MEAL_RATIOS[mt]
        blended[mt] = min(max(mixed, MIN_RATIO_BY_MEAL[mt]), MAX_RATIO_BY_MEAL[mt])
    return blended, "personal"


def meal_budget(
    db: Session,
    user_id: int,
    meal_type: str,
    *,
    now: datetime,
    day_start_hour: int,
    mood: str = "any",
) -> Budget:
    """이번 끼니에 쓸 칼로리 예산.

    사용자의 최근 2주 끼니별 비율 × 하루 목표가 기본 예산이고, 오늘 남은 칼로리가
    그보다 적으면 남은 값을 쓴다. 단 남은 값이 예산의 30% 미만이면 30%로 받쳐서
    (이미 목표를 넘겼어도) 가벼운 메뉴가 랭킹될 수 있게 한다. mood 는 예산을 ±20% 조정.
    """
    goals = get_goals(db, user_id)
    ratios, ratio_source = _meal_ratios(db, user_id, now=now)
    day = kst_date_of(now, day_start_hour)
    today = aggregate_day(db, user_id, day, day_start_hour)

    goal = int(goals["calories"])
    ratio = ratios.get(meal_type, DEFAULT_MEAL_RATIOS["snack"])
    ratio_budget = round(goal * ratio)
    remaining = round(goal - today["calories"])
    base = ratio_budget if remaining >= ratio_budget else max(remaining, round(ratio_budget * BUDGET_FLOOR))
    budget = round(base * MOOD_FACTOR.get(mood, 1.0))
    return Budget(
        meal_type=meal_type,
        goal_calories=goal,
        ratio=round(ratio, 4),
        ratio_source=ratio_source,
        ratio_budget=ratio_budget,
        remaining_today=remaining,
        meal_budget=budget,
        protein_gap=round(float(goals["protein"]) - today["protein"], 1),
        mood=mood,
    )


def budget_label(calories: float, budget: int) -> str:
    """예산 대비 라벨 — fit(±20%) / light / heavy."""
    if budget <= 0:
        return "heavy"
    ratio = calories / budget
    if ratio < 1 - FIT_TOLERANCE:
        return "light"
    if ratio > 1 + FIT_TOLERANCE:
        return "heavy"
    return "fit"
