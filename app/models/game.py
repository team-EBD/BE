"""게이미피케이션 도메인 모델 (로드맵 v3 — 함께 크는 펫).

설계 원칙 (게이미피케이션-개발-로드맵-v3.md / HANDOFF-GAMIFICATION.md):

- XP·코인·유대는 **절대 회수하지 않는다**. 기록을 지워도 레벨/성장은 유지된다.
- 모든 지급은 `reward_ledger(user_id, idempotency_key)` 유니크로 멱등 처리한다.
  보유 포인트(`game_profiles.points`)는 항상 원장과 같은 트랜잭션에서 갱신한다.
- '하루'의 경계는 KST 06:00 (settings.day_start_hour) — FE/BE 공통.
- UI 표기는 `잎 코인`, DB/API 필드명은 `points` 로 통일한다.
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import (
    TZDateTime,
    created_at_column,
    pk_column,
    updated_at_column,
)

# 카탈로그 카테고리 — DB enum 대신 문자열 + 서비스/Pydantic 검증 (운영 중 추가가 쉽다)
ITEM_CATEGORIES = ("pet", "background", "food", "blaster", "event_prop")

# 획득 경로 — 기본 지급 / 첫 친구 무료 선택 / 코인 구매 / 스트릭 / 이벤트
ITEM_SOURCES = (
    "default", "first_friend", "purchase", "streak", "food", "event", "backfill"
)


class GameProfile(Base):
    """사용자당 1행. 계정 단위의 레벨·코인·스트릭·활성 펫·장착 스킬."""

    __tablename__ = "game_profiles"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    best_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 마지막으로 유효 기록을 남긴 논리 날짜 (스트릭·일일 보너스 판정 기준)
    last_recorded_logical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 서로 다른 기록일 누계 — 첫 친구 선택권(3일)·스킬 충전(14일) 판정에 쓴다.
    # 펫별 유대 일수와 달리 펫을 바꿔도 이어진다.
    total_record_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 현재 무대에 세운 펫. stage_placements 와 중복이지만 보상 귀속 시
    # 매번 배치를 조회하지 않도록 캐시한다 (갱신은 서비스 한 곳에서만).
    active_pet_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 장착 스킬의 단일 진실 원천 (UserSkill 에 중복 저장하지 않는다 — v3 §7)
    equipped_skill_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 3일차 첫 친구 무료 선택권 사용 시각 (NULL = 아직 사용 안 함, 만료 없음)
    first_friend_claimed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # 무대 배치 낙관적 잠금 — PUT /game/stage 가 마지막 쓰기를 조용히 덮어쓰지 않게
    stage_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class CatalogItem(Base):
    """상점/컬렉션 상품 카탈로그. seed/gamification_catalog_v2.json 에서 시드한다."""

    __tablename__ = "catalog_items"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 플랫폼 독립 키 — 파일 경로가 아니라 FE 정적 asset map 의 키다 (핸드오프 §8)
    asset_key: Mapped[str] = mapped_column(String(60), nullable=False)
    preview_key: Mapped[str] = mapped_column(String(60), nullable=False)
    # 해금 조건 원본 ({"type":"coin","price":120} / {"type":"streak","days":7} ...)
    unlock: Mapped[dict] = mapped_column(JSON, nullable=False)
    # unlock.price 의 역정규화 — 상점 정렬/필터용. 비매품은 NULL
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserItem(Base):
    """보유 아이템. (user_id, catalog_item_id) 유일 — 중복 지급 방지."""

    __tablename__ = "user_items"
    __table_args__ = (
        UniqueConstraint("user_id", "catalog_item_id", name="uq_user_items_user_item"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    acquired_at: Mapped[datetime] = created_at_column()


class StagePlacement(Base):
    """홈 무대 배치. (user_id, slot_type, slot_index) 유일."""

    __tablename__ = "stage_placements"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "slot_type", "slot_index", name="uq_stage_placements_slot"
        ),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_type: Mapped[str] = mapped_column(String(20), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False
    )
    # 기기 픽셀이 아니라 0~1 정규화 좌표 {"x":0.5,"y":0.6,"scale":1}
    transform: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UnlockProgress(Base):
    """조건부 해금(음식 발견 등)의 진행도. (user_id, catalog_item_id) 유일.

    스트릭 기반 해금은 game_profiles.current_streak 로 즉시 판정하므로 쓰지 않는다.
    음식 태그 해금(seed/game_food_tags.json)만 이 테이블을 쓴다 — 같은 날/같은 메뉴를
    두 번 세지 않도록 증가는 reward_ledger(`food-progress:...`) 로 멱등 처리하고,
    여기에는 누계와 마지막으로 센 논리 날짜만 남긴다.
    """

    __tablename__ = "unlock_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "catalog_item_id", name="uq_unlock_progress_user_item"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("catalog_items.id", ondelete="CASCADE"), nullable=False
    )
    current_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    target_value: Mapped[int] = mapped_column(Integer, nullable=False)
    last_counted_logical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class RewardLedger(Base):
    """XP·코인 증감 원장. (user_id, idempotency_key) 유일 = 멱등 지급의 근거.

    key 예: `meal:123` / `daily-complete:2026-09-13` / `level:8` / `purchase:<uuid>`
    """

    __tablename__ = "reward_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_reward_ledger_user_key"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    xp_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    points_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 지급이 귀속된 논리 날짜 — 일일 지급 상한(하루 4건)을 서버 시각이 아니라
    # 기록의 날짜로 세기 위해 둔다. 구매처럼 날짜 개념이 없으면 NULL.
    logical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class PetBond(Base):
    """펫별 '함께한 날'. (user_id, pet_code) 유일. 미접속으로 감소하지 않는다."""

    __tablename__ = "pet_bonds"
    __table_args__ = (
        UniqueConstraint("user_id", "pet_code", name="uq_pet_bonds_user_pet"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pet_code: Mapped[str] = mapped_column(String(40), nullable=False)
    # 사용자가 지어 준 이름 (NULL = 카탈로그 기본 이름). 유대 Lv.1 에서 노출한다.
    nickname: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bond_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # bond_days 로 계산 가능하지만, 레벨업 감지를 한 곳(services/game_rewards)에서만
    # 하도록 저장한다. 불일치 검증 테스트를 둔다.
    bond_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_counted_logical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 사용자가 표시하기로 고른 성장 외형 레이어 (NULL = 획득한 최고 단계)
    selected_growth_style: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserSkill(Base):
    """유대 Lv.3 에서 계정에 귀속된 지원 스킬. (user_id, skill_code) 유일.

    장착 여부는 여기 두지 않는다 — GameProfile.equipped_skill_code 가 단일 원천.
    """

    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_code", name="uq_user_skills_user_skill"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_code: Mapped[str] = mapped_column(String(40), nullable=False)
    source_pet_code: Mapped[str] = mapped_column(String(40), nullable=False)
    unlocked_at: Mapped[datetime] = created_at_column()
    charge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 다음 충전이 이뤄지는 total_record_days 기준값 (NULL = 이미 만충)
    recharge_at_record_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = updated_at_column()


class SkillUsageLedger(Base):
    """스킬 발동 기록. (user_id, idempotency_key) 유일 — 자동 발동의 중복을 막는다.

    key 예: `xp-nudge:2026-09-13` / `streak-pause:2026-09-13`
    """

    __tablename__ = "skill_usage_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_skill_usage_user_key"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_code: Mapped[str] = mapped_column(String(40), nullable=False)
    logical_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class GameMission(Base):
    """미션 카탈로그. seed/game_missions.json 에서 시드한다 (code 가 불변 키).

    수치 조정을 코드 배포 없이 하기 위해 시드가 단일 원천이고, 이 테이블은 그 사본이다
    (catalog_items 와 같은 패턴).
    """

    __tablename__ = "game_missions"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # daily | weekly
    scope: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # 일일 미션은 티어 1/2/3 에서 각 1개씩 뽑는다 (주간은 1)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # meals_recorded / photo_meals / meal_slot / new_menu / food_tag / distinct_menus / record_days
    rule: Mapped[str] = mapped_column(String(30), nullable=False)
    # 규칙 인자 ({"meal_type":"breakfast"} / {"food_tag":"vegetable"})
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    target_value: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reward_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserMission(Base):
    """출제된 미션 1건과 그 진행도. (user_id, mission_code, period_key) 유일.

    `period_key` 는 일일이면 논리 날짜, 주간이면 그 주 **월요일**의 논리 날짜다.
    진행도 증가는 reward_ledger(`mission-progress:...`) 멱등 키로 막고, 여기에는
    누계만 남긴다 — 과거 날짜를 나중에 기록해도 두 번 세지 않게 하기 위해서다.
    수령은 수동이며 지급 역시 기존 reward_ledger(`mission:<code>:<period_key>`)를 쓴다.
    """

    __tablename__ = "user_missions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "mission_code", "period_key", name="uq_user_missions_user_code_period"
        ),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mission_code: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    period_key: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    target_value: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class GameEvent(Base):
    """이벤트 카탈로그. seed/game_events.json 에서 시드한다.

    **기간은 시드가 단일 원천**이다 — 코드 배포 없이 켜고 끈다.
    """

    __tablename__ = "game_events"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # stamp(기록한 서로 다른 논리 날짜 수) | mission_count(완료한 미션 수)
    rule: Mapped[str] = mapped_column(String(20), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    # [{"stamp":3,"item_code":"event_chest"}, ...] — 임계값 오름차순
    rewards: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class UserEvent(Base):
    """이벤트 진행도. (user_id, event_code) 유일.

    증가는 reward_ledger(`event-progress:...`) 멱등 키로 막고 `last_counted_logical_date`
    를 함께 갱신한다 (unlock_progress 와 같은 패턴). 수령한 임계값은 `claimed_thresholds`
    JSON 배열에 남기지만, 실제 중복 방지는 원장 키 `event:<code>:stamp:<n>` 가 한다.
    """

    __tablename__ = "user_events"
    __table_args__ = (
        UniqueConstraint("user_id", "event_code", name="uq_user_events_user_event"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_code: Mapped[str] = mapped_column(String(40), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_counted_logical_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    claimed_thresholds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()
