"""미션 도메인 — 일일 3개(티어당 1) + 주간 1개 (핸드오프 §6.2).

핵심 규칙:

- **결정론적 출제.** 시드는 `(user_id, period_key)` 의 sha256 해시다. 파이썬 내장
  `hash()` 는 프로세스마다 달라지므로 절대 쓰지 않는다 — 같은 사용자·같은 날은
  언제 조회해도 같은 미션이 나와야 한다.
- `period_key` 는 일일이면 논리 날짜, 주간이면 그 주 **월요일**의 논리 날짜.
- 진행도는 식단 저장 경로에서 올리고 **수령은 수동**이다 (홈의 `claimable`).
- 진행도 증가와 보상 지급 **둘 다** 기존 `reward_ledger` 멱등 키로 막는다.
  새 원장 테이블을 만들지 않는다 — 잔액 갱신 경로가 둘이 되면 정합성이 깨진다.
- 일일 지급 상한(하루 4건)은 `reason == "meal"` 만 세므로, 여기서 쓰는
  `mission_progress` / `mission` reason 은 상한에 영향을 주지 않는다.

이 모듈은 커밋하지 않는다 (트랜잭션 경계는 호출부가 잡는다).
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.core.timeutil import now_utc
from app.models import (
    GameMission,
    GameProfile,
    MealItem,
    MealRecord,
    SkillUsageLedger,
    UserMission,
    UserSkill,
)
from app.services import game_catalog as catalog
from app.services.game_events import apply_mission_completion
from app.services.game_ledger import apply_xp_gain, forget, ledger, skill_usage

# 원장 reason — "meal" 이 아니어야 하루 4건 지급 상한이 깨지지 않는다
PROGRESS_REASON = "mission_progress"
CLAIM_REASON = "mission"

SWAP_SKILL_CODE = "daily_mission_swap"


# --- 기간 ---

def week_start(day: date) -> date:
    """그 논리 날짜가 속한 주의 월요일."""
    return day - timedelta(days=day.weekday())


def period_key(scope: str, day: date) -> str:
    if scope == catalog.MISSION_SCOPE_WEEKLY:
        return week_start(day).isoformat()
    return day.isoformat()


# --- 결정론적 출제 ---

def _seed_int(*parts: str) -> int:
    """sha256 기반 시드. 프로세스가 바뀌어도 같은 값이 나온다."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _pick(pool: tuple, *parts: str):
    if not pool:
        return None
    return pool[_seed_int(*parts) % len(pool)]


# --- 카탈로그 ---

def ensure_mission_catalog(db: Session) -> dict[str, GameMission]:
    """seed/game_missions.json 을 game_missions 에 idempotent 하게 반영한다."""
    rows = {row.code: row for row in db.scalars(select(GameMission))}
    dirty = False
    for spec in catalog.mission_specs():
        row = rows.get(spec.code)
        if row is None:
            row = GameMission(
                code=spec.code,
                title=spec.title,
                description=spec.description,
                scope=spec.scope,
                tier=spec.tier,
                rule=spec.rule,
                params=dict(spec.params),
                target_value=spec.target,
                reward_xp=spec.xp,
                reward_points=spec.points,
                is_active=True,
                sort_order=spec.sort_order,
            )
            db.add(row)
            rows[spec.code] = row
            dirty = True
        elif (
            row.title != spec.title
            or row.description != spec.description
            or row.scope != spec.scope
            or row.tier != spec.tier
            or row.rule != spec.rule
            or row.params != spec.params
            or row.target_value != spec.target
            or row.reward_xp != spec.xp
            or row.reward_points != spec.points
            or row.sort_order != spec.sort_order
        ):
            row.title = spec.title
            row.description = spec.description
            row.scope = spec.scope
            row.tier = spec.tier
            row.rule = spec.rule
            row.params = dict(spec.params)
            row.target_value = spec.target
            row.reward_xp = spec.xp
            row.reward_points = spec.points
            row.sort_order = spec.sort_order
            dirty = True
    if dirty:
        db.flush()
    return rows


# --- 출제 ---

