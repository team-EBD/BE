"""푸시 토큰 라우터 (Phase 10, 명세서 12.2).

MVP 범위는 토큰 등록까지 — 실제 발송(FCM)은 이후 과제.
(user_id, device_id) 기준 upsert 로 재등록에도 안전하다.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DB, CurrentUser
from app.models import PushToken
from app.schemas.user import PushTokenRequest, PushTokenResponse

router = APIRouter(prefix="/push-tokens", tags=["notifications"])


@router.post("", response_model=PushTokenResponse, status_code=201)
def register_push_token(
    body: PushTokenRequest, user: CurrentUser, db: DB
) -> PushTokenResponse:
    token = db.scalar(
        select(PushToken).where(
            PushToken.user_id == user.id, PushToken.device_id == body.device_id
        )
    )
    if token is None:
        token = PushToken(
            user_id=user.id,
            device_id=body.device_id,
            push_token=body.push_token,
            platform=body.platform,
        )
        db.add(token)
    else:
        token.push_token = body.push_token
        token.platform = body.platform
    db.commit()
    return PushTokenResponse(push_token_id=token.id)
