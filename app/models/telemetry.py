"""계측(텔레메트리) 도메인 모델.

- request_logs: 모든 API 요청의 처리 시간 (타이밍 미들웨어가 기록)
- client_events: FE 가 측정한 구간 시간·행동 이벤트 (POST /client-events)
  ai_call_log_id 로 서버측 AI 호출 기록과 조인하면 한 요청의
  FE 체감 시간 / BE 전체 시간 / AI 내부 시간을 분해할 수 있다.
"""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import created_at_column, pk_column


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = pk_column()
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class ClientEvent(Base):
    __tablename__ = "client_events"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 예: analyze_flow / recommend_flow / meal_saved / analyze_abandoned
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ai_call_log_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_call_logs.id", ondelete="SET NULL"), nullable=True
    )
    # 사용자 체감 총 시간 (버튼 탭 → 화면 표시)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 구간 분해·상태 등 부가 정보 (upload_ms, status, from_cache ...)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