def _create_row(db: Session, user_id: int, spec, key: str) -> UserMission:
    row = UserMission(
        user_id=user_id,
        mission_code=spec.code,
        scope=spec.scope,
        tier=spec.tier,
        period_key=key,
        progress=0,
        target_value=spec.target,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # pragma: no cover — 동시 요청
        forget(db, row)
        row = db.scalar(
            select(UserMission).where(
                UserMission.user_id == user_id,
                UserMission.mission_code == spec.code,
                UserMission.period_key == key,
            )
        )
    return row


def ensure_missions(db: Session, user_id: int, day: date) -> list[UserMission]:
    """그 논리 날짜의 미션(일일 3 + 주간 1)을 보장하고 돌려준다.

    출제 시점에 `user_missions` 행을 만들고 이후 조회는 그 행을 그대로 쓴다 —
    **같은 날 미션이 바뀌면 안 된다** (바꾸기 스킬만 예외).
    """
    ensure_mission_catalog(db)
    daily_key = day.isoformat()
    weekly_key = week_start(day).isoformat()

    rows = list(
        db.scalars(
            select(UserMission).where(
                UserMission.user_id == user_id,
                UserMission.period_key.in_([daily_key, weekly_key]),
            )
        )
    )
    daily = [r for r in rows if r.scope == catalog.MISSION_SCOPE_DAILY and r.period_key == daily_key]
    weekly = [r for r in rows if r.scope == catalog.MISSION_SCOPE_WEEKLY and r.period_key == weekly_key]

    taken = {r.mission_code for r in daily}
    for tier in catalog.daily_tiers():
        if any(r.tier == tier for r in daily):
            continue
        pool = tuple(
            m for m in catalog.mission_pool(catalog.MISSION_SCOPE_DAILY, tier)
            if m.code not in taken
        )
        spec = _pick(pool, str(user_id), daily_key, f"daily:{tier}")
        if spec is None:  # pragma: no cover — 티어가 비어 있는 시드
            continue
        taken.add(spec.code)
        daily.append(_create_row(db, user_id, spec, daily_key))

    if not weekly:
        pool = catalog.mission_pool(catalog.MISSION_SCOPE_WEEKLY)
        spec = _pick(pool, str(user_id), weekly_key, "weekly")
        if spec is not None:
            weekly.append(_create_row(db, user_id, spec, weekly_key))

    # 시드에서 목표가 바뀌면 진행 중인 행도 따라간다 (이미 수령한 보상은 회수하지 않는다)
    for row in [*daily, *weekly]:
        spec = catalog.mission_spec(row.mission_code)
        if spec is not None and row.target_value != spec.target:
            row.target_value = spec.target

    daily.sort(key=lambda r: r.tier)
    return [*daily, *weekly]


# --- 진행도 ---

def _new_menu_ids(db: Session, user_id: int, meal: MealRecord, item_ids: list[int]) -> list[int]:
    """그 사용자가 **처음 기록하는** nutrition_item_id 만 남긴다."""
    if not item_ids:
        return []
    seen = set(
        db.scalars(
            select(MealItem.nutrition_item_id)
            .join(MealRecord, MealRecord.id == MealItem.meal_record_id)
            .where(
                MealRecord.user_id == user_id,
                MealRecord.id != meal.id,
                MealRecord.deleted_at.is_(None),
                MealRecord.is_skipped.is_(False),
                MealItem.nutrition_item_id.in_(set(item_ids)),
            )
        ).all()
    )
    return sorted(set(item_ids) - seen)


def _progress_keys(
    spec,
    row: UserMission,
    *,
    day: date,
    meal: MealRecord,
    tags: frozenset[str],
    item_ids: list[int],
    new_menu_ids: list[int],
) -> list[str]:
    """이번 기록이 이 미션에 기여하는 '세는 단위'의 멱등 키 목록.

    `_food_progress_keys` 와 같은 패턴이다 — 같은 식단/날짜/메뉴는 몇 번을 다시
    처리해도 한 번만 센다 (과거 날짜를 나중에 기록해도 안전하다).
    """
    base = f"mission-progress:{spec.code}:{row.period_key}"
    rule = spec.rule
    if rule == catalog.MISSION_RULE_MEALS:
        return [f"{base}:meal:{meal.id}"]
    if rule == catalog.MISSION_RULE_PHOTO:
        return [f"{base}:meal:{meal.id}"] if meal.meal_image_id else []
    if rule == catalog.MISSION_RULE_MEAL_SLOT:
        wanted = spec.params.get("meal_type")
        return [f"{base}:meal:{meal.id}"] if meal.meal_type == wanted else []
    if rule == catalog.MISSION_RULE_FOOD_TAG:
        wanted = spec.params.get("food_tag")
        return [f"{base}:meal:{meal.id}"] if wanted in tags else []
    if rule == catalog.MISSION_RULE_NEW_MENU:
        return [f"{base}:menu:{nid}" for nid in new_menu_ids]
    if rule == catalog.MISSION_RULE_DISTINCT_MENUS:
        return [f"{base}:menu:{nid}" for nid in sorted(set(item_ids))]
    if rule == catalog.MISSION_RULE_RECORD_DAYS:
        return [f"{base}:day:{day.isoformat()}"]
    return []  # pragma: no cover — 규칙 추가 대비


def apply_mission_progress(
    db: Session,
    profile: GameProfile,
    meal: MealRecord,
    day: date,
    *,
    item_ids: list[int],
    tags: frozenset[str],
) -> list[dict]:
    """식단 1건으로 그 기간의 미션 진행도를 올린다. 완료해도 자동 지급하지 않는다.

    반환: 이번에 새로 **완료된** 미션 요약 (연출·이벤트 집계용).
    """
    rows = ensure_missions(db, profile.user_id, day)
    specs = [catalog.mission_spec(row.mission_code) for row in rows]
    needs_new_menu = any(
        spec is not None and spec.rule == catalog.MISSION_RULE_NEW_MENU for spec in specs
    )
    new_menu_ids = (
        _new_menu_ids(db, profile.user_id, meal, item_ids) if needs_new_menu else []
    )

    completed: list[dict] = []
    for row, spec in zip(rows, specs):
        if spec is None:  # pragma: no cover — 시드에서 사라진 미션
            continue
        was_complete = row.progress >= row.target_value
        keys = _progress_keys(
            spec,
            row,
            day=day,
            meal=meal,
            tags=tags,
            item_ids=item_ids,
            new_menu_ids=new_menu_ids,
        )
        gained = 0
        for key in keys:
            if ledger(
                db,
                profile.user_id,
                key=key,
                reason=PROGRESS_REASON,
                ref_type="mission",
                ref_id=spec.code,
                logical_date=day,
            ):
                gained += 1
        if gained == 0:
            continue
        row.progress += gained
        if not was_complete and row.progress >= row.target_value:
            row.completed_at = now_utc()
            # 완료한 미션 수로 세는 이벤트(`mission_count`)의 진행도도 함께 올린다
            apply_mission_completion(db, profile.user_id, spec.code, row.period_key, day)
            completed.append(
                {
                    "code": spec.code,
                    "title": spec.title,
                    "scope": spec.scope,
                    "period_key": row.period_key,
                    "reward": {"xp": spec.xp, "points": spec.points},
                }
            )
    return completed


# --- 조회 ---

def _mission_view(row: UserMission, spec) -> dict:
    return {
        "code": spec.code,
        "title": spec.title,
        "description": spec.description,
        "scope": spec.scope,
        "tier": spec.tier,
        "target": row.target_value,
        "progress": min(row.progress, row.target_value),
        "completed": row.progress >= row.target_value,
        "claimed": row.claimed_at is not None,
        "reward": {"xp": spec.xp, "points": spec.points},
        "period_key": row.period_key,
    }


def _has_skill(db: Session, user_id: int, skill_code: str) -> bool:
    return db.scalar(
        select(UserSkill.id).where(
            UserSkill.user_id == user_id, UserSkill.skill_code == skill_code
        )
    ) is not None


def swap_used_today(db: Session, user_id: int, today: date) -> bool:
    return db.scalar(
        select(SkillUsageLedger.id).where(
            SkillUsageLedger.user_id == user_id,
            SkillUsageLedger.idempotency_key == f"mission-swap:{today.isoformat()}",
        )
    ) is not None


def list_missions(db: Session, profile: GameProfile, today: date) -> dict:
    """일일 3 + 주간 1 + 바꾸기 가능 여부."""
    rows = ensure_missions(db, profile.user_id, today)
    missions = []
    for row in rows:
        spec = catalog.mission_spec(row.mission_code)
        if spec is None:  # pragma: no cover
            continue
        missions.append(_mission_view(row, spec))

    swap_ready = (
        profile.equipped_skill_code == SWAP_SKILL_CODE
        and _has_skill(db, profile.user_id, SWAP_SKILL_CODE)
        and not swap_used_today(db, profile.user_id, today)
    )
    return {
        "missions": missions,
        "swap_available": bool(
            swap_ready and any(m["scope"] == "daily" and m["progress"] == 0 for m in missions)
        ),
        "swap_skill_equipped": profile.equipped_skill_code == SWAP_SKILL_CODE,
    }


def claimable(db: Session, user_id: int, today: date) -> list[dict]:
    """홈의 `claimable` 에 실을 '완료했지만 아직 안 받은' 미션 (§6.3 형식)."""
    rows = ensure_missions(db, user_id, today)
    out: list[dict] = []
    for row in rows:
        spec = catalog.mission_spec(row.mission_code)
        if spec is None:  # pragma: no cover
            continue
        if row.claimed_at is not None or row.progress < row.target_value:
            continue
        out.append(
            {
                "type": "mission",
                "code": spec.code,
                "label": spec.title,
                "reward": {"xp": spec.xp, "points": spec.points},
            }
        )
    return out


# --- 수령 ---

def _current_row(db: Session, user_id: int, spec, today: date) -> UserMission | None:
    key = period_key(spec.scope, today)
    return db.scalar(
        select(UserMission).where(
            UserMission.user_id == user_id,
            UserMission.mission_code == spec.code,
            UserMission.period_key == key,
        )
    )


def claim_mission(db: Session, profile: GameProfile, code: str, today: date) -> dict:
    """완료된 미션의 보상을 수령한다. 기간이 지난 미션은 소급 수령할 수 없다."""
    spec = catalog.mission_spec(code)
    if spec is None:
        raise APIError(404, "MISSION_NOT_FOUND", "존재하지 않는 미션이에요.")
    ensure_missions(db, profile.user_id, today)
    row = _current_row(db, profile.user_id, spec, today)
    if row is None:
        raise APIError(404, "MISSION_NOT_FOUND", "지금 진행 중인 미션이 아니에요.")
    if row.claimed_at is not None:
        raise APIError(409, "MISSION_ALREADY_CLAIMED", "이미 받은 미션이에요.")
    if row.progress < row.target_value:
        raise APIError(409, "MISSION_NOT_COMPLETED", "아직 완료하지 않은 미션이에요.")

    if not ledger(
        db,
        profile.user_id,
        key=f"mission:{spec.code}:{row.period_key}",
        reason=CLAIM_REASON,
        xp=spec.xp,
        points=spec.points,
        ref_type="mission",
        ref_id=spec.code,
        logical_date=today,
    ):
        raise APIError(409, "MISSION_ALREADY_CLAIMED", "이미 받은 미션이에요.")

    profile.points += spec.points
    row.claimed_at = now_utc()
    level_up = apply_xp_gain(db, profile, spec.xp, today)
    return {
        "code": spec.code,
        "period_key": row.period_key,
        "reward": {"xp": spec.xp, "points": spec.points},
        "points": profile.points,
        "level": profile.level,
        "level_up": level_up,
    }


# --- 오늘의 바꾸기 (daily_mission_swap) ---

def swap_mission(db: Session, profile: GameProfile, code: str, today: date) -> dict:
    """시작 전(progress == 0) 일일 미션 하나를 **같은 티어 안에서** 바꾼다 (일 1회)."""
    if profile.equipped_skill_code != SWAP_SKILL_CODE or not _has_skill(
        db, profile.user_id, SWAP_SKILL_CODE
    ):
        raise APIError(400, "SKILL_NOT_UNLOCKED", "아직 쓸 수 없는 스킬이에요.")

    spec = catalog.mission_spec(code)
    if spec is None or spec.scope != catalog.MISSION_SCOPE_DAILY:
        raise APIError(404, "MISSION_NOT_FOUND", "바꿀 수 있는 일일 미션이 아니에요.")

    rows = ensure_missions(db, profile.user_id, today)
    row = next(
        (
            r for r in rows
            if r.mission_code == code and r.scope == catalog.MISSION_SCOPE_DAILY
        ),
        None,
    )
    if row is None:
        raise APIError(404, "MISSION_NOT_FOUND", "오늘 출제된 미션이 아니에요.")
    if row.progress > 0:
        raise APIError(409, "MISSION_ALREADY_STARTED", "이미 시작한 미션은 바꿀 수 없어요.")

    taken = {r.mission_code for r in rows if r.period_key == row.period_key}
    pool = tuple(
        m for m in catalog.mission_pool(catalog.MISSION_SCOPE_DAILY, row.tier)
        if m.code not in taken
    )
    if not pool:
        raise APIError(409, "MISSION_ALREADY_STARTED", "바꿀 수 있는 미션이 없어요.")

    if not skill_usage(
        db,
        profile.user_id,
        key=f"mission-swap:{today.isoformat()}",
        skill_code=SWAP_SKILL_CODE,
        day=today,
        reason="swap_daily_mission",
    ):
        raise APIError(409, "SKILL_CHARGE_EXHAUSTED", "오늘은 이미 한 번 바꿨어요.")

    replacement = _pick(pool, str(profile.user_id), row.period_key, f"swap:{row.tier}:{code}")
    row.mission_code = replacement.code
    row.target_value = replacement.target
    row.progress = 0
    row.completed_at = None
    row.claimed_at = None
    db.flush()
    return _mission_view(row, replacement)
