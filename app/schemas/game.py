"""게이미피케이션 스키마 (핸드오프 §7 응답 계약).

필드 **추가**는 자유롭게 하되 기존 필드 이름을 조용히 바꾸지 않는다 —
FE mock fixture 와 구버전 앱이 같은 이름을 읽고 있다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SlotType = Literal["pet", "background", "food", "blaster", "event_prop"]


# --- 공통 조각 ---

class GameProfileBrief(BaseModel):
    points: int
    xp: int
    level: int
    # 다음 레벨까지 필요한 XP (레벨 곡선은 서버에만 둔다)
    xp_to_next_level: int
    current_streak: int
    best_streak: int
    total_record_days: int
    logical_date: str


class ActivePet(BaseModel):
    item_code: str
    display_name: str
    name: str
    preview_key: str
    bond_level: int
    bond_level_name: str
    bond_days: int
    # 다음 유대 레벨에 필요한 '누적' 함께한 날. 최고 단계면 None
    next_level_days: int | None = None
    next_reward: str | None = None
    growth_layers: list[str] = Field(default_factory=list)


class EquippedSkill(BaseModel):
    code: str
    name: str
    charges: int
    max_charges: int
    # 서버 판정이 아직 없는 스킬(발견 돋보기·오늘의 바꾸기)은 false
    is_active: bool


class StagePlacementOut(BaseModel):
    slot_type: str
    slot_index: int
    item_code: str
    preview_key: str
    category: str
    transform: dict | None = None


class StageOut(BaseModel):
    revision: int
    placements: list[StagePlacementOut] = Field(default_factory=list)


class NextUnlock(BaseModel):
    item_code: str
    current: int
    target: int
    label: str


class ClaimableReward(BaseModel):
    """홈에서 '받기'를 누를 수 있는 것 (§6.3).

    1.8.0 은 `claimable` 을 **읽고 쓰지 않으므로**(mock 에만 등장) list[str] 에서
    객체 배열로 바꿔도 안전하다.
    """

    # mission | event
    type: Literal["mission", "event"]
    code: str
    label: str
    # 미션은 {"xp":15,"points":15}, 이벤트는 {"item_code":"event_chest","stamp":3}
    reward: dict = Field(default_factory=dict)


class FirstFriendOffer(BaseModel):
    """3일차 무료 펫 선택권. 만료되지 않는다."""

    available: bool
    required_days: int
    current_days: int
    choices: list[str] = Field(default_factory=list)


# --- GET /v1/game/home ---

class GameHomeResponse(BaseModel):
    profile: GameProfileBrief
    active_pet: ActivePet | None = None
    equipped_skill: EquippedSkill | None = None
    stage: StageOut
    next_unlock: NextUnlock | None = None
    first_friend: FirstFriendOffer
    claimable: list[ClaimableReward] = Field(default_factory=list)


# --- GET /v1/game/collection · /v1/game/shop ---

class CollectionItem(BaseModel):
    code: str
    name: str
    category: str
    preview_key: str
    owned: bool
    active: bool
    price: int | None = None
    unlock: dict
    # 펫만: 이 펫과 쌓은 유대
    bond_days: int | None = None
    bond_level: int | None = None
    # 펫만: 유대 Lv.3 에서 배우는 지원 스킬
    signature_skill: str | None = None
    signature_skill_name: str | None = None
    # 구매 불가 사유 (LOCKED_LEVEL / INSUFFICIENT_POINTS / NOT_FOR_SALE)
    locked_reason: str | None = None
    # 음식 해금 진행도 {"current":3,"target":5,"unit":"day"|"menu"}.
    # 이미 보유했거나 진행도 개념이 없는 아이템은 None (구버전 앱은 무시한다)
    progress: dict | None = None


class CollectionResponse(BaseModel):
    slot_limits: dict[str, int]
    points: int
    items: list[CollectionItem]


class ShopResponse(BaseModel):
    points: int
    items: list[CollectionItem]


# --- GET /v1/game/pets/{code} ---

class GrowthStep(BaseModel):
    level: int
    days: int
    name: str
    layer: str
    layer_label: str
    reward: str
    reached: bool


class PetDetailResponse(BaseModel):
    item_code: str
    name: str
    display_name: str
    preview_key: str
    owned: bool
    active: bool
    bond_days: int
    bond_level: int
    bond_level_name: str
    next_level_days: int | None = None
    growth: list[GrowthStep]
    signature_skill: str | None = None
    signature_skill_name: str | None = None
    price: int | None = None
    unlock: dict


class PetNicknameRequest(BaseModel):
    nickname: str | None = Field(default=None, max_length=20)


# --- PUT /v1/game/stage ---

class StagePlacementInput(BaseModel):
    slot_type: SlotType
    slot_index: int = Field(ge=0, le=9)
    item_code: str = Field(min_length=1, max_length=40)
    # 0~1 정규화 좌표. 기기 픽셀을 저장하지 않는다
    transform: dict | None = None


class StageSaveRequest(BaseModel):
    revision: int = Field(ge=0)
    placements: list[StagePlacementInput] = Field(default_factory=list, max_length=20)


# --- POST /v1/game/shop/purchase ---

class PurchaseRequest(BaseModel):
    item_code: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=64)


class PurchaseResponse(BaseModel):
    item_code: str
    points: int
    already_purchased: bool = False


# --- POST /v1/game/adoption/first-choice ---

class FirstFriendRequest(BaseModel):
    item_code: str = Field(min_length=1, max_length=40)


class FirstFriendResponse(BaseModel):
    item_code: str
    already_claimed: bool = False


# --- PUT /v1/game/skills/equipped ---

class SkillOut(BaseModel):
    code: str
    name: str
    description: str
    source_pet_code: str
    unlock_bond_level: int
    charges: int
    max_charges: int
    is_active: bool
    equipped: bool


class SkillsResponse(BaseModel):
    equipped_skill_code: str | None = None
    skills: list[SkillOut]


class EquipSkillRequest(BaseModel):
    # None = 해제
    skill_code: str | None = Field(default=None, max_length=40)


# --- 식단 저장 응답에 실리는 보상 (핸드오프 §6 PR3) ---

class PetGrowthReward(BaseModel):
    pet_code: str
    bond_day_added: bool
    bond_days: int
    bond_level_before: int
    bond_level_after: int
    unlocked_growth_layers: list[str] = Field(default_factory=list)
    unlocked_skill: str | None = None


class LevelUpReward(BaseModel):
    level_before: int
    level_after: int
    points: int


class MealRewards(BaseModel):
    xp: int
    points: int
    current_streak: int
    level: int
    total_points: int
    level_up: LevelUpReward | None = None
    unlocked_items: list[str] = Field(default_factory=list)
    progress_updates: list[dict] = Field(default_factory=list)
    pet_growth: PetGrowthReward | None = None
    skill_effect: dict | None = None


# --- GET /v1/game/missions ---

class MissionReward(BaseModel):
    xp: int
    points: int


class MissionOut(BaseModel):
    code: str
    title: str
    description: str
    # daily | weekly
    scope: str
    tier: int
    target: int
    progress: int
    completed: bool
    claimed: bool
    reward: MissionReward
    # 일일 = 논리 날짜, 주간 = 그 주 월요일의 논리 날짜
    period_key: str


class MissionsResponse(BaseModel):
    missions: list[MissionOut] = Field(default_factory=list)
    # '오늘의 바꾸기'를 지금 쓸 수 있는가 (스킬 장착 + 오늘 미사용 + 시작 전 미션 존재)
    swap_available: bool = False
    swap_skill_equipped: bool = False


class MissionClaimResponse(BaseModel):
    code: str
    period_key: str
    reward: MissionReward
    # 수령 후 잔액
    points: int
    level: int
    level_up: LevelUpReward | None = None


# --- GET /v1/game/events ---

class EventRewardOut(BaseModel):
    # stamp 규칙은 stamp, mission_count 규칙은 count 에 임계값이 담긴다
    stamp: int | None = None
    count: int | None = None
    item_code: str
    preview_key: str | None = None
    name: str
    reached: bool
    claimed: bool


class EventOut(BaseModel):
    code: str
    name: str
    description: str
    # stamp | mission_count
    rule: str
    progress: int
    target: int
    starts_on: str
    ends_on: str
    rewards: list[EventRewardOut] = Field(default_factory=list)


class EventsResponse(BaseModel):
    events: list[EventOut] = Field(default_factory=list)


class EventClaimRequest(BaseModel):
    """스탬프 이벤트는 {"stamp": 3}, 미션 이벤트는 {"count": 7}."""

    stamp: int | None = Field(default=None, ge=1, le=999)
    count: int | None = Field(default=None, ge=1, le=999)

    def threshold(self) -> int | None:
        return self.stamp if self.stamp is not None else self.count


class EventClaimResponse(BaseModel):
    event_code: str
    item_code: str
    name: str
    progress: int
