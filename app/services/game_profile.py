"""게임 프로필 보장(ensure)과 카탈로그 시드.

핵심 원칙:

- 가입 플로우를 건드리지 않고 **최초 조회 시 지연 생성(lazy ensure)** 한다.
  신규/기존 사용자가 같은 코드 경로를 타므로 별도 백필 배치가 필요 없고,
  동시에 여러 요청이 들어와도 unique 제약 + savepoint 재시도로 중복되지 않는다.
- 기존 사용자에게는 최근 30일 기록일 중 **최대 7일**만 고양이 유대로 백필한다
  (로드맵 v3 §9 — 바로 Lv.5 가 되어 성장 콘텐츠를 소진하지 않도록).
- 소급 코인은 지급하지 않는다.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import kst_date_of, now_utc
from app.models import (
    CatalogItem,
    GameProfile,
    MealRecord,
    PetBond,
    StagePlacement,
    User,
    UserItem,
)
from app.services import game_catalog as catalog


def logical_today() -> date:
    """지금 이 순간이 속한 논리 날짜 (KST 06:00 경계)."""
    return kst_date_of(now_utc(), settings.day_start_hour)


# --- 카탈로그 ---

def ensure_catalog(db: Session) -> dict[str, CatalogItem]:
    """기획 JSON 의 상품을 catalog_items 에 idempotent 하게 반영한다.

    이름/가격/노출은 기획 변경을 따라가고, code 는 불변 키다. 이미 지급된
    아이템은 가격이 바뀌어도 회수하지 않는다 (로드맵 v3 §9).
    """
    rows = {row.code: row for row in db.scalars(select(CatalogItem))}
    dirty = False
    for entry in catalog.entries():
        row = rows.get(entry.code)
        if row is None:
            row = CatalogItem(
                code=entry.code,
                name=entry.name,
                category=entry.category,
                asset_key=entry.asset_key,
                preview_key=entry.preview_key,
                unlock=entry.unlock,
                price=entry.price,
                is_visible=True,
                sort_order=entry.sort_order,
            )
            db.add(row)
            rows[entry.code] = row
            dirty = True
        elif (
            row.name != entry.name
            or row.price != entry.price
            or row.unlock != entry.unlock
            or row.sort_order != entry.sort_order
        ):
            row.name = entry.name
            row.price = entry.price
            row.unlock = entry.unlock
            row.sort_order = entry.sort_order
            dirty = True
    if dirty:
        db.flush()
    return rows


# --- 최근 기록일 (백필·스트릭 계산) ---

def recent_record_days(db: Session, user_id: int, lookback_days: int) -> list[date]:
    """최근 N일 안의 '유효 기록이 있는 논리 날짜' 목록 (최신순, 중복 제거)."""
    since = now_utc() - timedelta(days=lookback_days + 1)
    eaten_ats = db.scalars(
        select(MealRecord.eaten_at)
        .where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= since,
        )
        .order_by(MealRecord.eaten_at.desc())
    ).all()
    seen: list[date] = []
    for eaten_at in eaten_ats:
        day = kst_date_of(eaten_at, settings.day_start_hour)
        if day not in seen:
            seen.append(day)
    return seen


def _streak_from_days(days: list[date], today: date) -> int:
    """오늘 또는 어제부터 연속으로 이어진 기록일 수 (FE utils/streak.js 와 같은 규칙)."""
    day_set = set(days)
    cursor = today if today in day_set else today - timedelta(days=1)
    streak = 0
    while cursor in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# --- 프로필 보장 ---

def _forget(db: Session, obj) -> None:
    """savepoint 롤백으로 이미 빠졌을 수도 있는 객체를 안전하게 세션에서 뗀다."""
    if obj in db:
        db.expunge(obj)


def _grant_item(db: Session, user_id: int, item: CatalogItem, source: str) -> bool:
    """보유 아이템 지급. 이미 있으면 False (멱등)."""
    exists = db.scalar(
        select(UserItem.id).where(
            UserItem.user_id == user_id, UserItem.catalog_item_id == item.id
        )
    )
    if exists is not None:
        return False
    db.add(UserItem(user_id=user_id, catalog_item_id=item.id, source=source))
    db.flush()
    return True


def _place(db: Session, user_id: int, slot_type: str, slot_index: int, item: CatalogItem) -> None:
    exists = db.scalar(
        select(StagePlacement.id).where(
            StagePlacement.user_id == user_id,
            StagePlacement.slot_type == slot_type,
            StagePlacement.slot_index == slot_index,
        )
    )
    if exists is not None:
        return
    db.add(
        StagePlacement(
            user_id=user_id,
            slot_type=slot_type,
            slot_index=slot_index,
            catalog_item_id=item.id,
            transform=None,
        )
    )
    db.flush()


def _create_profile(db: Session, user_id: int) -> GameProfile:
    """동시 요청에서도 한 행만 남도록 savepoint 안에서 생성한다."""
    profile = GameProfile(user_id=user_id, active_pet_code=catalog.DEFAULT_PET_CODE)
    try:
        with db.begin_nested():
            db.add(profile)
            db.flush()
    except IntegrityError:
        _forget(db, profile)
        existing = db.scalar(select(GameProfile).where(GameProfile.user_id == user_id))
        if existing is None:  # pragma: no cover — unique 위반이면 반드시 존재한다
            raise
        return existing
    return profile


def ensure_pet_bond(db: Session, user_id: int, pet_code: str) -> PetBond:
    bond = db.scalar(
        select(PetBond).where(PetBond.user_id == user_id, PetBond.pet_code == pet_code)
    )
    if bond is not None:
        return bond
    bond = PetBond(user_id=user_id, pet_code=pet_code, bond_days=0, bond_level=1)
    try:
        with db.begin_nested():
            db.add(bond)
            db.flush()
    except IntegrityError:
        _forget(db, bond)
        return db.scalar(
            select(PetBond).where(PetBond.user_id == user_id, PetBond.pet_code == pet_code)
        )
    return bond


def ensure_game_profile(db: Session, user: User) -> GameProfile:
    """게임 프로필 + 기본 지급 + 기본 배치를 보장한다. 여러 번 호출해도 안전하다.

    커밋하지 않는다 — 호출한 쪽이 트랜잭션 경계를 정한다.
    """
    items = ensure_catalog(db)
    profile = db.scalar(select(GameProfile).where(GameProfile.user_id == user.id))
    is_new = profile is None
    if profile is None:
        profile = _create_profile(db, user.id)

    pet = items[catalog.DEFAULT_PET_CODE]
    background = items[catalog.DEFAULT_BACKGROUND_CODE]
    _grant_item(db, user.id, pet, "default")
    _grant_item(db, user.id, background, "default")
    _place(db, user.id, "pet", 0, pet)
    _place(db, user.id, "background", 0, background)

    if profile.active_pet_code is None:
        profile.active_pet_code = catalog.DEFAULT_PET_CODE
    bond = ensure_pet_bond(db, user.id, profile.active_pet_code)

    if is_new:
        _backfill_existing_user(db, profile, bond)
    return profile


def _backfill_existing_user(db: Session, profile: GameProfile, bond: PetBond) -> None:
    """출시 전부터 쓰던 사용자의 기록을 유대·스트릭에 한 번만 반영한다.

    - 최근 30일의 서로 다른 기록일 중 최대 7일을 고양이 유대로 준다.
    - 스트릭은 실제 기록에서 다시 계산한다 (홈 스트릭 배지와 숫자를 맞추기 위해).
    - 코인·XP 는 소급 지급하지 않는다 (로드맵 v3 §9).
    """
    policy = catalog.backfill_policy()
    lookback = int(policy.get("lookback_days") or 30)
    max_bond = int(policy.get("max_bond_days") or 7)

    days = recent_record_days(db, profile.user_id, lookback)
    if not days:
        return

    today = logical_today()
    profile.total_record_days = len(days)
    profile.last_recorded_logical_date = days[0]
    profile.current_streak = _streak_from_days(days, today)
    profile.best_streak = profile.current_streak

    bond.bond_days = min(len(days), max_bond)
    bond.bond_level = catalog.bond_level_for(bond.bond_days)
    bond.last_counted_logical_date = days[0]
    db.flush()


# --- 조회 헬퍼 ---

def owned_items(db: Session, user_id: int) -> dict[str, UserItem]:
    """code → UserItem."""
    rows = db.execute(
        select(CatalogItem.code, UserItem)
        .join(UserItem, UserItem.catalog_item_id == CatalogItem.id)
        .where(UserItem.user_id == user_id)
    ).all()
    return {code: item for code, item in rows}


def stage_placements(db: Session, user_id: int) -> list[tuple[StagePlacement, str]]:
    """(배치, 아이템 코드) 목록 — 슬롯 순서."""
    rows = db.execute(
        select(StagePlacement, CatalogItem.code)
        .join(CatalogItem, CatalogItem.id == StagePlacement.catalog_item_id)
        .where(StagePlacement.user_id == user_id)
        .order_by(StagePlacement.slot_type, StagePlacement.slot_index)
    ).all()
    return [(placement, code) for placement, code in rows]
