"""일일 AI 사용량 제한 (분석/추천).

사용자당 하루(KST 기준)에 허용되는 AI 분석/추천 횟수를 제한한다.
- 집계 원천은 ai_call_logs (별도 카운터 테이블 없이 기존 로그를 사용).
- 프리미엄 구독자는 별도 한도(analyze/recommend_daily_limit_premium, 기본 무제한)를
  적용한다 — 한도 계산에 들어가는 것은 '구독 여부'뿐이고 결제 검증은 하지 않는다.
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
from app.models import AiCallLog
from app.services.subscription import is_premium

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
                AiCallLog.task_type == task_type,
                AiCallLog.status == "success",
                AiCallLog.created_at >= start,
                AiCallLog.created_at < end,
            )
        )
        or 0
    )


def enforce_daily_limit(db: Session, user_id: int, task_type: str) -> None:
    """한도 초과 시 429. limit 이 0 이하면 무제한(비활성화)."""
    premium = is_premium(db, user_id)
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
