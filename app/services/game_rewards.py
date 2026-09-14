"""식단 기록 → 보상 지급 (XP·잎 코인·스트릭·펫 유대·해금).

이 모듈은 **커밋하지 않는다**. 식단 저장과 보상은 한 트랜잭션이어야 하므로
경계는 services/meals.create_meal_with_rewards 가 잡는다. 보상 계산이 실패하면
식단도 함께 롤백된다.

멱등성은 전부 `reward_ledger(user_id, idempotency_key)` 유니크로 보장한다.
같은 meal_id 로 두 번 요청해도 잔액은 한 번만 변한다.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import kst_date_of, kst_day_bounds, now_utc
from app.models import (
    CatalogItem,
    GameProfile,
    MealRecord,
    PetBond,
    RewardLedger,
    SkillUsageLedger,
    User,
    UserItem,
    UserSkill,
)
from app.services import game_catalog as catalog
from app.services.game_profile import (
    ensure_catalog,
    ensure_game_profile,
    ensure_pet_bond,
    logical_today,
    recent_record_days,
)

# 스트릭 재계산에 쓰는 조회 범위 — '쉼표 지킴이' 보호일까지 감안해 넉넉히 본다
_STREAK_LOOKBACK_DAYS = 120
# 하루 3끼 완주 판정에 쓰는 끼니
_FULL_DAY_MEAL_TYPES = frozenset({"breakfast", "lunch", "dinner"})


def _forget(db: Session, obj) -> None:
    """savepoint 롤백으로 이미 빠졌을 수도 있는 객체를 안전하게 세션에서 뗀다."""
    if obj in db:
        db.expunge(obj)


def _ledger(
    db: Session,
    user_id: int,
    *,
    key: str,
    reason: str,
    xp: int = 0,
    points: int = 0,
    ref_type: str | None = None,
    ref_id: str | None = None,
    logical_date: date | None = None,
) -> bool:
    """원장에 한 줄 기록한다. 같은 키가 이미 있으면 False (= 이미 지급됨)."""
    row = RewardLedger(
        user_id=user_id,
        xp_delta=xp,
        points_delta=points,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        logical_date=logical_date,
        idempotency_key=key,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        _forget(db, row)
        return False
    return True


def _skill_usage(
    db: Session, user_id: int, *, key: str, skill_code: str, day: date, reason: str
) -> bool:
    row = SkillUsageLedger(
        user_id=user_id,
        skill_code=skill_code,
        logical_date=day,
        reason=reason,
        idempotency_key=key,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        _forget(db, row)
        return False
    return True


# --- 스트릭 ---

def _protected_days(db: Session, user_id: int) -> set[date]:
    """'쉼표 지킴이'가 보호해 준 논리 날짜."""
    rows = db.scalars(
        select(SkillUsageLedger.logical_date).where(
            SkillUsageLedger.user_id == user_id,
            SkillUsageLedger.skill_code == "streak_pause",
        )
    ).all()
    return set(rows)


def _streak_pause_skill(db: Session, profile: GameProfile) -> UserSkill | None:
    """장착 중이고 충전이 남은 '쉼표 지킴이'."""
    if profile.equipped_skill_code != "streak_pause":
        return None
    skill = db.scalar(
        select(UserSkill).where(
            UserSkill.user_id == profile.user_id, UserSkill.skill_code == "streak_pause"
        )
    )
    if skill is None or skill.charge_count <= 0:
        return None
    return skill


def recompute_streak(db: Session, profile: GameProfile, today: date) -> dict | None:
    """실제 기록에서 연속 기록일을 다시 센다. 보호가 발동하면 그 내역을 반환한다.

    요청 순서에 의존하지 않도록(과거 날짜를 나중에 기록해도) 매번 재계산한다.
    """
    days = set(recent_record_days(db, profile.user_id, _STREAK_LOOKBACK_DAYS))
    protected = _protected_days(db, profile.user_id)
    effect: dict | None = None

    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while True:
        if cursor in days or cursor in protected:
            streak += 1
            cursor -= timedelta(days=1)
            continue
        # 하루 비었을 때 '쉼표 지킴이'가 장착돼 있으면 1회 자동 보호한다.
        # (연속이 시작된 뒤에만 — 기록이 아예 없는 사용자의 충전을 태우지 않는다)
        skill = _streak_pause_skill(db, profile) if streak >= 1 else None
        if skill is not None and _skill_usage(
            db,
            profile.user_id,
            key=f"streak-pause:{cursor.isoformat()}",
            skill_code="streak_pause",
            day=cursor,
            reason="protect_missed_day",
        ):
            skill.charge_count -= 1
            recharge = catalog.skill_recharge_days("streak_pause")
            skill.recharge_at_record_days = (
                profile.total_record_days + recharge if recharge else None
            )
            protected.add(cursor)
            effect = {
                "skill_code": "streak_pause",
                "skill_name": catalog.skill_name("streak_pause"),
                "protected_date": cursor.isoformat(),
                "charges": skill.charge_count,
                "max_charges": catalog.skill_max_charges("streak_pause"),
            }
            continue
        break

    profile.current_streak = streak
    profile.best_streak = max(profile.best_streak, streak)
    return effect


def _recharge_skills(db: Session, profile: GameProfile) -> None:
    """함께 기록한 날이 쌓이면 소모된 충전을 되돌린다 (v3 §4)."""
    skills = db.scalars(
        select(UserSkill).where(UserSkill.user_id == profile.user_id)
    ).all()
    for skill in skills:
        target = skill.recharge_at_record_days
        if target is None or skill.charge_count >= catalog.skill_max_charges(skill.skill_code):
            continue
        if profile.total_record_days >= target:
            skill.charge_count += 1
            skill.recharge_at_record_days = None


# --- 펫 유대 ---

def unlock_skill(db: Session, profile: GameProfile, pet_code: str) -> str | None:
    """유대 Lv.3 달성 시 펫의 대표 스킬을 계정에 해금한다 (멱등)."""
    skill_code = catalog.signature_skill(pet_code)
    if not skill_code:
        return None
    exists = db.scalar(
        select(UserSkill.id).where(
            UserSkill.user_id == profile.user_id, UserSkill.skill_code == skill_code
        )
    )
    if exists is not None:
        return None
    skill = UserSkill(
        user_id=profile.user_id,
        skill_code=skill_code,
        source_pet_code=pet_code,
        charge_count=catalog.skill_max_charges(skill_code),
    )
    try:
        with db.begin_nested():
            db.add(skill)
            db.flush()
    except IntegrityError:
        _forget(db, skill)
        return None
    # 첫 스킬은 바로 장착해 준다 — 해금했는데 아무 일도 없으면 보상으로 읽히지 않는다
    if profile.equipped_skill_code is None:
        profile.equipped_skill_code = skill_code
    return skill_code


def apply_pet_bond_day(
    db: Session, profile: GameProfile, pet_code: str, day: date
) -> dict:
    """활성 펫의 '함께한 날'을 논리 날짜당 한 번만 올린다."""
    bond = ensure_pet_bond(db, profile.user_id, pet_code)
    before_level = bond.bond_level
    added = _ledger(
        db,
        profile.user_id,
        key=f"pet-bond:{pet_code}:{day.isoformat()}",
        reason="pet_bond",
        ref_type="pet",
        ref_id=pet_code,
        logical_date=day,
    )
    if added:
        bond.bond_days += 1
        bond.bond_level = catalog.bond_level_for(bond.bond_days)
        if bond.last_counted_logical_date is None or day > bond.last_counted_logical_date:
            bond.last_counted_logical_date = day

    unlocked_layers: list[str] = []
    unlocked_skill: str | None = None
    if bond.bond_level > before_level:
        before_layers = catalog.unlocked_growth_layers(pet_code, before_level)
        unlocked_layers = [
            layer
            for layer in catalog.unlocked_growth_layers(pet_code, bond.bond_level)
            if layer not in before_layers
        ]
        if bond.bond_level >= catalog.skill_unlock_bond_level():
            unlocked_skill = unlock_skill(db, profile, pet_code)

    return {
        "pet_code": pet_code,
        "bond_day_added": added,
        "bond_days": bond.bond_days,
        "bond_level_before": before_level,
        "bond_level_after": bond.bond_level,
        "unlocked_growth_layers": unlocked_layers,
        "unlocked_skill": unlocked_skill,
    }


# --- 해금 ---

def grant_streak_unlocks(db: Session, profile: GameProfile) -> list[str]:
    """스트릭 조건을 채운 음식을 지급한다 (멱등)."""
    items = ensure_catalog(db)
    owned = set(
        db.scalars(
            select(CatalogItem.code)
            .join(UserItem, UserItem.catalog_item_id == CatalogItem.id)
            .where(UserItem.user_id == profile.user_id)
        ).all()
    )
    granted: list[str] = []
    for entry in catalog.entries():
        if entry.unlock.get("type") != "streak" or entry.code in owned:
            continue
        if profile.current_streak < int(entry.unlock.get("days", 0)):
            continue
        row = UserItem(
            user_id=profile.user_id, catalog_item_id=items[entry.code].id, source="streak"
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            _forget(db, row)
            continue
        granted.append(entry.code)
    return granted


def next_streak_unlock(db: Session, profile: GameProfile) -> dict | None:
    """다음으로 가까운 스트릭 해금 1개 — 홈의 '다음 목표' 카드."""
    owned = set(
        db.scalars(
            select(CatalogItem.code)
            .join(UserItem, UserItem.catalog_item_id == CatalogItem.id)
            .where(UserItem.user_id == profile.user_id)
        ).all()
    )
    best: tuple[int, object] | None = None
    for entry in catalog.entries():
        if entry.unlock.get("type") != "streak" or entry.code in owned:
            continue
        target = int(entry.unlock.get("days", 0))
        if target <= 0:
            continue
        if best is None or target < best[0]:
            best = (target, entry)
    if best is None:
        return None
    target, entry = best
    remain = max(target - profile.current_streak, 0)
    return {
        "item_code": entry.code,
        "current": profile.current_streak,
        "target": target,
        "label": (
            f"하루 더 기록하면 {entry.name}" if remain == 1
            else f"{remain}일 더 기록하면 {entry.name}"
        ),
    }


# --- 레벨 ---

def _apply_xp(db: Session, profile: GameProfile, gained: int, day: date) -> dict | None:
    if gained <= 0:
        return None
    before_level = profile.level
    level, xp, ups = catalog.apply_xp(profile.level, profile.xp, gained)
    profile.level, profile.xp = level, xp
    if ups <= 0:
        return None
    # 레벨업 보상 코인은 레벨당 1회 (원장 키 `level:<n>`)
    bonus = 0
    for lv in range(before_level + 1, level + 1):
        points = catalog.level_up_points(lv)
        if _ledger(
            db,
            profile.user_id,
            key=f"level:{lv}",
            reason="level_up",
            points=points,
            ref_type="level",
            ref_id=str(lv),
            logical_date=day,
        ):
            profile.points += points
            bonus += points
    return {"level_before": before_level, "level_after": level, "points": bonus}


# --- 진입점 ---

def apply_meal_rewards(db: Session, user: User, meal: MealRecord) -> dict | None:
    """새로 저장된 식단 1건에 대한 보상을 지급하고 연출용 payload 를 돌려준다.

    생략(is_skipped) 기록은 XP·코인·유대·해금을 주지 않는다 (핸드오프 §6 PR3).
    """
    if meal.is_skipped:
        return None

    profile = ensure_game_profile(db, user)
    day = kst_date_of(meal.eaten_at, settings.day_start_hour)

    # 1) 기록 보상 — 하루 4건까지
    rewarded_today = db.scalar(
        select(func.count(RewardLedger.id)).where(
            RewardLedger.user_id == user.id,
            RewardLedger.reason == "meal",
            RewardLedger.logical_date == day,
        )
    ) or 0

    already_paid = db.scalar(
        select(RewardLedger.id).where(
            RewardLedger.user_id == user.id,
            RewardLedger.idempotency_key == f"meal:{meal.id}",
        )
    ) is not None

    base_xp = 0
    points = 0
    if not already_paid and rewarded_today < catalog.MAX_REWARDED_MEALS_PER_DAY:
        base_xp = (
            catalog.XP_PER_PHOTO_MEAL if meal.meal_image_id else catalog.XP_PER_MEAL
        )
        points = catalog.POINTS_PER_MEAL

    # 2) 스트릭 재계산 (XP 배수는 갱신된 스트릭 기준)
    skill_effect = recompute_streak(db, profile, logical_today())

    gained_xp = 0
    if base_xp:
        gained_xp = int(round(base_xp * catalog.streak_xp_multiplier(profile.current_streak)))
        # 3) '힘찬 응원' — 그날 첫 유효 기록 XP 가산 (코인에는 영향 없음)
        if profile.equipped_skill_code == "daily_xp_nudge" and _has_skill(
            db, user.id, "daily_xp_nudge"
        ) and _skill_usage(
            db,
            user.id,
            key=f"xp-nudge:{day.isoformat()}",
            skill_code="daily_xp_nudge",
            day=day,
            reason="first_meal_bonus",
        ):
            gained_xp += catalog.XP_SKILL_NUDGE
            skill_effect = skill_effect or {
                "skill_code": "daily_xp_nudge",
                "skill_name": catalog.skill_name("daily_xp_nudge"),
                "xp_bonus": catalog.XP_SKILL_NUDGE,
            }
        if not _ledger(
            db,
            user.id,
            key=f"meal:{meal.id}",
            reason="meal",
            xp=gained_xp,
            points=points,
            ref_type="meal_record",
            ref_id=str(meal.id),
            logical_date=day,
        ):
            # 같은 기록으로 이미 지급됨 (재시도) — 잔액은 건드리지 않는다
            gained_xp, points = 0, 0
        else:
            profile.points += points

    # 4) 기록일 누계 (펫을 바꿔도 이어진다)
    if _ledger(
        db,
        user.id,
        key=f"record-day:{day.isoformat()}",
        reason="record_day",
        logical_date=day,
    ):
        profile.total_record_days += 1
        _recharge_skills(db, profile)
    if profile.last_recorded_logical_date is None or day > profile.last_recorded_logical_date:
        profile.last_recorded_logical_date = day

    # 5) 하루 3끼 완주 보너스
    if _is_full_day(db, user.id, day) and _ledger(
        db,
        user.id,
        key=f"daily-complete:{day.isoformat()}",
        reason="daily_complete",
        xp=catalog.XP_DAILY_COMPLETE,
        points=catalog.POINTS_DAILY_COMPLETE,
        logical_date=day,
    ):
        gained_xp += catalog.XP_DAILY_COMPLETE
        points += catalog.POINTS_DAILY_COMPLETE
        profile.points += catalog.POINTS_DAILY_COMPLETE

    # 6) 펫 유대 — 활성 펫에게 논리 날짜당 1회
    pet_code = profile.active_pet_code or catalog.DEFAULT_PET_CODE
    pet_growth = apply_pet_bond_day(db, profile, pet_code, day)

    # 7) 레벨업 → 해금
    level_up = _apply_xp(db, profile, gained_xp, day)
    unlocked_items = grant_streak_unlocks(db, profile)

    db.flush()
    return {
        "xp": gained_xp,
        "points": points,
        "current_streak": profile.current_streak,
        "level": profile.level,
        "total_points": profile.points,
        "level_up": level_up,
        "unlocked_items": unlocked_items,
        "progress_updates": [],
        "pet_growth": pet_growth,
        "skill_effect": skill_effect,
    }


def _has_skill(db: Session, user_id: int, skill_code: str) -> bool:
    return db.scalar(
        select(UserSkill.id).where(
            UserSkill.user_id == user_id, UserSkill.skill_code == skill_code
        )
    ) is not None


def _is_full_day(db: Session, user_id: int, day: date) -> bool:
    """그 논리 날짜에 아침·점심·저녁이 모두 기록됐는가."""
    start, end = kst_day_bounds(day, settings.day_start_hour)
    types = set(
        db.scalars(
            select(MealRecord.meal_type).where(
                MealRecord.user_id == user_id,
                MealRecord.deleted_at.is_(None),
                MealRecord.is_skipped.is_(False),
                MealRecord.eaten_at >= start,
                MealRecord.eaten_at < end,
            )
        ).all()
    )
    return _FULL_DAY_MEAL_TYPES <= types
