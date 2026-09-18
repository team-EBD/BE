"""이벤트 도메인 — 스탬프/미션 수로 이벤트 소품·펫·배경을 연다 (핸드오프 §6.4).

- **스탬프 = 이벤트 기간 중 기록한 서로 다른 논리 날짜 수.** 하루에 여러 번 기록해도 1장.
- `mission_count` 이벤트는 기간 중 **완료한**(수령 여부 무관) 미션 수로 센다.
- 기간 밖에서는 진행도가 오르지 않고, 기간이 끝나면 미수령 보상은 만료된다.
- 지급은 `UserItem` + 기존 `reward_ledger`(`event:<code>:stamp:<n>`)를 쓴다.
  **코인 판매로 우회 지급하지 않는다** — 상점은 `unlock.type == "event"` 를 계속 제외한다.

이 모듈은 커밋하지 않는다 (트랜잭션 경계는 호출부가 잡는다).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.models import GameEvent, GameProfile, UserEvent
from app.services import game_catalog as catalog
from app.services.game_ledger import forget, grant_item, ledger
from app.services.game_profile import ensure_catalog

# 진행도 원장의 reason — 하루 4건 지급 상한은 reason == "meal" 만 세므로 섞이지 않는다
PROGRESS_REASON = "event_progress"
CLAIM_REASON = "event"


def _parse(value: str) -> date:
    return date.fromisoformat(value)


def ensure_event_catalog(db: Session) -> dict[str, GameEvent]:
    """seed/game_events.json 을 game_events 에 idempotent 하게 반영한다.

    **기간의 단일 원천은 시드다** — 시드를 고치면 다음 요청에서 바로 반영된다
    (코드 배포 없이 이벤트를 켜고 끈다).
    """
    rows = {row.code: row for row in db.scalars(select(GameEvent))}
    dirty = False
    for spec in catalog.event_specs():
        starts_on, ends_on = _parse(spec.starts_on), _parse(spec.ends_on)
        rewards = [dict(r) for r in spec.rewards]
        row = rows.get(spec.code)
        if row is None:
            row = GameEvent(
                code=spec.code,
                name=spec.name,
                description=spec.description,
                rule=spec.rule,
                starts_on=starts_on,
                ends_on=ends_on,
                rewards=rewards,
                is_active=True,
                sort_order=spec.sort_order,
            )
            db.add(row)
            rows[spec.code] = row
            dirty = True
        elif (
            row.name != spec.name
            or row.description != spec.description
            or row.rule != spec.rule
            or row.starts_on != starts_on
            or row.ends_on != ends_on
            or row.rewards != rewards
            or row.sort_order != spec.sort_order
        ):
            row.name = spec.name
            row.description = spec.description
            row.rule = spec.rule
            row.starts_on = starts_on
            row.ends_on = ends_on
            row.rewards = rewards
            row.sort_order = spec.sort_order
            dirty = True
    if dirty:
        db.flush()
    return rows


def active_events(db: Session, day: date, rule: str | None = None) -> list[GameEvent]:
    """그 논리 날짜가 기간 안에 드는 이벤트 (정렬 순서 고정)."""
    rows = ensure_event_catalog(db).values()
    return sorted(
        (
            row
            for row in rows
            if row.is_active
            and row.starts_on <= day <= row.ends_on
            and (rule is None or row.rule == rule)
        ),
        key=lambda r: r.sort_order,
    )


def _user_event(db: Session, user_id: int, event_code: str) -> UserEvent:
    row = db.scalar(
        select(UserEvent).where(
            UserEvent.user_id == user_id, UserEvent.event_code == event_code
        )
    )
    if row is not None:
        return row
    row = UserEvent(
        user_id=user_id, event_code=event_code, progress=0, claimed_thresholds=[]
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # pragma: no cover — 동시 요청
        forget(db, row)
        row = db.scalar(
            select(UserEvent).where(
                UserEvent.user_id == user_id, UserEvent.event_code == event_code
            )
        )
    return row


def user_events(db: Session, user_id: int) -> dict[str, UserEvent]:
    return {
        row.event_code: row
        for row in db.scalars(select(UserEvent).where(UserEvent.user_id == user_id))
    }


def _bump(db: Session, user_id: int, event: GameEvent, key: str, day: date) -> bool:
    """진행도 +1. 같은 단위(날짜/미션)를 두 번 세지 않도록 원장 키로 막는다."""
    if not ledger(
        db,
        user_id,
        key=key,
        reason=PROGRESS_REASON,
        ref_type="game_event",
        ref_id=event.code,
        logical_date=day,
    ):
        return False
    row = _user_event(db, user_id, event.code)
    row.progress += 1
    if row.last_counted_logical_date is None or day > row.last_counted_logical_date:
        row.last_counted_logical_date = day
    return True


def apply_event_stamps(db: Session, profile: GameProfile, day: date) -> list[str]:
    """식단을 저장한 논리 날짜로 스탬프를 찍는다 (하루 1장, 기간 안에서만)."""
    stamped: list[str] = []
    for event in active_events(db, day, rule=catalog.EVENT_RULE_STAMP):
        key = f"event-progress:{event.code}:{day.isoformat()}"
        if _bump(db, profile.user_id, event, key, day):
            stamped.append(event.code)
    return stamped


def apply_mission_completion(
    db: Session, user_id: int, mission_code: str, period_key: str, day: date
) -> list[str]:
    """미션 1개가 완료됐을 때 `mission_count` 이벤트의 진행도를 올린다."""
    counted: list[str] = []
    for event in active_events(db, day, rule=catalog.EVENT_RULE_MISSION_COUNT):
        key = f"event-progress:{event.code}:mission:{mission_code}:{period_key}"
        if _bump(db, user_id, event, key, day):
            counted.append(event.code)
    return counted


# --- 조회 ---

def _reward_view(event: GameEvent, reward: dict, progress: int, claimed: list) -> dict:
    key = "stamp" if event.rule == catalog.EVENT_RULE_STAMP else "count"
    threshold = int(reward.get(key) or 0)
    item = catalog.get(reward.get("item_code", ""))
    return {
        "stamp": threshold if key == "stamp" else None,
        "count": threshold if key == "count" else None,
        "item_code": reward.get("item_code"),
        "preview_key": item.preview_key if item else None,
        "name": item.name if item else reward.get("item_code"),
        "reached": progress >= threshold,
        "claimed": threshold in claimed,
    }


def _thresholds(event: GameEvent) -> list[int]:
    key = "stamp" if event.rule == catalog.EVENT_RULE_STAMP else "count"
    return sorted(int(r[key]) for r in event.rewards if r.get(key) is not None)


def list_events(db: Session, user_id: int, today: date) -> list[dict]:
    """기간 중인 이벤트 + 내 진행도 + 보상 도달/수령 여부."""
    mine = user_events(db, user_id)
    out: list[dict] = []
    for event in active_events(db, today):
        row = mine.get(event.code)
        progress = row.progress if row else 0
        claimed = list(row.claimed_thresholds or []) if row else []
        thresholds = _thresholds(event)
        out.append(
            {
                "code": event.code,
                "name": event.name,
                "description": event.description,
                "rule": event.rule,
                "progress": progress,
                "target": thresholds[-1] if thresholds else 0,
                "starts_on": event.starts_on.isoformat(),
                "ends_on": event.ends_on.isoformat(),
                "rewards": [
                    _reward_view(event, reward, progress, claimed)
                    for reward in event.rewards
                ],
            }
        )
    return out


def claimable(db: Session, user_id: int, today: date) -> list[dict]:
    """홈의 `claimable` 에 실을 '지금 받을 수 있는' 이벤트 보상 (§6.3 형식)."""
    mine = user_events(db, user_id)
    out: list[dict] = []
    for event in active_events(db, today):
        row = mine.get(event.code)
        if row is None:
            continue
        claimed = set(row.claimed_thresholds or [])
        key = "stamp" if event.rule == catalog.EVENT_RULE_STAMP else "count"
        unit = "스탬프" if key == "stamp" else "미션"
        for reward in event.rewards:
            threshold = int(reward.get(key) or 0)
            if threshold <= 0 or row.progress < threshold or threshold in claimed:
                continue
            out.append(
                {
                    "type": "event",
                    "code": event.code,
                    "label": f"{event.name} {unit} {threshold}",
                    "reward": {
                        "item_code": reward.get("item_code"),
                        key: threshold,
                    },
                }
            )
    return out


# --- 수령 ---

def claim_reward(
    db: Session, profile: GameProfile, code: str, threshold: int, today: date
) -> dict:
    """스탬프(또는 미션 수) 보상 1건을 수령한다. 기간 밖이면 받을 수 없다."""
    event = next((e for e in active_events(db, today) if e.code == code), None)
    if event is None:
        raise APIError(404, "EVENT_NOT_FOUND", "진행 중인 이벤트가 아니에요.")

    key = "stamp" if event.rule == catalog.EVENT_RULE_STAMP else "count"
    reward = next(
        (r for r in event.rewards if int(r.get(key) or 0) == threshold), None
    )
    row = _user_event(db, profile.user_id, event.code)
    if reward is None or threshold <= 0 or row.progress < threshold:
        raise APIError(409, "EVENT_REWARD_NOT_REACHED", "아직 받을 수 없는 보상이에요.")
    if threshold in set(row.claimed_thresholds or []):
        raise APIError(409, "EVENT_REWARD_ALREADY_CLAIMED", "이미 받은 보상이에요.")

    item_code = reward.get("item_code")
    items = ensure_catalog(db)
    item = items.get(item_code)
    if item is None:  # pragma: no cover — 카탈로그에서 사라진 코드
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "존재하지 않는 아이템입니다.")

    if not ledger(
        db,
        profile.user_id,
        key=f"event:{event.code}:stamp:{threshold}",
        reason=CLAIM_REASON,
        ref_type="catalog_item",
        ref_id=item_code,
        logical_date=today,
    ):
        raise APIError(409, "EVENT_REWARD_ALREADY_CLAIMED", "이미 받은 보상이에요.")

    grant_item(db, profile.user_id, item.id, "event")
    row.claimed_thresholds = sorted({*(row.claimed_thresholds or []), threshold})
    return {
        "event_code": event.code,
        "item_code": item_code,
        "name": item.name,
        "progress": row.progress,
    }


__all__ = [
    "apply_event_stamps",
    "apply_mission_completion",
    "claim_reward",
    "claimable",
    "ensure_event_catalog",
    "list_events",
    "user_events",
]
