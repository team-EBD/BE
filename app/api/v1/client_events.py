"""클라이언트 계측 이벤트 수집 (POST /client-events).

FE 가 측정한 구간 시간(업로드/분석/렌더)과 행동 이벤트를 저장한다.
ai_call_log_id 를 함께 보내면 서버측 AI 호출 기록과 조인해
FE 체감 시간 / BE 전체 시간 / AI 내부 시간을 분해할 수 있다.

개인정보 원칙: 음식명·사진 등 내용 데이터는 받지 않는다 —
시간(ms)·단계·성공 여부 같은 메타데이터만 저장한다.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.deps import DB, CurrentUser
from app.models import ClientEvent

router = APIRouter(prefix="/client-events", tags=["telemetry"])

# meta 필드 폭주 방지 상한 (키 수 / 직렬화 길이)
_META_MAX_KEYS = 20


class ClientEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=40)
    duration_ms: int | None = Field(default=None, ge=0, le=10 * 60 * 1000)
    ai_call_log_id: int | None = None
    meta: dict | None = None


class ClientEventResponse(BaseModel):
    client_event_id: int


@router.post("", response_model=ClientEventResponse, status_code=201)
def create_client_event(
    body: ClientEventRequest, user: CurrentUser, db: DB
) -> ClientEventResponse:
    meta = body.meta
    if meta is not None and len(meta) > _META_MAX_KEYS:
        # 초과분은 조용히 버리는 대신 상한까지만 저장 (계측이 실패해선 안 됨)
        meta = dict(list(meta.items())[:_META_MAX_KEYS])
    event = ClientEvent(
        user_id=user.id,
        event_type=body.event_type,
        ai_call_log_id=body.ai_call_log_id,
        duration_ms=body.duration_ms,
        meta=meta,
    )
    db.add(event)
    db.commit()
    return ClientEventResponse(client_event_id=event.id)
