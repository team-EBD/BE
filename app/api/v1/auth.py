"""인증 라우터 (Phase 2, 명세서 3장).

- POST /auth/social/login: 제공자 토큰 검증 → (social_provider, social_id) 조회/자동 생성
  → 자체 JWT 발급. 기존 200 / 신규 201.
- POST /auth/signup / POST /auth/login: 이메일 가입/로그인.
  users 에는 social_provider="email", social_id=<소문자 이메일> 로 저장한다.
- POST /auth/refresh: refresh 회전(기존 철회 → 새 쌍 발급).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.core.deps import DB
from app.core.errors import APIError
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.timeutil import from_db, now_utc
from app.models import RefreshToken, User, UserProfile
from app.schemas.auth import (
    EmailAuthResponse,
    EmailLoginRequest,
    EmailSignupRequest,
    RefreshRequest,
    RefreshResponse,
    SocialLoginRequest,
    SocialLoginResponse,
)
from app.services.nickname import allocate_nickname_tag
from app.services.summary import DEFAULT_GOALS, derive_macro_goals
from app.social_client import SocialIdentity, verify_social_token

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_PROVIDER = "email"


def get_social_verifier() -> Callable[[str, str], SocialIdentity]:
    """테스트에서 dependency_overrides 로 교체하는 검증기 의존성."""
    return verify_social_token


def _issue_token_pair(db, user_id: int) -> tuple[str, str]:
    """access/refresh 발급 + refresh 해시 저장."""
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    payload = decode_token(refresh, expected_type="refresh")
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    db.add(
        RefreshToken(user_id=user_id, token_hash=hash_token(refresh), expires_at=expires_at)
    )
    return access, refresh


@router.post("/social/login", response_model=SocialLoginResponse)
def social_login(
    body: SocialLoginRequest,
    db: DB,
    response: Response,
    verifier: Callable[[str, str], SocialIdentity] = Depends(get_social_verifier),
) -> SocialLoginResponse:
    identity = verifier(body.provider, body.token)

    user = db.scalar(
        select(User).where(
            User.social_provider == identity.provider,
            User.social_id == identity.social_id,
        )
    )
    created = user is None
    if created:
        user = User(
            social_provider=identity.provider,
            social_id=identity.social_id,
            email=identity.email,
            nickname=identity.nickname,
            nickname_tag=allocate_nickname_tag(db, identity.nickname),
            profile_image_url=identity.profile_image_url,
        )
        db.add(user)
        db.flush()  # id 확보

    access, refresh = _issue_token_pair(db, user.id)
    db.commit()

    response.status_code = 201 if created else 200
    return SocialLoginResponse(access_token=access, refresh_token=refresh, user=user)


@router.post("/signup", response_model=EmailAuthResponse, status_code=201)
def email_signup(body: EmailSignupRequest, db: DB) -> EmailAuthResponse:
    email_lower = body.email.lower()
    existing = db.scalar(
        select(User).where(
            User.social_provider == EMAIL_PROVIDER,
            User.social_id == email_lower,
        )
    )
    if existing is not None:
        raise APIError(409, "CONFLICT", "이미 가입된 이메일입니다.")

    user = User(
        social_provider=EMAIL_PROVIDER,
        social_id=email_lower,
        email=body.email,
        nickname=body.nickname,
        nickname_tag=allocate_nickname_tag(db, body.nickname),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.flush()  # id 확보

    # 온보딩 입력(성별/키/몸무게) + 기본 목표 (칼로리 2000, 탄단지 50:30:20 유도)
    macros = derive_macro_goals(DEFAULT_GOALS["calories"])
    db.add(
        UserProfile(
            user_id=user.id,
            goal_calories=DEFAULT_GOALS["calories"],
            goal_carbs=macros["carbs"],
            goal_protein=macros["protein"],
            goal_fat=macros["fat"],
            gender=body.gender,
            height=body.height,
            weight=body.weight,
        )
    )

    access, refresh = _issue_token_pair(db, user.id)
    db.commit()
    return EmailAuthResponse(access_token=access, refresh_token=refresh, user=user)


@router.post("/login", response_model=EmailAuthResponse)
def email_login(body: EmailLoginRequest, db: DB) -> EmailAuthResponse:
    # 이메일/비밀번호 중 무엇이 틀렸는지 노출하지 않는다 (소셜 전용 계정 포함)
    invalid = APIError(401, "UNAUTHORIZED", "이메일 또는 비밀번호가 올바르지 않습니다.")
    user = db.scalar(
        select(User).where(
            User.social_provider == EMAIL_PROVIDER,
            User.social_id == body.email.strip().lower(),
        )
    )
    if (
        user is None
        or user.password_hash is None
        or not verify_password(body.password, user.password_hash)
    ):
        raise invalid

    access, refresh = _issue_token_pair(db, user.id)
    db.commit()
    return EmailAuthResponse(access_token=access, refresh_token=refresh, user=user)


@router.post("/refresh", response_model=RefreshResponse)
def refresh_tokens(body: RefreshRequest, db: DB) -> RefreshResponse:
    invalid = APIError(401, "UNAUTHORIZED", "Refresh Token이 만료되었거나 유효하지 않습니다.")
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
    except TokenError:
        raise invalid

    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token))
    )
    now = now_utc()
    if stored is None or stored.revoked_at is not None or from_db(stored.expires_at) <= now:
        raise invalid

    # 회전: 기존 토큰 철회 후 새 쌍 발급
    stored.revoked_at = now
    access, refresh = _issue_token_pair(db, int(payload["sub"]))
    db.commit()
    return RefreshResponse(access_token=access, refresh_token=refresh)
