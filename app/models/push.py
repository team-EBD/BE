"""서버 푸시 발송 이력.

- push_send_logs: 사용자·채널·논리 날짜(KST, day_start_hour 경계)마다 한 줄.
  (user_id, channel, logical_date) 유일 제약이 '하루 한 번' 상한 그 자체다 —
  발송 **전에** 이 행을 커밋하고, 유일 제약 위반이면 이미 다른 인스턴스·이전 프로세스가
  보낸 것으로 보고 건너뛴다 (app/services/push_send_log.py). 그래서 재기동·다중 인스턴스에서도
  같은 날 두 번 나가지 않는다. 반대로 행을 남긴 뒤 발송이 실패하면 그날은 다시 보내지 않는다
  (최대 1회 — 두 번 받는 것보다 한 번 놓치는 편이 낫다).
"""
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import TZDateTime, pk_column

# channel 값
PUSH_CHANNEL_LIFECYCLE = "lifecycle"
PUSH_CHANNEL_WEEKLY_REPORT = "weekly_report"


class PushSendLog(Base):
    __tablename__ = "push_send_logs"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "channel", "logical_date", name="uq_push_send_logs_user_channel_date"
        ),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # lifecycle/weekly_report
    # 세그먼트·문구 분류 (lifecycle: first_log/missed_one/keep_going/lapsed)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    template_id: Mapped[str] = mapped_column(String(40), nullable=False)
    logical_date: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)
