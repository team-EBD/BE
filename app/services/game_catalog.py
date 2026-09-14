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

# 카테고리별 동시 활성(무대 배치) 한도 — 서버가 강제한다
DEFAULT_SLOT_LIMITS = {"pet": 1, "background": 1, "food": 5, "blaster": 3, "event_prop": 2}

# 기본 지급 세트 (가입/최초 조회 시 idempotent 지급)
DEFAULT_PET_CODE = "pet_cat"
DEFAULT_BACKGROUND_CODE = "bg_sunny_kitchen"

# 아직 서버 판정이 구현되지 않은 스킬 — 해금은 되지만 장착해도 효과가 없다.
# (미션/음식 발견이 들어오는 다음 PR 에서 열린다. 로드맵 v3 §4)
ACTIVE_SKILL_CODES = frozenset({"streak_pause", "daily_xp_nudge"})


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
