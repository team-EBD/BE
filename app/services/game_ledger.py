"""보상 원장 기본기 — 멱등 지급의 유일한 관문.

미션·이벤트가 생기면서 원장에 쓰는 곳이 여러 모듈이 됐다. **잔액 갱신 경로가
둘이 되면 정합성이 깨지므로**, 원장에 줄을 남기는 방법은 여기 한 곳만 둔다
(새 원장 테이블을 만들지 않는다 — 핸드오프 §4).

이 모듈은 커밋하지 않는다. 트랜잭션 경계는 호출부(services/meals · api/v1/game)가 잡는다.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GameProfile, RewardLedger, SkillUsageLedger, UserItem
from app.services import game_catalog as catalog


def forget(db: Session, obj) -> None:
    """savepoint 롤백으로 이미 빠졌을 수도 있는 객체를 안전하게 세션에서 뗀다."""
    if obj in db:
        db.expunge(obj)


def ledger(
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
    """원장에 한 줄 기록한다. 같은 키가 이미 있으면 False (= 이미 지급됨).

    `reason` 에 `"meal"` 을 쓰면 하루 4건 지급 상한을 세는 쿼리에 섞여 상한이
    깨진다. 진행도/미션/이벤트 행은 반드시 다른 reason 을 쓴다.
    """
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
        forget(db, row)
        return False
    return True


def skill_usage(
    db: Session, user_id: int, *, key: str, skill_code: str, day: date, reason: str
) -> bool:
    """스킬 발동 1회를 기록한다. 같은 키가 이미 있으면 False (= 오늘 이미 썼다)."""
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
        forget(db, row)
        return False
    return True


def grant_item(db: Session, user_id: int, catalog_item_id: int, source: str) -> bool:
    """아이템 지급 1건. 이미 보유 중이면 False (unique 제약으로 멱등)."""
    row = UserItem(user_id=user_id, catalog_item_id=catalog_item_id, source=source)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        forget(db, row)
        return False
    return True


def apply_xp_gain(
    db: Session, profile: GameProfile, gained: int, day: date | None
) -> dict | None:
    """XP 를 더하고 레벨업 보상 코인을 지급한다 (레벨당 1회, 원장 키 `level:<n>`).

    포인트는 항상 원장과 같은 트랜잭션에서 profile.points 로 반영한다.
    """
    if gained <= 0:
        return None
    before_level = profile.level
    level, xp, ups = catalog.apply_xp(profile.level, profile.xp, gained)
    profile.level, profile.xp = level, xp
    if ups <= 0:
        return None
    bonus = 0
    for lv in range(before_level + 1, level + 1):
        points = catalog.level_up_points(lv)
        if ledger(
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
