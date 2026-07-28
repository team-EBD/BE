"""Apple identityToken 검증 (Sign in with Apple).

iOS 네이티브 인증이 발급한 identityToken(JWT)을 Apple 공개키(JWKS)로 검증한다.
- 서명: https://appleid.apple.com/auth/keys 공개키 (kid 매칭, 1시간 캐시)
- iss: https://appleid.apple.com
- aud: 앱 번들 ID (APPLE_BUNDLE_ID 설정 시 검증 — 미설정이면 생략, 운영에선 필수 설정)

Apple 특성:
- 이메일 클레임은 최초 인증 1회만 포함될 수 있고, 사용자가 '이메일 가리기'를
  선택하면 @privaterelay.appleid.com 릴레이 주소가 온다.
- 이름(fullName)은 identityToken 에 아예 없다 — FE 가 최초 인증 응답에서
  별도 필드(name)로 전달한다 (auth 라우터에서 처리).
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import APIError

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"

_JWKS_TTL_SECONDS = 60 * 60
_jwks_cache: dict | None = None
_jwks_cached_at: float = 0.0


def reset_jwks_cache() -> None:
    """키 롤오버 대응·테스트용 캐시 초기화."""
    global _jwks_cache
    _jwks_cache = None


def _fetch_jwks() -> dict:
    global _jwks_cache, _jwks_cached_at
    now = time.monotonic()
    if _jwks_cache is not None and now - _jwks_cached_at < _JWKS_TTL_SECONDS:
        return _jwks_cache
    try:
        res = httpx.get(APPLE_JWKS_URL, timeout=10.0)
        res.raise_for_status()
    except httpx.HTTPError as exc:
        raise APIError(
            502, "AI_PROVIDER_ERROR", "소셜 제공자 서버와 통신에 실패했습니다."
        ) from exc
    _jwks_cache = res.json()
    _jwks_cached_at = now
    return _jwks_cache


def _find_key(kid: str | None) -> dict | None:
    jwks = _fetch_jwks()
    return next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)


_INVALID = APIError(401, "UNAUTHORIZED", "소셜 토큰 검증에 실패했습니다.")


def verify_identity_token(identity_token: str) -> dict[str, Any]:
    """identityToken 을 검증하고 클레임(sub/email 등)을 반환한다."""
    try:
        header = jwt.get_unverified_header(identity_token)
    except JWTError as exc:
        raise _INVALID from exc

    key = _find_key(header.get("kid"))
    if key is None:
        # Apple 키 롤오버 직후일 수 있으므로 캐시를 비우고 1회 재조회
        reset_jwks_cache()
        key = _find_key(header.get("kid"))
        if key is None:
            raise _INVALID

    bundle_id = settings.apple_bundle_id
    try:
        claims: dict[str, Any] = jwt.decode(
            identity_token,
            key,
            algorithms=["RS256"],
            issuer=APPLE_ISSUER,
            audience=bundle_id if bundle_id else None,
            options={"verify_aud": bool(bundle_id)},
        )
    except JWTError as exc:
        raise _INVALID from exc

    if "sub" not in claims:
        raise _INVALID
    return claims
