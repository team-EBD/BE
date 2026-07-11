"""공통 의존성 (Phase 2).

- get_current_user: Bearer 토큰 → User. 만료는 401 TOKEN_EXPIRED,
  그 외 무효/누락은 401 UNAUTHORIZED 로 구분한다(명세서 1.5).
- 소유권 체크는 각 서비스에서 user_id 대조로 수행한다(403 FORBIDDEN).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import APIError
from app.core.security import TokenError, TokenExpired, decode_token
from app.models import User


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise APIError(401, "UNAUTHORIZED", "인증 토큰이 필요합니다.")
    return token


def get_current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> User:
    token = _extract_bearer(request)
    try:
        payload = decode_token(token, expected_type="access")
    except TokenExpired:
        raise APIError(401, "TOKEN_EXPIRED", "Access Token이 만료되었습니다.")
    except TokenError:
        raise APIError(401, "UNAUTHORIZED", "유효하지 않은 토큰입니다.")

    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise APIError(401, "UNAUTHORIZED", "존재하지 않는 사용자입니다.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DB = Annotated[Session, Depends(get_db)]
