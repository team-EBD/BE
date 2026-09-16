"""추천 행동 로그 — 노출·채택·섭취를 recommendation_items 행으로 남기고, 랭킹이 다시 읽는다.

docs/음식군-DB-계약.md §2.5 · §3 J. 한 번의 추천(recommendation_logs 1행)에 카드 3장이
recommendation_items 3행으로 붙는다. 출처별 섭취율이 GROUP BY 한 줄로 나온다.
recommendation_logs.recommended_items JSON 에는 이름·출처만 가볍게 남긴다 (기존 조회 호환).

섭취 판정은 추론이다 — 추천 후 EATEN_WINDOW_HOURS 안에 저장된 기록에 추천과 같은 군(없으면
같은 정규화 이름)이 있으면 "먹었다"로 본다. 명시 신호(카드 탭 = 채택)와 구분해 따로 남긴다.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import from_db, now_utc
from app.models import RecommendationItem, RecommendationLog
from app.services.matching import normalize_name

from .groups import GroupIndex, load_group_index

if TYPE_CHECKING:  # 순환 import 방지 — 타입 힌트에만 쓴다
    from .engine import RecommendationResult

logger = logging.getLogger(__name__)

# 추천 후 이 시간 안에 저장된 기록만 "추천을 먹은 것"으로 본다
EATEN_WINDOW_HOURS = 4
# 채택률 계산 구간과, 비율을 신뢰하기 위한 최소 노출 수
ACCEPTANCE_WINDOW_DAYS = 60
MIN_EXPOSURES_FOR_RATE = 3


def _row_key(row: RecommendationItem, index: GroupIndex) -> str:
    """항목의 비교 키 — 군이 있으면 군 키, 없으면 정규화 이름 (엔진 후보 키와 같은 규칙)."""
    if row.food_group_id is not None and row.food_group_id in index.by_id:
        return index.by_id[row.food_group_id].key
    return normalize_name(row.name)


def log_exposure(
    db: Session,
    user_id: int,
    result: RecommendationResult,
    *,
    ai_call_log_id: int | None = None,
    commit: bool = True,
) -> RecommendationLog:
    """추천 노출을 기록하고 로그 행을 돌려준다. 반환된 id 를 FE 가 채택 신고에 쓴다."""
    budget = result.budget
    log = RecommendationLog(
        user_id=user_id,
        ai_call_log_id=ai_call_log_id,
        meal_context={
            "meal_type": result.meal_type,
            "mood": result.mood,
            "meal_budget": budget.meal_budget,
            "remaining_today": budget.remaining_today,
            "ratio_source": budget.ratio_source,
            "protein_gap": budget.protein_gap,
            "candidate_count": len(result.candidates),
            "anchors": result.anchors,
            "groups_enabled": result.groups_enabled,
            "engine": "v2",
        },
        preferred_category=result.mood,
        recommendation_summary=f"{result.meal_type} 추천 {len(result.items)}건",
        recommended_items=[
            {"name": item.name, "source": item.source, "group_id": item.group_id}
            for item in result.items
        ],
    )
    db.add(log)
    db.flush()
    for rank, item in enumerate(result.items, start=1):
        db.add(
            RecommendationItem(
                log_id=log.id,
                food_group_id=item.group_id,
                name=item.name[:100],
                source=item.source,
                rank=rank,
                score=item.score,
            )
        )
    db.flush()
    if commit:
        db.commit()
    return log


def mark_accepted(
    db: Session,
    user_id: int,
    log_id: int,
    key_or_name: str,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> bool:
    """추천 카드를 탭했을 때(명시 채택). 같은 항목을 다시 눌러도 시각은 처음 것을 유지한다."""
    log = db.get(RecommendationLog, log_id)
    if log is None or log.user_id != user_id:
        return False
    index = load_group_index(db)
    target = index.key_for(key_or_name)
    raw = normalize_name(key_or_name)
    rows = db.scalars(select(RecommendationItem).where(RecommendationItem.log_id == log_id)).all()
    for row in rows:
        if _row_key(row, index) in (target, raw) or normalize_name(row.name) == raw:
            if row.accepted_at is None:
                row.accepted_at = now or now_utc()
            if commit:
                db.commit()
            return True
    return False


def mark_eaten(
    db: Session,
    user_id: int,
    meal_record_id: int,
    food_names: list[str],
    *,
    food_group_ids: list[int | None] | None = None,
    now: datetime | None = None,
    window_hours: int = EATEN_WINDOW_HOURS,
) -> int:
    """저장된 기록이 최근 추천과 겹치면 그 항목에 섭취 표시. 표시한 항목 수를 반환한다.

    기록 저장 흐름에서 호출되므로 어떤 이유로 실패해도 저장을 막지 않는다(예외 삼킴).
    커밋은 호출자(create_meal)의 트랜잭션에 맡긴다.
    """
    now = now or now_utc()
    try:
        index = load_group_index(db)
        keys = {index.key_for(name) for name in food_names if name}
        for gid in food_group_ids or []:
            if gid is not None and gid in index.by_id:
                keys.add(index.by_id[gid].key)
        keys.discard("")
        if not keys:
            return 0
        rows = db.scalars(
            select(RecommendationItem)
            .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
            .where(
                RecommendationLog.user_id == user_id,
                RecommendationLog.created_at >= now - timedelta(hours=window_hours),
                RecommendationItem.eaten_at.is_(None),
            )
        ).all()
        marked = 0
        for row in rows:
            if _row_key(row, index) in keys:
                row.eaten_at = now
                row.eaten_meal_record_id = meal_record_id
                marked += 1
        if marked:
            db.flush()
        return marked
    except Exception:  # noqa: BLE001 - 기록 저장을 막지 않는다
        logger.exception("추천 섭취 판정 실패 (user_id=%s, meal_record_id=%s)", user_id, meal_record_id)
        return 0


def _items_in_window(
    db: Session, *, now: datetime, window_days: int, user_id: int | None
) -> list[RecommendationItem]:
    stmt = (
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(RecommendationLog.created_at >= now - timedelta(days=window_days))
    )
    if user_id is not None:
        stmt = stmt.where(RecommendationLog.user_id == user_id)
    return db.scalars(stmt).all()


def acceptance_rates(
    db: Session,
    user_id: int | None = None,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
    min_exposures: int = MIN_EXPOSURES_FOR_RATE,
) -> dict[str, float]:
    """음식 키 → 섭취·채택율(0~1). 노출이 min_exposures 미만인 키는 빼서 랭킹의 기본값을 쓰게 한다.

    user_id 를 주면 그 사용자 것만, 없으면 전체. 랭킹의 accept 항 입력이다.
    """
    now = now or now_utc()
    index = load_group_index(db)
    shown: dict[str, int] = defaultdict(int)
    hit: dict[str, int] = defaultdict(int)
    for row in _items_in_window(db, now=now, window_days=window_days, user_id=user_id):
        key = _row_key(row, index)
        shown[key] += 1
        if row.eaten_at is not None or row.accepted_at is not None:
            hit[key] += 1
    return {
        key: round(hit[key] / count, 3) for key, count in shown.items() if count >= min_exposures
    }


def source_stats(
    db: Session,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
) -> dict[str, dict[str, int]]:
    """출처(personal/popular/similar)별 노출·채택·거절·섭취 수 — 생성기 가치 판단용."""
    now = now or now_utc()
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"shown": 0, "accepted": 0, "rejected": 0, "eaten": 0}
    )
    for row in _items_in_window(db, now=now, window_days=window_days, user_id=None):
        bucket = stats[row.source]
        bucket["shown"] += 1
        if row.accepted_at is not None:
            bucket["accepted"] += 1
        if row.rejected_at is not None:
            bucket["rejected"] += 1
        if row.eaten_at is not None:
            bucket["eaten"] += 1
    return dict(stats)


def last_exposure(db: Session, user_id: int, *, now: datetime | None = None) -> datetime | None:
    """가장 최근 추천 노출 시각 (디버깅·미리보기용)."""
    log = db.scalars(
        select(RecommendationLog)
        .where(RecommendationLog.user_id == user_id)
        .order_by(RecommendationLog.id.desc())
        .limit(1)
    ).first()
    return from_db(log.created_at) if log else None
