"""AI 사용량 제한 (분석/추천).

기본 off 에서는 사용자당 하루(KST) 분석/추천 한도를 각각 적용한다.
ai_premium_gate 가 켜지면 무료 사용자의 평생 성공 호출 10회를 통합 적용한다.
- 집계 원천은 ai_call_logs (별도 카운터 테이블 없이 기존 로그를 사용).
- off 에서 프리미엄 구독자는 별도 한도(analyze/recommend_daily_limit_premium,
  기본 무제한)를 적용한다. on 에서는 프리미엄 구독자가 무제한이다.
- 성공(status=success) 호출만 횟수로 센다 — AI 서버 장애/타임아웃으로
  실패한 시도는 사용자 귀책이 아니므로 차감하지 않는다.
- 한도 초과 시 429 TOO_MANY_REQUESTS (명세서 1.4 에러 봉투).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.core.timeutil import kst_date_of, kst_day_bounds, now_utc
from app.models import AI_LIMIT_EXEMPT_ROLES, AiCallLog, User
from app.services.subscription import is_premium

# 같은 기능인데 **비용 분리 계측** 때문에 task_type 을 나눠 기록하는 것들.
# 한도는 기능 단위로 세야 한다 — 그렇지 않으면 '발견 돋보기' 분석(analyze_clarifier)이
# 하루 한도를 통째로 우회한다.
TASK_TYPE_FAMILY: dict[str, tuple[str, ...]] = {
    "analyze": ("analyze", "analyze_clarifier"),
    "recommend": ("recommend",),
}
FREE_CREDIT_LIMIT = 10
FREE_CREDIT_TASK_TYPES = TASK_TYPE_FAMILY["analyze"] + TASK_TYPE_FAMILY["recommend"]


def task_types_for(task_type: str) -> tuple[str, ...]:
    """한도·사용량 집계에 함께 세야 하는 ai_call_logs.task_type 값들."""
    return TASK_TYPE_FAMILY.get(task_type, (task_type,))


# task_type 은 AI 서버 ai_call_log 계약값 (analyze/recommend)
_LIMIT_MESSAGES = {
    "analyze": "오늘 사용할 수 있는 음식 분석 횟수를 모두 사용했어요.",
    "recommend": "오늘 사용할 수 있는 추천 횟수를 모두 사용했어요.",
}
# 무료 사용자에게만 붙이는 안내 — 이미 구독 중인 사용자에게 구독을 권하면 안 된다
_FREE_SUFFIX = " 프리미엄으로 업그레이드하면 무제한으로 이용할 수 있어요."
_PREMIUM_SUFFIX = " 내일 다시 이용해주세요."


def daily_limit(task_type: str, premium: bool = False) -> int:
    """task_type 의 하루 한도. 0 이하면 무제한."""
    if task_type == "analyze":
        return settings.analyze_daily_limit_premium if premium else settings.analyze_daily_limit
    if task_type == "recommend":
        return settings.recommend_daily_limit_premium if premium else settings.recommend_daily_limit
    raise ValueError(f"unknown task_type: {task_type}")


def count_today_success(db: Session, user_id: int, task_type: str) -> int:
    """오늘(KST, day_start_hour 경계) 성공한 AI 호출 수.

    캘린더/요약과 동일하게 06시 경계를 쓴다 (settings.day_start_hour) —
    이전엔 자정 리셋이라 새벽 사용이 '오늘'과 '캘린더의 오늘'이 어긋났다.
    """
    dsh = settings.day_start_hour
    today = kst_date_of(now_utc(), dsh)
    start, end = kst_day_bounds(today, dsh)
    return int(
        db.scalar(
            select(func.count(AiCallLog.id)).where(
                AiCallLog.user_id == user_id,
                AiCallLog.task_type.in_(task_types_for(task_type)),
                AiCallLog.status == "success",
                AiCallLog.created_at >= start,
                AiCallLog.created_at < end,
            )
        )
        or 0
    )


def count_lifetime_success(db: Session, user_id: int) -> int:
    """분석·추천의 성공한 AI 호출 누적 수 (무료 크레딧 사용량)."""
    return int(
        db.scalar(
            select(func.count(AiCallLog.id)).where(
                AiCallLog.user_id == user_id,
                AiCallLog.task_type.in_(FREE_CREDIT_TASK_TYPES),
                AiCallLog.status == "success",
            )
        )
        or 0
    )


def is_limit_exempt(db: Session, user_id: int) -> bool:
    """AI 사용 한도를 받지 않는 계정인가 — users.role 이 tester/admin (UserRole 참고).

    테스터가 점검·디버깅 중에 한도에 막히지 않게 하기 위한 것이다. 구독 상태(is_premium)와는 별개라
    구독·결제 화면은 그대로 시험할 수 있다. 역할은 서버에서만 지정된다.
    """
    return db.scalar(select(User.role).where(User.id == user_id)) in AI_LIMIT_EXEMPT_ROLES


def enforce_daily_limit(db: Session, user_id: int, task_type: str) -> None:
    """플래그에 따른 AI 한도 초과 시 429. 한도 면제 역할(tester/admin)은 어떤 한도도 받지 않는다."""
    if is_limit_exempt(db, user_id):
        return
    premium = is_premium(db, user_id)
    if settings.ai_premium_gate:
        if premium:
            return
        used = count_lifetime_success(db, user_id)
        if used >= FREE_CREDIT_LIMIT:
            raise APIError(
                429,
                "TOO_MANY_REQUESTS",
                "무료 AI 사용권 10회를 모두 사용했어요." + _FREE_SUFFIX,
                details=[
                    {
                        "field": task_type,
                        "reason": "free_credit_exhausted",
                        "limit": FREE_CREDIT_LIMIT,
                        "used": used,
                        "upgradable": True,
                    }
                ],
            )
        return

    limit = daily_limit(task_type, premium=premium)
    if limit <= 0:
        return
    used = count_today_success(db, user_id, task_type)
    if used >= limit:
        raise APIError(
            429,
            "TOO_MANY_REQUESTS",
            _LIMIT_MESSAGES[task_type] + (_PREMIUM_SUFFIX if premium else _FREE_SUFFIX),
            details=[
                {
                    "field": task_type,
                    "reason": "daily_limit_exceeded",
                    "limit": limit,
                    "used": used,
                    # FE 가 429 화면에서 구독 유도 버튼을 띄울지 판단한다
                    "upgradable": not premium,
                }
            ],
        )
