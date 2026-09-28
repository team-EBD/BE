"""추천 행동 로그 — 노출·채택·섭취를 recommendation_items 행으로 남기고, 랭킹이 다시 읽는다.

docs/음식군-DB-계약.md §2.5 · §3 J. 한 번의 추천(recommendation_logs 1행)에 카드 3장이
recommendation_items 3행으로 붙는다. 출처별 섭취율이 GROUP BY 한 줄로 나온다.
recommendation_logs.recommended_items JSON 에는 이름·출처만 가볍게 남긴다 (기존 조회 호환).

생성 로그는 노출이 아니다. 실제 화면 노출 신고만 shown_at 에 남긴다.
섭취는 카드 ID로 연결한 식사의 실제 시각·음식·소유권을 검증하며, 기록 시작 탭과 구분한다.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.timeutil import from_db, now_utc
from app.models import MealItem, MealRecord, RecommendationItem, RecommendationLog, User
from app.services.matching import normalize_name

from .groups import GroupIndex, load_group_index

if TYPE_CHECKING:  # 순환 import 방지 — 타입 힌트에만 쓴다
    from .engine import RecommendationResult

# 화면 노출 후 이 시간 안의 실제 식사만 추천에 연결한다.
EATEN_WINDOW_HOURS = 4
# DB 시각과 앱 시각의 허용 오차 — 이보다 앞선 created_at 만 '미래 로그'로 본다
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
# 노출 시각보다 이만큼 앞선 eaten_at 까지는 '추천 보고 먹은 것'으로 귀속한다 (분 단위 반올림·단말 시계 오차)
ATTRIBUTION_GRACE = timedelta(minutes=5)
# 채택률 계산 구간과, 비율을 신뢰하기 위한 최소 노출 수
ACCEPTANCE_WINDOW_DAYS = 60
MIN_EXPOSURES_FOR_RATE = 3
DISLIKE_WINDOW_DAYS = 90
NOT_NOW_WINDOW_HOURS = 4


def _row_key(row: RecommendationItem, index: GroupIndex) -> str:
    """항목의 비교 키 — 군이 있으면 군 키, 없으면 정규화 이름 (엔진 후보 키와 같은 규칙)."""
    return index.key_for(row.name, row.food_group_id)


def log_exposure(
    db: Session,
    user_id: int,
    result: RecommendationResult,
    *,
    ai_call_log_id: int | None = None,
    commit: bool = True,
) -> RecommendationLog:
    """추천 생성·정책 스냅샷을 기록한다. 실제 노출은 record_feedback 에서 따로 받는다.

    함수명은 기존 호출 호환용이며, 여기서는 shown_at 을 설정하지 않는다.
    """
    budget = result.budget
    log = RecommendationLog(
        user_id=user_id,
        ai_call_log_id=ai_call_log_id,
        decision=deepcopy(getattr(result, "decision", None)),
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
                features=deepcopy(getattr(item, "features", None)),
                selection_probability=getattr(item, "selection_probability", None),
                policy_version=(getattr(result, "decision", None) or {}).get("policy_version"),
            )
        )
    db.flush()
    if commit:
        db.commit()
    return log


def _owned_item(db: Session, user_id: int, item_id: int) -> RecommendationItem | None:
    return db.scalar(
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(RecommendationItem.id == item_id, RecommendationLog.user_id == user_id)
        .with_for_update(of=RecommendationItem)
    )


def record_feedback(
    db: Session, user_id: int, item_id: int, action: str, *, reason: str | None = None,
    now: datetime | None = None, commit: bool = True,
) -> bool:
    """소유한 카드의 명시 행동. 같은 요청 재시도는 원래 시각을 유지한다."""
    if action not in {"impression", "accept", "reject"}:
        raise ValueError("invalid feedback action")
    if (action == "reject" and reason not in {"not_now", "dislike"}) or (action != "reject" and reason):
        raise ValueError("invalid feedback reason")
    row = _owned_item(db, user_id, item_id)
    if row is None:
        return False
    now = now or now_utc()
    log = db.get(RecommendationLog, row.log_id)
    # created_at 은 DB 시각(server_default), now 는 앱 시각 — 운영은 앱(EC2)과 DB 가 다른 호스트라
    # 몇 초 어긋날 수 있다. 미래 로그(조작·테스트의 고정 now)만 거르고 시계 오차는 허용한다.
    if from_db(log.created_at) > now + CLOCK_SKEW_TOLERANCE:
        return False
    # 탭·거절은 실제 카드를 본 명시 신호다. 노출 요청과의 경합/구버전도 수용한다.
    if row.shown_at is None:
        row.shown_at = now
    if action == "accept" and row.accepted_at is None:
        row.accepted_at = now
    elif action == "reject" and (row.rejected_at is None or row.reject_reason != reason):
        row.rejected_at = now
        row.reject_reason = reason
    db.flush()
    if commit:
        db.commit()
    return True


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
            return record_feedback(db, user_id, row.id, "accept", now=now, commit=commit)
    return False


def mark_eaten(
    db: Session,
    user_id: int,
    meal_record_id: int,
    food_names: list[str] | None = None,
    *,
    food_group_ids: list[int | None] | None = None,
    recommendation_item_id: int | None = None,
    now: datetime | None = None,
    window_hours: int = EATEN_WINDOW_HOURS,
) -> int:
    """명시적으로 연결된 카드 1개만 검증한다. 식사 항목은 DB 스냅샷이 기준이다.

    food_names/food_group_ids 는 구 호출 호환용이며 보상 근거로 신뢰하지 않는다.
    ID가 없거나 부적합한 연결은 무시하여 식사 저장 자체를 막지 않는다.
    """
    if recommendation_item_id is None:
        return 0
    row = _owned_item(db, user_id, recommendation_item_id)
    meal = db.get(MealRecord, meal_record_id)
    if row is None or meal is None or meal.user_id != user_id:
        return 0
    if row.eaten_meal_record_id is not None:
        return 0  # 이미 처리한 식사·재시도 또는 다른 식사로 중복 보상 금지
    if db.scalar(select(RecommendationItem.id).where(RecommendationItem.eaten_meal_record_id == meal.id)):
        return 0
    if not _valid_attribution(db, row, meal, now=now or now_utc(), window_hours=window_hours):
        return 0
    row.eaten_meal_record_id = meal.id
    row.eaten_at = from_db(meal.eaten_at)
    db.flush()
    return 1


def _valid_attribution(
    db: Session, row: RecommendationItem, meal: MealRecord, *, now: datetime,
    window_hours: int = EATEN_WINDOW_HOURS,
) -> bool:
    if row.shown_at is None or meal.deleted_at is not None or meal.is_skipped:
        return False
    shown, eaten = from_db(row.shown_at), from_db(meal.eaten_at)
    # eaten_at 은 FE 가 분(시간 선택기)·초 단위로 내려 보내고 단말 시계도 조금 어긋난다. 노출 몇 초 뒤에
    # 저장해도 eaten < shown 이 되어 귀속이 통째로 빠지는 것을 운영 복제본 리허설에서 확인 — 앞쪽으로
    # ATTRIBUTION_GRACE 만큼은 '노출 직후'로 본다. 뒤쪽 상한은 노출 + 창(기본 4시간), 미래 기록은 시계 오차만 허용.
    if not shown - ATTRIBUTION_GRACE <= eaten <= min(now + CLOCK_SKEW_TOLERANCE, shown + timedelta(hours=window_hours)):
        return False
    index = load_group_index(db)
    target = index.resolve(row.name, row.food_group_id)
    for item in db.scalars(select(MealItem).where(MealItem.meal_record_id == meal.id)):
        actual = index.resolve(item.food_name, item.food_group_id)
        if target is not None and actual is not None:
            if target.id == actual.id:
                return True
        elif normalize_name(row.name) == normalize_name(item.food_name):
            return True
    return False


def reconcile_meal_feedback(db: Session, meal: MealRecord, *, now: datetime | None = None) -> None:
    """식사 수정·삭제에 맞춰 보상을 정정한다. 연결은 남겨 재수정도 검증할 수 있다."""
    now = now or now_utc()
    rows = db.scalars(
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(RecommendationItem.eaten_meal_record_id == meal.id, RecommendationLog.user_id == meal.user_id)
        .with_for_update(of=RecommendationItem)
    ).all()
    for row in rows:
        row.eaten_at = from_db(meal.eaten_at) if _valid_attribution(db, row, meal, now=now) else None
    db.flush()


def excluded_keys(
    db: Session, user_id: int, *, now: datetime, index: GroupIndex | None = None,
) -> set[str]:
    """명시 비선호는 90일, '지금은 다른 메뉴'는 4시간 동안 같은 음식군을 제외한다."""
    index = index or load_group_index(db)
    excluded: set[str] = set()
    rows = db.scalars(
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(
            RecommendationLog.user_id == user_id,
            RecommendationItem.shown_at <= now,
            RecommendationItem.rejected_at >= now - timedelta(days=DISLIKE_WINDOW_DAYS),
            RecommendationItem.rejected_at <= now,
        )
    )
    for row in rows:
        if _occurred(row.eaten_at, now) and from_db(row.eaten_at) > from_db(row.rejected_at):
            continue
        if row.reject_reason == "dislike" or (
            row.reject_reason == "not_now" and from_db(row.rejected_at) >= now - timedelta(hours=NOT_NOW_WINDOW_HOURS)
        ):
            excluded.add(_row_key(row, index))
    return excluded


def _items_in_window(
    db: Session, *, now: datetime, window_days: int, user_id: int | None,
    resolved_only: bool = False,
) -> list[RecommendationItem]:
    stmt = (
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(
            RecommendationItem.shown_at >= now - timedelta(days=window_days),
            RecommendationItem.shown_at <= now,
            RecommendationLog.created_at <= now,
        )
    )
    if user_id is not None:
        stmt = stmt.where(RecommendationLog.user_id == user_id)
    else:
        stmt = stmt.join(User, User.id == RecommendationLog.user_id).where(
            or_(User.email.is_(None), ~User.email.ilike("%@test.com"))
        )
    if resolved_only:
        # 보상 여부와 무관하게 같은 관측 시간을 준 뒤 비교한다.
        stmt = stmt.where(RecommendationItem.shown_at <= now - timedelta(hours=EATEN_WINDOW_HOURS))
    return db.scalars(stmt).all()


def _occurred(at: datetime | None, now: datetime) -> bool:
    return at is not None and from_db(at) <= now


def outcome_reward(row: RecommendationItem, *, now: datetime) -> float:
    """화면 노출 후 4시간 안의 검증 식사=1, 명시 거절=0, 기록 시작=0.2, 무응답=0.

    무응답은 기록 전환 미관측이며 비선호가 아니다. 이 함수 호출자는 노출 후 4시간이
    지난 표본만 사용한다. 규칙 순위의 반응 신호와 밴딧이 같은 보상 정의를 공유한다.
    """
    if row.shown_at is None:
        return 0.0
    shown = from_db(row.shown_at)
    end = min(now, shown + timedelta(hours=EATEN_WINDOW_HOURS))

    def within(at: datetime | None) -> bool:
        return at is not None and shown <= from_db(at) <= end

    if row.eaten_meal_record_id is not None and within(row.eaten_at):
        return 1.0
    if within(row.rejected_at):
        return 0.0
    return 0.2 if within(row.accepted_at) else 0.0


def acceptance_rates(
    db: Session,
    user_id: int | None = None,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
    min_exposures: int = MIN_EXPOSURES_FOR_RATE,
) -> dict[str, float]:
    """음식 키 → 평균 관측 보상(0~1). 최소 노출 미만인 음식은 랭킹 기본값을 쓴다.

    user_id 를 주면 그 사용자 것만, 없으면 전체. 랭킹의 accept 항 입력이다.
    실제 노출 후 4시간이 지난 표본만 사용하며 클릭은 실제 식사와 구분한다.
    """
    now = now or now_utc()
    index = load_group_index(db)
    shown: dict[str, int] = defaultdict(int)
    hit: dict[str, float] = defaultdict(float)
    for row in _items_in_window(db, now=now, window_days=window_days, user_id=user_id, resolved_only=True):
        key = _row_key(row, index)
        shown[key] += 1
        hit[key] += outcome_reward(row, now=now)
    return {
        key: round(hit[key] / count, 3) for key, count in shown.items() if count >= min_exposures
    }


def source_stats(
    db: Session,
    *,
    now: datetime | None = None,
    window_days: int = ACCEPTANCE_WINDOW_DAYS,
) -> dict[str, dict[str, int]]:
    """출처(personal/popular/similar/catalog)별 노출·채택·거절·섭취 수."""
    now = now or now_utc()
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"shown": 0, "accepted": 0, "rejected": 0, "eaten": 0}
    )
    for row in _items_in_window(db, now=now, window_days=window_days, user_id=None):
        bucket = stats[row.source]
        bucket["shown"] += 1
        if _occurred(row.accepted_at, now):
            bucket["accepted"] += 1
        if _occurred(row.rejected_at, now):
            bucket["rejected"] += 1
        if _occurred(row.eaten_at, now):
            bucket["eaten"] += 1
    return dict(stats)


def last_exposure(db: Session, user_id: int, *, now: datetime | None = None) -> datetime | None:
    """가장 최근 추천 노출 시각 (디버깅·미리보기용)."""
    row = db.scalars(
        select(RecommendationItem)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .where(
            RecommendationLog.user_id == user_id,
            RecommendationLog.created_at <= (now or now_utc()),
            RecommendationItem.shown_at <= (now or now_utc()),
        )
        .order_by(RecommendationItem.shown_at.desc(), RecommendationItem.id.desc())
        .limit(1)
    ).first()
    return from_db(row.shown_at) if row else None
