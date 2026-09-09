"""추천 행동 로그 — 노출·채택·섭취를 남기고, 랭킹이 다시 읽는다.

DB 스키마 변경 없이 recommendation_logs 의 기존 JSON 컬럼만 쓴다:
  meal_context      추천 시점 입력 (끼니·mood·예산·단백질 갭)
  recommended_items 항목별 {key, name, source, score, reason, …}
                    + 사후에 채워지는 accepted_at / eaten_at / eaten_meal_record_id
전용 컬럼(accepted_at 등)이 생기면 이 모듈만 바꾸면 된다.

섭취 판정은 추론이다 — 추천 후 EATEN_WINDOW_HOURS 안에 저장된 기록에 추천과 같은
음식 키가 있으면 "먹었다"로 본다. 명시 신호(카드 탭 = 채택)와 구분해 따로 남긴다.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.timeutil import from_db, now_utc
from app.models import RecommendationLog

from .signals import group_key

if TYPE_CHECKING:  # 순환 import 방지 — 타입 힌트에만 쓴다
    from .engine import RecommendationResult

logger = logging.getLogger(__name__)

# 추천 후 이 시간 안에 저장된 기록만 "추천을 먹은 것"으로 본다
EATEN_WINDOW_HOURS = 4
# 채택률 계산 구간과, 비율을 신뢰하기 위한 최소 노출 수
ACCEPTANCE_WINDOW_DAYS = 60
MIN_EXPOSURES_FOR_RATE = 3


def _items_of(log: RecommendationLog) -> list[dict[str, Any]]:
    items = log.recommended_items
    return list(items) if isinstance(items, list) else []


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
            "engine": "v2",
        },
        preferred_category=result.mood,
        recommendation_summary=f"{result.meal_type} 추천 {len(result.items)}건",
        recommended_items=[
            {
                "key": item.key,
                "name": item.name,
                "calories": item.calories,
                "protein": item.protein,
                "source": item.source,
                "budget_label": item.budget_label,
                "score": item.score,
                "parts": item.parts,
                "reason": item.reason,
                "accepted_at": None,
                "eaten_at": None,
                "eaten_meal_record_id": None,
            }
            for item in result.items
        ],
    )
    db.add(log)
    db.flush()
    if commit:
        db.commit()
    return log


def mark_accepted(
    db: Session,
    user_id: int,
    log_id: int,
    key: str,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> bool:
    """추천 카드를 탭했을 때(명시 채택). 같은 항목을 다시 눌러도 시각은 처음 것을 유지한다."""
    log = db.get(RecommendationLog, log_id)
    if log is None or log.user_id != user_id:
        return False
    normalized = group_key(key)
    items = _items_of(log)
    for item in items:
        if item.get("key") == normalized or group_key(str(item.get("name", ""))) == normalized:
            if item.get("accepted_at") is None:
                item["accepted_at"] = (now or now_utc()).isoformat()
            log.recommended_items = items
            flag_modified(log, "recommended_items")
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
    now: datetime | None = None,
    window_hours: int = EATEN_WINDOW_HOURS,
) -> int:
    """저장된 기록이 최근 추천과 겹치면 그 항목에 섭취 표시. 표시한 항목 수를 반환한다.

    기록 저장 흐름에서 호출되므로 어떤 이유로 실패해도 저장을 막지 않는다(예외 삼킴).
    커밋은 호출자(create_meal)의 트랜잭션에 맡긴다.
    """
    now = now or now_utc()
    keys = {group_key(name) for name in food_names if group_key(name)}
    if not keys:
        return 0
    try:
        logs = db.scalars(
            select(RecommendationLog)
            .where(
                RecommendationLog.user_id == user_id,
                RecommendationLog.created_at >= now - timedelta(hours=window_hours),
            )
            .order_by(RecommendationLog.id.desc())
        ).all()
        marked = 0
        for log in logs:
            items = _items_of(log)
            touched = False
            for item in items:
                if item.get("eaten_at") is not None or item.get("key") not in keys:
                    continue
                item["eaten_at"] = now.isoformat()
                item["eaten_meal_record_id"] = meal_record_id
                touched = True
                marked += 1
            if touched:
                log.recommended_items = items
                flag_modified(log, "recommended_items")
        return marked
    except Exception:  # noqa: BLE001 - 기록 저장을 막지 않는다
        logger.warning("추천 섭취 판정 실패 (user_id=%s, meal_record_id=%s)", user_id, meal_record_id)
        return 0


def acceptance_rates(
    db: Session,
    user_id: int | None = None,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
    min_exposures: int = MIN_EXPOSURES_FOR_RATE,
) -> dict[str, float]:
    """음식 키 → 섭취율(0~1). 노출이 min_exposures 미만인 키는 빼서 랭킹의 기본값을 쓰게 한다.

    user_id 를 주면 그 사용자 것만, 없으면 전체. 랭킹의 accept 항 입력이다.
    """
    now = now or now_utc()
    stmt = select(RecommendationLog).where(
        RecommendationLog.created_at >= now - timedelta(days=window_days)
    )
    if user_id is not None:
        stmt = stmt.where(RecommendationLog.user_id == user_id)

    shown: dict[str, int] = defaultdict(int)
    eaten: dict[str, int] = defaultdict(int)
    for log in db.scalars(stmt):
        for item in _items_of(log):
            key = item.get("key")
            if not key:
                continue
            shown[key] += 1
            if item.get("eaten_at") or item.get("accepted_at"):
                eaten[key] += 1
    return {
        key: round(eaten.get(key, 0) / count, 3)
        for key, count in shown.items()
        if count >= min_exposures
    }


def source_stats(
    db: Session,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
) -> dict[str, dict[str, int]]:
    """출처(personal/popular/similar)별 노출·채택·섭취 수 — 생성기 가치 판단용."""
    now = now or now_utc()
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"shown": 0, "accepted": 0, "eaten": 0}
    )
    logs = db.scalars(
        select(RecommendationLog).where(
            RecommendationLog.created_at >= now - timedelta(days=window_days)
        )
    )
    for log in logs:
        for item in _items_of(log):
            bucket = stats[str(item.get("source", "unknown"))]
            bucket["shown"] += 1
            if item.get("accepted_at"):
                bucket["accepted"] += 1
            if item.get("eaten_at"):
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
