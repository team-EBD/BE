"""자체 JWT 발급/검증 (Phase 2).

- Access(기본 30분) / Refresh(기본 14일). 페이로드 `type` 으로 구분한다.
- Refresh 토큰은 원문을 저장하지 않고 SHA-256 해시만 refresh_tokens 에 남긴다.
- 비밀번호 해싱은 없다(소셜 로그인 전용).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import settings
from app.core.timeutil import now_utc


class TokenError(Exception):
    """서명 불일치/형식 오류 등 유효하지 않은 토큰."""


class TokenExpired(TokenError):
    """만료된 토큰."""


def _create_token(user_id: int, token_type: str, expires_delta: timedelta) -> str:
    now = now_utc()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: int) -> str:
    return _create_token(
        user_id, "access", timedelta(minutes=settings.access_token_expire_minutes)
    )


def create_refresh_token(user_id: int) -> str:
    return _create_token(
        user_id, "refresh", timedelta(days=settings.refresh_token_expire_days)
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """토큰 검증. 만료는 TokenExpired, 그 외 무효는 TokenError 를 던진다."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except ExpiredSignatureError as exc:
        raise TokenExpired("token expired") from exc
    except JWTError as exc:
        raise TokenError("invalid token") from exc
    if payload.get("type") != expected_type or "sub" not in payload:
        raise TokenError("invalid token type")
    return payload


def hash_token(token: str) -> str:
    """refresh_tokens.token_hash 저장용 SHA-256 hex."""
    return hashlib.sha256(token.encode()).hexdigest()
