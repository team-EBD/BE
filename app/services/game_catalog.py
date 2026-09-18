"""게이미피케이션 카탈로그 — 기획 JSON 을 서버 상수로 읽어 들인다.

`seed/gamification_catalog_v2.json`(기본 상품 34종)에 `seed/gamification_growth_v3.json`
(v3 펫 가격·성장 단계·지원 스킬)을 덮어써 하나의 카탈로그로 만든다. 기획 문서를
단일 원천으로 두기 위해 루트의 두 파일을 그대로 복사해 쓰고, 여기서 병합한다.

레벨 곡선·XP 표처럼 **계산식은 서버에만** 둔다 (로드맵 v1 §2.1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
_CATALOG_FILE = _SEED_DIR / "gamification_catalog_v2.json"
_GROWTH_FILE = _SEED_DIR / "gamification_growth_v3.json"
# curated `nutrition_item_id → 음식 태그` 매핑. NutritionItem.category 는 한식/분식 같은
# **요리 장르**라 여기서 vegetable/fruit/fish/soup 를 유도할 수 없어 손으로 태깅한다.
_FOOD_TAGS_FILE = _SEED_DIR / "game_food_tags.json"
# 미션·이벤트 카탈로그. 수치와 기간을 코드 배포 없이 조정하려고 시드를 단일 원천으로 둔다.
_MISSIONS_FILE = _SEED_DIR / "game_missions.json"
_EVENTS_FILE = _SEED_DIR / "game_events.json"

# 카테고리별 동시 활성(무대 배치) 한도 — 서버가 강제한다
DEFAULT_SLOT_LIMITS = {"pet": 1, "background": 1, "food": 5, "blaster": 3, "event_prop": 2}

# 기본 지급 세트 (가입/최초 조회 시 idempotent 지급)
DEFAULT_PET_CODE = "pet_cat"
DEFAULT_BACKGROUND_CODE = "bg_sunny_kitchen"

# 서버 판정이 구현된 스킬 — 장착하면 실제로 동작한다.
# (발견 돋보기 `food_clarifier` 는 아직 판정이 없어 빠져 있다. 로드맵 v3 §4)
ACTIVE_SKILL_CODES = frozenset({"streak_pause", "daily_xp_nudge", "daily_mission_swap"})

# 음식 해금 태그 어휘 — 카탈로그 unlock.food_tags 와 같은 4종으로 고정한다
FOOD_TAGS = ("vegetable", "fruit", "fish", "soup")
# '다음 목표' 문구용 한국어 라벨. 비판단 말투를 유지한다 (체중/칼로리 언어 금지)
FOOD_TAG_LABELS = {
    "vegetable": "채소",
    "fruit": "과일",
    "fish": "생선",
    "soup": "국물 요리",
}

# 음식 해금 규칙 (카탈로그 unlock.rule)
FOOD_RULE_TAG_DAYS = "distinct_food_days"        # 태그가 든 식사를 기록한 서로 다른 날
FOOD_RULE_MEAL_SLOT_DAYS = "distinct_meal_slot_days"  # 특정 끼니를 기록한 서로 다른 날
FOOD_RULE_MENU_COUNT = "distinct_menu_days"      # 서로 다른 메뉴 가짓수 (날짜가 아니다)


@dataclass(frozen=True)
class CatalogEntry:
    code: str
    name: str
    category: str
    asset_key: str
    preview_key: str
    unlock: dict
    price: int | None
    sort_order: int

    @property
    def min_level(self) -> int:
        return int(self.unlock.get("min_account_level") or self.unlock.get("min_level") or 0)


def _asset_key(item: dict) -> str:
    """파일 경로 대신 FE 정적 asset map 의 키를 만든다 (핸드오프 §8).

    `assets/kenney_cube-pets_1.0/Previews/animal-cat.png` → `animal-cat`
    """
    path = item.get("preview_png") or item.get("asset_png") or item.get("asset_glb") or ""
    return Path(path).stem or item["code"]


@lru_cache(maxsize=1)
def _raw() -> tuple[dict, dict]:
    catalog = json.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
    growth = json.loads(_GROWTH_FILE.read_text(encoding="utf-8"))
    return catalog, growth


@lru_cache(maxsize=1)
def entries() -> tuple[CatalogEntry, ...]:
    """v3 가격 오버라이드까지 반영한 전체 카탈로그 (정렬 순서 고정)."""
    catalog, growth = _raw()
    overrides = {o["code"]: o for o in growth.get("pet_overrides", [])}

    result: list[CatalogEntry] = []
    for order, item in enumerate(catalog["items"]):
        unlock = dict(item["unlock"])
        override = overrides.get(item["code"])
        if override is not None:
            # v3 §2 — 펫 가격 인하와 '첫 친구 무료 선택' 대상 표시
            unlock = dict(override["adoption"])
        price = unlock.get("price")
        result.append(
            CatalogEntry(
                code=item["code"],
                name=item["name"],
                category=item["category"],
                asset_key=_asset_key(item),
                preview_key=_asset_key(item),
                unlock=unlock,
                price=int(price) if price is not None else None,
                sort_order=order,
            )
        )
    return tuple(result)


@lru_cache(maxsize=1)
def by_code() -> dict[str, CatalogEntry]:
    return {e.code: e for e in entries()}


def get(code: str) -> CatalogEntry | None:
    return by_code().get(code)


def food_unlock_entries() -> tuple[CatalogEntry, ...]:
    """`unlock.type == "food"` 인 카탈로그 항목 (해금 진행도의 대상)."""
    return tuple(e for e in entries() if e.unlock.get("type") == "food")


def food_unlock_target(entry: CatalogEntry) -> int:
    """해금에 필요한 값. 규칙에 따라 '일수' 또는 '메뉴 가짓수'다 (키는 둘 다 days)."""
    return int(entry.unlock.get("days") or 0)


def food_unlock_unit(entry: CatalogEntry) -> str:
    """진행도 단위 — `day` 또는 `menu` (CollectionItem.progress.unit)."""
    return "menu" if entry.unlock.get("rule") == FOOD_RULE_MENU_COUNT else "day"


# --- 음식 태그 (curated → 키워드 폴백 2계층) ---

@lru_cache(maxsize=1)
def _food_tag_seed() -> dict:
    return json.loads(_FOOD_TAGS_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def food_tag_map() -> dict[int, frozenset[str]]:
    """nutrition_item_id → curated 태그.

    **빈 집합도 담는다** — "검토했고 태그가 없다"는 명시적 판단이라 키워드 폴백으로
    넘어가면 안 되기 때문이다 (id 가 있는지 여부로 두 계층을 가른다).
    """
    return {
        int(item["nutrition_item_id"]): frozenset(
            t for t in (item.get("tags") or []) if t in FOOD_TAGS
        )
        for item in _food_tag_seed().get("curated", [])
    }


@lru_cache(maxsize=1)
def food_tag_keywords() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """태그 → (include, exclude) 키워드. 시드 파일이 단일 원천이다 (코드에 두지 않는다)."""
    raw = _food_tag_seed().get("keywords") or {}
    table: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for tag in FOOD_TAGS:
        spec = raw.get(tag) or {}
        table[tag] = (
            tuple(spec.get("include") or ()),
            tuple(spec.get("exclude") or ()),
        )
    return table


def food_tags_from_name(name: str | None) -> frozenset[str]:
    """음식 **이름**으로 추정한 태그 (curated 에 없는 항목의 폴백).

    식약처 가공식품 OpenAPI 적재분처럼 curated 46종 밖의 음식이 대부분이라 필요하다.
    `exclude` 가 하나라도 걸리면 그 태그는 붙이지 않는다 — 사과주스·귤차·탕수육·
    유부초밥처럼 부분 문자열만 같은 가공품/동음이의를 막기 위해서다.
    """
    if not name:
        return frozenset()
    text = name.strip()
    if not text:
        return frozenset()
    found = {
        tag
        for tag, (include, exclude) in food_tag_keywords().items()
        if not any(bad in text for bad in exclude)
        and any(good in text for good in include)
    }
    return frozenset(found)


def food_tags_for(nutrition_item_id: int | None, name: str | None = None) -> frozenset[str]:
    """확정된 음식 1건의 태그. curated 우선, 없으면 이름 키워드 폴백.

    자유 입력(nutrition_item_id 가 None)은 애초에 진행도를 올리지 않으므로 빈 집합이다.
    """
    if nutrition_item_id is None:
        return frozenset()
    curated = food_tag_map()
    key = int(nutrition_item_id)
    if key in curated:
        return curated[key]
    return food_tags_from_name(name)


def food_tag_label(tag: str) -> str:
    return FOOD_TAG_LABELS.get(tag, tag)


def slot_limits() -> dict[str, int]:
    catalog, _ = _raw()
    return dict(catalog.get("slot_limits") or DEFAULT_SLOT_LIMITS)


# --- 펫 성장(유대) ---

@lru_cache(maxsize=1)
def bond_levels() -> tuple[tuple[int, int, str], ...]:
    """((level, 누적 함께한 날, 이름), ...) — 오름차순."""
    _, growth = _raw()
    levels = growth["pet_growth"]["levels"]
    return tuple(sorted((int(l["level"]), int(l["days"]), l["name"]) for l in levels))


def bond_level_for(bond_days: int) -> int:
    level = 1
    for lv, days, _name in bond_levels():
        if bond_days >= days:
            level = lv
    return level


def bond_level_name(level: int) -> str:
    for lv, _days, name in bond_levels():
        if lv == level:
            return name
    return ""


def next_bond_level_days(bond_days: int) -> int | None:
    """다음 유대 레벨까지 필요한 '누적' 일수. 최고 단계면 None."""
    for _lv, days, _name in bond_levels():
        if bond_days < days:
            return days
    return None


@lru_cache(maxsize=1)
def pet_overrides() -> dict[str, dict]:
    _, growth = _raw()
    return {o["code"]: o for o in growth.get("pet_overrides", [])}


# 성장 외형 레이어의 한국어 라벨 — '다음 성장 보상' 문구와 펫 상세 타임라인에 쓴다
GROWTH_LAYER_LABELS = {
    "base": "기본 모습",
    "name_tag": "이름표와 배지",
    "green_scarf": "초록 스카프",
    "leaf_steps": "잎 발자국",
    "forest_friend_costume": "숲 친구 의상",
    "yellow_bandana": "노란 반다나",
    "spark_steps": "반짝 발자국",
    "sunny_friend_costume": "햇살 친구 의상",
    "carrot_pin": "당근 핀",
    "flower_steps": "꽃 발자국",
    "garden_friend_costume": "정원 친구 의상",
    "red_neckerchief": "빨간 목도리",
    "ember_steps": "불씨 발자국",
    "sunset_friend_costume": "노을 친구 의상",
    "blue_cap": "파란 모자",
    "snow_steps": "눈 발자국",
    "winter_friend_costume": "겨울 친구 의상",
    "eucalyptus_pin": "유칼립투스 핀",
    "soft_steps": "포근 발자국",
    "grove_friend_costume": "숲속 친구 의상",
    "gold_medal": "황금 메달",
    "sun_steps": "태양 발자국",
    "royal_friend_costume": "황실 의상",
}


def layer_label(layer: str) -> str:
    return GROWTH_LAYER_LABELS.get(layer, layer)


def growth_layers(pet_code: str) -> list[str]:
    return list(pet_overrides().get(pet_code, {}).get("growth_layers") or ["base"])


def unlocked_growth_layers(pet_code: str, bond_level: int) -> list[str]:
    """유대 레벨까지 획득한 외형 레이어. Lv.1 은 base 만."""
    return growth_layers(pet_code)[:max(bond_level, 1)]


def signature_skill(pet_code: str) -> str | None:
    return pet_overrides().get(pet_code, {}).get("signature_skill")


def skill_unlock_bond_level() -> int:
    for skill in skills().values():
        return int(skill.get("unlock_bond_level", 3))
    return 3


# --- 지원 스킬 ---

@lru_cache(maxsize=1)
def skills() -> dict[str, dict]:
    _, growth = _raw()
    return {s["code"]: s for s in growth.get("skills", [])}


# 지원 스킬 설명 — 비판단 말투. 체중/다이어트/보상 심리 자극 문구 금지 (v3 §4)
SKILL_DESCRIPTIONS = {
    "streak_pause": "하루를 놓쳐도 연속 기록을 한 번 지켜 줘요.",
    "daily_xp_nudge": "그날의 첫 기록에 XP를 조금 더 얹어 줘요.",
    "food_clarifier": "헷갈리는 음식 후보를 하나 더 확인할 수 있어요.",
    "daily_mission_swap": "시작하기 전 일일 미션 하나를 바꿀 수 있어요.",
}


def skill_description(code: str) -> str:
    return SKILL_DESCRIPTIONS.get(code, "")


def skill_name(code: str | None) -> str:
    if not code:
        return ""
    return skills().get(code, {}).get("name", code)


def skill_max_charges(code: str) -> int:
    return int(skills().get(code, {}).get("max_charges") or 1)


def skill_recharge_days(code: str) -> int | None:
    days = skills().get(code, {}).get("recharge_distinct_record_days")
    return int(days) if days else None


# --- 첫 친구 선택 ---

@lru_cache(maxsize=1)
def first_friend() -> dict:
    _, growth = _raw()
    return dict(growth.get("first_friend") or {})


def first_friend_choices() -> list[str]:
    return list(first_friend().get("choices") or [])


def first_friend_required_days() -> int:
    return int(first_friend().get("unlock_distinct_record_days") or 3)


# --- 기존 사용자 백필 ---

@lru_cache(maxsize=1)
def backfill_policy() -> dict:
    _, growth = _raw()
    return dict(growth.get("backfill") or {})


# --- 보상 경제 (로드맵 v2.1 §5) ---

# 기록 1건당 XP/코인. 사진 기록은 XP 만 더 준다.
XP_PER_MEAL = 10
XP_PER_PHOTO_MEAL = 15
POINTS_PER_MEAL = 10
# 어뷰징 방어 — 하루 4건까지만 지급 (5번째부터 0. 기록 자체는 정상 저장)
MAX_REWARDED_MEALS_PER_DAY = 4
# 하루 3끼(아침·점심·저녁) 완주 보너스, 일 1회
XP_DAILY_COMPLETE = 20
POINTS_DAILY_COMPLETE = 20
# 장착 스킬 '힘찬 응원' — 그날 첫 유효 기록 XP 가산 (코인에는 영향 없음)
XP_SKILL_NUDGE = 5

# 스트릭은 XP 만 증폭하고 코인은 증폭하지 않는다
_STREAK_XP_MULTIPLIERS = ((30, 1.50), (14, 1.30), (7, 1.15))


def streak_xp_multiplier(streak: int) -> float:
    for days, mult in _STREAK_XP_MULTIPLIERS:
        if streak >= days:
            return mult
    return 1.0


# 레벨 곡선 — 로드맵 v1 §2.1 의 앵커를 선형 보간한 서버 상수 테이블.
# (계산식을 클라이언트에 넣지 않는다)
_LEVEL_ANCHORS = {1: 50, 2: 70, 3: 95, 10: 420, 15: 620, 20: 850, 25: 1100, 50: 2800}
_MAX_TABLE_LEVEL = 50
# 50 레벨 이후 1레벨당 증가분 (25→50 구간 기울기 유지)
_XP_PER_LEVEL_BEYOND_TABLE = 68


@lru_cache(maxsize=1)
def _level_requirements() -> tuple[int, ...]:
    """index i = 레벨 (i+1) → (i+2) 로 올라가는 데 필요한 XP."""
    anchors = sorted(_LEVEL_ANCHORS.items())
    table: list[int] = []
    for level in range(1, _MAX_TABLE_LEVEL + 1):
        if level in _LEVEL_ANCHORS:
            table.append(_LEVEL_ANCHORS[level])
            continue
        lo = max(l for l, _ in anchors if l < level)
        hi = min(l for l, _ in anchors if l > level)
        ratio = (level - lo) / (hi - lo)
        value = _LEVEL_ANCHORS[lo] + (_LEVEL_ANCHORS[hi] - _LEVEL_ANCHORS[lo]) * ratio
        table.append(int(round(value / 5.0) * 5))
    return tuple(table)


def xp_to_next_level(level: int) -> int:
    table = _level_requirements()
    if level <= 0:
        return table[0]
    if level <= len(table):
        return table[level - 1]
    return table[-1] + (level - len(table)) * _XP_PER_LEVEL_BEYOND_TABLE


def apply_xp(level: int, xp: int, gained: int) -> tuple[int, int, int]:
    """(레벨, 현재 레벨 내 XP) 에 XP 를 더한 결과. 반환: (새 레벨, 새 XP, 오른 레벨 수)."""
    level_ups = 0
    xp += gained
    while xp >= xp_to_next_level(level):
        xp -= xp_to_next_level(level)
        level += 1
        level_ups += 1
    return level, xp, level_ups


def level_up_points(level: int) -> int:
    """레벨업 보상 코인 — min(level × 10, 200)."""
    return min(level * 10, 200)


# --- 미션 카탈로그 (seed/game_missions.json) ---

# 판정 규칙 — 전부 논리 날짜(KST 06:00 경계) 기준이고 생략 기록은 세지 않는다
MISSION_RULE_MEALS = "meals_recorded"        # 기간 안에 저장된 식단 건수
MISSION_RULE_PHOTO = "photo_meals"           # 사진으로 만든 식단 건수
MISSION_RULE_MEAL_SLOT = "meal_slot"         # 지정한 meal_type 식단 건수
MISSION_RULE_NEW_MENU = "new_menu"           # 그 사용자가 처음 기록하는 nutrition_item_id
MISSION_RULE_FOOD_TAG = "food_tag"           # 지정 태그가 붙은 음식을 포함한 식단 건수
MISSION_RULE_DISTINCT_MENUS = "distinct_menus"  # 서로 다른 nutrition_item_id 가짓수
MISSION_RULE_RECORD_DAYS = "record_days"     # 기록한 서로 다른 논리 날짜 수

MISSION_SCOPE_DAILY = "daily"
MISSION_SCOPE_WEEKLY = "weekly"


@dataclass(frozen=True)
class MissionSpec:
    code: str
    title: str
    description: str
    scope: str
    tier: int
    rule: str
    params: dict
    target: int
    xp: int
    points: int
    sort_order: int


@lru_cache(maxsize=1)
def _mission_seed() -> dict:
    return json.loads(_MISSIONS_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def mission_specs() -> tuple[MissionSpec, ...]:
    raw = _mission_seed()
    result: list[MissionSpec] = []
    for order, item in enumerate(raw.get("missions", [])):
        reward = item.get("reward") or {}
        result.append(
            MissionSpec(
                code=item["code"],
                title=item["title"],
                description=item.get("description", ""),
                scope=item.get("scope", MISSION_SCOPE_DAILY),
                tier=int(item.get("tier") or 1),
                rule=item["rule"],
                params=dict(item.get("params") or {}),
                target=int(item["target"]),
                xp=int(reward.get("xp") or 0),
                points=int(reward.get("points") or 0),
                sort_order=order,
            )
        )
    return tuple(result)


def mission_by_code() -> dict[str, MissionSpec]:
    return {m.code: m for m in mission_specs()}


def mission_spec(code: str) -> MissionSpec | None:
    return mission_by_code().get(code)


def daily_tiers() -> tuple[int, ...]:
    """일일 미션을 뽑는 티어 목록 (티어당 1개)."""
    raw = _mission_seed().get("daily_tiers")
    if raw:
        return tuple(int(t) for t in raw)
    return tuple(sorted({m.tier for m in mission_specs() if m.scope == MISSION_SCOPE_DAILY}))


def mission_pool(scope: str, tier: int | None = None) -> tuple[MissionSpec, ...]:
    """출제 후보 — 정렬 순서 고정 (결정론적 출제의 전제)."""
    return tuple(
        m
        for m in mission_specs()
        if m.scope == scope and (tier is None or m.tier == tier)
    )


# --- 이벤트 카탈로그 (seed/game_events.json) ---

EVENT_RULE_STAMP = "stamp"                 # 기간 중 기록한 서로 다른 논리 날짜 수
EVENT_RULE_MISSION_COUNT = "mission_count" # 기간 중 완료한 미션 수


@dataclass(frozen=True)
class EventSpec:
    code: str
    name: str
    description: str
    rule: str
    starts_on: str
    ends_on: str
    rewards: tuple[dict, ...]
    sort_order: int

    def threshold_key(self) -> str:
        """보상 임계값이 담긴 키 — stamp 규칙은 `stamp`, 미션 규칙은 `count`."""
        return "stamp" if self.rule == EVENT_RULE_STAMP else "count"


@lru_cache(maxsize=1)
def _event_seed() -> dict:
    return json.loads(_EVENTS_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def event_specs() -> tuple[EventSpec, ...]:
    raw = _event_seed()
    result: list[EventSpec] = []
    for order, item in enumerate(raw.get("events", [])):
        result.append(
            EventSpec(
                code=item["code"],
                name=item["name"],
                description=item.get("description", ""),
                rule=item.get("rule", EVENT_RULE_STAMP),
                starts_on=item["starts_on"],
                ends_on=item["ends_on"],
                rewards=tuple(dict(r) for r in item.get("rewards") or ()),
                sort_order=order,
            )
        )
    return tuple(result)


def event_spec(code: str) -> EventSpec | None:
    return {e.code: e for e in event_specs()}.get(code)


def event_thresholds(spec: EventSpec) -> list[int]:
    key = spec.threshold_key()
    return sorted(int(r[key]) for r in spec.rewards if r.get(key) is not None)
