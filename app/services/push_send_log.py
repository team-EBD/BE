"""서버 푸시 발송 이력 기록 — '하루 한 번'을 DB 유일 제약으로 보장한다.

발송 직전에 claim_push_send 로 (user_id, channel, logical_date) 행을 커밋한다.
유일 제약 위반이면 같은 날 이미 다른 인스턴스·이전 프로세스가 보낸 것이므로 건너뛴다.
행을 먼저 남기므로 발송 자체가 실패해도 그날 다시 보내지 않는다(최대 1회).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timeutil import now_utc
from app.models import PushSendLog


def claim_push_send(
    db: Session,
    *,
    user_id: int,
    channel: str,
    kind: str,
    template_id: str,
    logical_date: date,
    now: datetime | None = None,
) -> bool:
    """발송 이력을 먼저 커밋한다. True 면 보내도 된다, False 면 이미 보낸 날이다."""
    db.add(
        PushSendLog(
            user_id=user_id,
            channel=channel,
            kind=kind,
            template_id=template_id,
            logical_date=logical_date,
            sent_at=now or now_utc(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def sent_logs_since(
    db: Session, user_ids: list[int], channel: str, since: date
) -> list[PushSendLog]:
    """채널별 발송 이력 중 logical_date >= since 인 것 (템플릿 회전·재발송 간격 판단용)."""
    if not user_ids:
        return []
    return list(
        db.scalars(
            select(PushSendLog).where(
                PushSendLog.user_id.in_(user_ids),
                PushSendLog.channel == channel,
                PushSendLog.logical_date >= since,
            )
        ).all()
    )
