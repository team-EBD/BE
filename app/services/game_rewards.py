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
    MealItem,
    MealRecord,
    NutritionItem,
    PetBond,
    RewardLedger,
    SkillUsageLedger,
    UnlockProgress,
    User,
    UserItem,
    UserSkill,
)
from app.services import game_catalog as catalog
from app.services.game_events import apply_event_stamps
from app.services.game_ledger import apply_xp_gain as _apply_xp
from app.services.game_ledger import forget as _forget
from app.services.game_ledger import grant_item as _grant_item
from app.services.game_ledger import ledger as _ledger
from app.services.game_ledger import skill_usage as _skill_usage
from app.services.game_missions import apply_mission_progress
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

def _owned_codes(db: Session, user_id: int) -> set[str]:
    return set(
        db.scalars(
            select(CatalogItem.code)
            .join(UserItem, UserItem.catalog_item_id == CatalogItem.id)
            .where(UserItem.user_id == user_id)
        ).all()
    )


def grant_streak_unlocks(db: Session, profile: GameProfile) -> list[str]:
    """스트릭 조건을 채운 음식을 지급한다 (멱등)."""
    items = ensure_catalog(db)
    owned = _owned_codes(db, profile.user_id)
    granted: list[str] = []
    for entry in catalog.entries():
        if entry.unlock.get("type") != "streak" or entry.code in owned:
            continue
        if profile.current_streak < int(entry.unlock.get("days", 0)):
            continue
        if _grant_item(db, profile.user_id, items[entry.code].id, "streak"):
            granted.append(entry.code)
    return granted


# --- 음식 태그 해금 진행도 ---

def _confirmed_foods(db: Session, meal: MealRecord) -> list[tuple[int, str | None]]:
    """사용자가 **최종 확정한** 음식의 (nutrition_item_id, 영양DB 이름).

    nutrition_item_id 가 없으면 자유 입력이라 세지 않는다. 이름은 curated 매핑에 없는
    항목의 키워드 폴백에 쓴다 — **여기서 한 번에 조인해 가져와 N+1 을 만들지 않는다.**
    (영양 항목은 운영 중 추가·수정되므로 이 결과는 캐시하지 않는다.)
    """
    rows = db.execute(
        select(MealItem.nutrition_item_id, NutritionItem.name)
        .join(NutritionItem, NutritionItem.id == MealItem.nutrition_item_id, isouter=True)
        .where(
            MealItem.meal_record_id == meal.id,
            MealItem.nutrition_item_id.is_not(None),
        )
    ).all()
    return [(int(nid), name) for nid, name in rows]


