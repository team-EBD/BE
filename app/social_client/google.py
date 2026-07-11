"""구글 id_token 검증.

MVP 는 구글 tokeninfo 엔드포인트로 검증한다(공개키 캐싱 없이 단순·확실).
트래픽이 커지면 JWKS 공개키 캐싱 방식으로 교체한다.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import APIError

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def verify_id_token(id_token: str) -> dict[str, Any]:
    """구글 id_token 을 검증하고 클레임(sub/email/name/picture)을 반환한다.

    - 검증 실패/만료: 401 UNAUTHORIZED
    - 구글 서버 통신 실패: 502 AI_PROVIDER_ERROR (명세서 3.1 — 외부 provider 오류 코드 재사용)
    """
    try:
        res = httpx.get(TOKENINFO_URL, params={"id_token": id_token}, timeout=10.0)
    except httpx.HTTPError as exc:
        raise APIError(502, "AI_PROVIDER_ERROR", "소셜 제공자 서버와 통신에 실패했습니다.") from exc

    if res.status_code != 200:
        raise APIError(401, "UNAUTHORIZED", "소셜 토큰 검증에 실패했습니다.")

    claims: dict[str, Any] = res.json()
    if "sub" not in claims:
        raise APIError(401, "UNAUTHORIZED", "소셜 토큰 검증에 실패했습니다.")

    # GOOGLE_CLIENT_ID 가 설정된 경우 우리 앱으로 발급된 토큰인지 확인
    if settings.google_client_id and claims.get("aud") != settings.google_client_id:
        raise APIError(401, "UNAUTHORIZED", "이 앱을 위해 발급된 토큰이 아닙니다.")

    return claims
