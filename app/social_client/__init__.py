"""소셜 제공자 토큰 검증 (Phase 2).

`provider` 스위치로 카카오·애플 확장 여지를 둔다. 백엔드는 각 제공자의
OAuth *클라이언트*일 뿐이며 자체 인가 서버를 만들지 않는다(아키텍처 4.1).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import APIError
from app.social_client import apple, google


@dataclass
class SocialIdentity:
    provider: str
    social_id: str
    email: str | None
    nickname: str
    profile_image_url: str | None


def verify_social_token(provider: str, token: str) -> SocialIdentity:
    """제공자 토큰을 검증하고 식별정보를 반환한다. 실패 시 APIError.

    지원: google, apple. (확장 예정: kakao)
    """
    if provider == "google":
        info = google.verify_id_token(token)
        return SocialIdentity(
            provider="google",
            social_id=info["sub"],
            email=info.get("email"),
            nickname=info.get("name") or (info.get("email") or "사용자").split("@")[0],
            profile_image_url=info.get("picture"),
        )
    if provider == "apple":
        claims = apple.verify_identity_token(token)
        email = claims.get("email")
        # identityToken 에는 이름이 없다 — 최초 인증 시 FE 가 body.name 으로
        # 전달하며(auth 라우터에서 우선 적용), 없으면 이메일 앞부분으로 폴백.
        return SocialIdentity(
            provider="apple",
            social_id=claims["sub"],
            email=email,
            nickname=(email or "사과유저").split("@")[0],
            profile_image_url=None,
        )
    raise APIError(
        400,
        "VALIDATION_ERROR",
        f"지원하지 않는 provider 입니다: {provider}",
        details=[{"field": "provider", "reason": "unsupported"}],
    )