def _unlock_progress_row(
    db: Session, user_id: int, catalog_item_id: int, target: int
) -> UnlockProgress:
    row = db.scalar(
        select(UnlockProgress).where(
            UnlockProgress.user_id == user_id,
            UnlockProgress.catalog_item_id == catalog_item_id,
        )
    )
    if row is None:
        row = UnlockProgress(
            user_id=user_id,
            catalog_item_id=catalog_item_id,
            current_value=0,
            target_value=target,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:  # pragma: no cover — 동시 요청
            _forget(db, row)
            row = db.scalar(
                select(UnlockProgress).where(
                    UnlockProgress.user_id == user_id,
                    UnlockProgress.catalog_item_id == catalog_item_id,
                )
            )
    elif row.target_value != target:
        # 기획이 목표를 바꾸면 따라간다 (이미 지급된 아이템은 회수하지 않는다)
        row.target_value = target
    return row


def _food_progress_keys(
    entry, *, day: date, meal: MealRecord, tags: frozenset[str], item_ids: list[int]
) -> list[str]:
    """이번 기록이 이 해금에 기여하는 '세는 단위'의 멱등 키 목록.

    `pet-bond:<pet>:<date>` 와 같은 원장 멱등 방식이다. 같은 논리 날짜(또는 같은
    메뉴)는 몇 번을 기록해도 한 번만 센다 — 과거 날짜를 나중에 기록해도 안전하다.
    """
    rule = entry.unlock.get("rule")
    if rule == catalog.FOOD_RULE_TAG_DAYS:
        wanted = set(entry.unlock.get("food_tags") or [])
        if wanted & tags:
            return [f"food-progress:{entry.code}:{day.isoformat()}"]
        return []
    if rule == catalog.FOOD_RULE_MEAL_SLOT_DAYS:
        if meal.meal_type == entry.unlock.get("meal_slot"):
            return [f"food-progress:{entry.code}:{day.isoformat()}"]
        return []
    if rule == catalog.FOOD_RULE_MENU_COUNT:
        # 날짜가 아니라 '서로 다른 메뉴' 가짓수를 센다
        return [f"food-progress:{entry.code}:menu:{nid}" for nid in sorted(set(item_ids))]
    return []


def _apply_food_progress(
    db: Session,
    profile: GameProfile,
    meal: MealRecord,
    day: date,
    *,
    item_ids: list[int],
    tags: frozenset[str],
) -> tuple[list[dict], list[str]]:
    """확정된 음식으로 음식 해금 진행도를 올린다. (progress_updates, 새로 해금된 코드)."""
    if not item_ids:
        return [], []

    items = ensure_catalog(db)
    owned = _owned_codes(db, profile.user_id)
    updates: list[dict] = []
    unlocked_codes: list[str] = []

    for entry in catalog.food_unlock_entries():
        target = catalog.food_unlock_target(entry)
        if target <= 0 or entry.code in owned:
            continue
        keys = _food_progress_keys(
            entry, day=day, meal=meal, tags=tags, item_ids=item_ids
        )
        if not keys:
            continue

        row = _unlock_progress_row(db, profile.user_id, items[entry.code].id, target)
        gained = 0
        for key in keys:
            if _ledger(
                db,
                profile.user_id,
                key=key,
                reason="food_progress",
                ref_type="catalog_item",
                ref_id=entry.code,
                logical_date=day,
            ):
                gained += 1
        if gained == 0:
            continue  # 이미 센 날짜/메뉴 — 진행도는 그대로다

        row.current_value += gained
        if row.last_counted_logical_date is None or day > row.last_counted_logical_date:
            row.last_counted_logical_date = day

        unlocked = row.current_value >= row.target_value
        if unlocked and _grant_item(db, profile.user_id, items[entry.code].id, "food"):
            unlocked_codes.append(entry.code)
        updates.append(
            {
                "item_code": entry.code,
                # 메뉴 규칙은 한 끼에 새 메뉴가 여러 개면 한 번에 여러 칸 오른다.
                # 저장값은 그대로 두고 표시값만 목표에서 끊는다 ("9/8" 을 막는다).
                "current": min(row.current_value, row.target_value),
                "target": row.target_value,
                "unlocked": unlocked,
            }
        )
    return updates, unlocked_codes


def food_progress_by_code(db: Session, user_id: int) -> dict[str, UnlockProgress]:
    """아이템 코드 → 진행도 행 (컬렉션 응답·다음 목표 계산용)."""
    rows = db.execute(
        select(CatalogItem.code, UnlockProgress)
        .join(UnlockProgress, UnlockProgress.catalog_item_id == CatalogItem.id)
        .where(UnlockProgress.user_id == user_id)
    ).all()
    return {code: row for code, row in rows}


def _food_goal_label(entry, remain: int) -> str:
    """음식 목표 문구 — 비판단 말투 (v3 §2)."""
    rule = entry.unlock.get("rule")
    days = "하루" if remain == 1 else f"{remain}일"
    if rule == catalog.FOOD_RULE_TAG_DAYS:
        tags = entry.unlock.get("food_tags") or []
        what = "·".join(catalog.food_tag_label(t) for t in tags)
        return f"{what}가 든 식사를 {days} 더 기록하면 {entry.name}"
    if rule == catalog.FOOD_RULE_MEAL_SLOT_DAYS:
        slot = {"breakfast": "아침", "lunch": "점심", "dinner": "저녁", "snack": "간식"}.get(
            entry.unlock.get("meal_slot"), "식사"
        )
        return f"{slot}을 {days} 더 기록하면 {entry.name}"
    if rule == catalog.FOOD_RULE_MENU_COUNT:
        count = "한 가지" if remain == 1 else f"{remain}가지"
        return f"서로 다른 메뉴를 {count} 더 기록하면 {entry.name}"
    return f"{days} 더 기록하면 {entry.name}"  # pragma: no cover — 규칙 추가 대비


def _streak_goal_label(entry, remain: int) -> str:
    return (
        f"하루 더 기록하면 {entry.name}" if remain == 1
        else f"{remain}일 더 기록하면 {entry.name}"
    )


def next_unlock(db: Session, profile: GameProfile) -> dict | None:
    """다음으로 가까운 해금 1개 — 홈의 '다음 목표' 카드.

    스트릭 해금과 음식 해금을 함께 보고 **남은 양이 가장 적은 1개**를 고른다.
    (같으면 목표가 작은 쪽 → 카탈로그 정렬 순서. 매 조회에 같은 답이 나와야 한다)
    """
    owned = _owned_codes(db, profile.user_id)
    progress = food_progress_by_code(db, profile.user_id)
    best: tuple[int, int, int, dict] | None = None  # (남은 양, 목표, 정렬 순서, payload)

    for entry in catalog.entries():
        unlock_type = entry.unlock.get("type")
        if unlock_type not in ("streak", "food") or entry.code in owned:
            continue
        if unlock_type == "streak":
            target = int(entry.unlock.get("days", 0))
            current = profile.current_streak
        else:
            target = catalog.food_unlock_target(entry)
            row = progress.get(entry.code)
            current = row.current_value if row is not None else 0
        if target <= 0:
            continue
        remain = max(target - current, 0)
        label = (
            _streak_goal_label(entry, remain)
            if unlock_type == "streak"
            else _food_goal_label(entry, remain)
        )
        candidate = (
            remain,
            target,
            entry.sort_order,
            {
                "item_code": entry.code,
                "current": current,
                "target": target,
                "label": label,
            },
        )
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    return best[3] if best is not None else None


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

    # 7) 확정된 음식 1회 조회 — 해금 진행도와 미션 판정이 같은 결과를 쓴다
    foods = _confirmed_foods(db, meal)
    item_ids = [nid for nid, _name in foods]
    found: set[str] = set()
    for nid, name in foods:
        found |= catalog.food_tags_for(nid, name)
    tags = frozenset(found)

    # 7-1) 음식 태그 해금 진행도 — 확정된 음식만, 같은 날/같은 메뉴는 한 번만 센다
    progress_updates, food_unlocks = _apply_food_progress(
        db, profile, meal, day, item_ids=item_ids, tags=tags
    )

    # 7-2) 미션 진행도 — 완료해도 자동 지급하지 않는다 (홈의 claimable 로 간다).
    #      완료된 미션은 `mission_count` 이벤트 진행도도 함께 올린다.
    apply_mission_progress(db, profile, meal, day, item_ids=item_ids, tags=tags)

    # 7-3) 이벤트 스탬프 — 기간 중 기록한 서로 다른 논리 날짜 수 (하루 1장)
    apply_event_stamps(db, profile, day)

    # 8) 레벨업 → 해금
    level_up = _apply_xp(db, profile, gained_xp, day)
    unlocked_items = grant_streak_unlocks(db, profile) + food_unlocks

    db.flush()
    return {
        "xp": gained_xp,
        "points": points,
        "current_streak": profile.current_streak,
        "level": profile.level,
        "total_points": profile.points,
        "level_up": level_up,
        "unlocked_items": unlocked_items,
        "progress_updates": progress_updates,
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
