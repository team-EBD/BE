"""인증 라우터 (Phase 2, 명세서 3장).

- POST /auth/social/login: 제공자 토큰 검증 → (social_provider, social_id) 조회/자동 생성
  → 자체 JWT 발급. 기존 200 / 신규 201.
- POST /auth/signup / POST /auth/login: 이메일 가입/로그인.
  users 에는 social_provider="email", social_id=<소문자 이메일> 로 저장한다.
- POST /auth/refresh: refresh 회전(기존 철회 → 새 쌍 발급).
- POST /auth/password/forgot / POST /auth/password/reset: 비밀번호 재설정
  (이메일로 6자리 인증코드 발송 → 코드 검증 후 새 비밀번호 저장).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select

from app.core.config import settings
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
from app.mail_client import MailClient, MailSendError, get_mail_client
from app.models import RefreshToken, User, UserProfile
from app.schemas.auth import (
    EmailAuthResponse,
    EmailLoginRequest,
    EmailSignupRequest,
    PasswordForgotRequest,
    PasswordMessageResponse,
    PasswordResetRequest,
    RefreshRequest,
    RefreshResponse,
    SocialLoginRequest,
    SocialLoginResponse,
)
from app.services.goals import personalized_goals
from app.services.nickname import allocate_nickname_tag
from app.services.password_reset import consume_code, issue_code
from app.services.summary import DEFAULT_GOALS, derive_macro_goals
from app.social_client import SocialIdentity, verify_social_token

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_PROVIDER = "email"


def get_social_verifier() -> Callable[[str, str], SocialIdentity]:
    """테스트에서 dependency_overrides 로 교체하는 검증기 의존성."""
    return verify_social_token


def get_mailer() -> MailClient:
    """테스트에서 dependency_overrides 로 교체하는 메일 클라이언트 의존성."""
    return get_mail_client()


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
        # Apple 은 이름을 최초 인증 응답에만 주므로 body.name 이 있으면 우선한다
        # (구글은 토큰에 이름이 있어 body.name 을 보내지 않는다)
        nickname = (body.name or identity.nickname).strip() or "사용자"
        user = User(
            social_provider=identity.provider,
            social_id=identity.social_id,
            email=identity.email,
            nickname=nickname,
            nickname_tag=allocate_nickname_tag(db, nickname),
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

    # 온보딩 입력(성별/키/몸무게)으로 BMR/TDEE 기반 목표를 자동 산정한다.
    # (신체정보 부족 시에만 기본 목표 2000 kcal 사용 — 가입 스키마상 발생하지 않음)
    goals = personalized_goals(body.gender, None, body.height, body.weight) or {
        "calories": DEFAULT_GOALS["calories"],
        **derive_macro_goals(DEFAULT_GOALS["calories"]),
    }
    db.add(
        UserProfile(
            user_id=user.id,
            goal_calories=goals["calories"],
            goal_carbs=goals["carbs"],
            goal_protein=goals["protein"],
            goal_fat=goals["fat"],
            goal_source="auto",
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


@router.post("/password/forgot", response_model=PasswordMessageResponse)
def password_forgot(
    body: PasswordForgotRequest,
    db: DB,
    mailer: MailClient = Depends(get_mailer),
) -> PasswordMessageResponse:
    email_lower = body.email.strip().lower()
    user = db.scalar(
        select(User).where(
            User.social_provider == EMAIL_PROVIDER,
            User.social_id == email_lower,
        )
    )
    if user is None:
        # 소셜 가입자가 비밀번호를 찾으려는 흔한 실수 — 가입 경로를 안내한다
        social = db.scalar(
            select(User).where(
                func.lower(User.email) == email_lower,
                User.social_provider != EMAIL_PROVIDER,
            )
        )
        if social is not None:
            raise APIError(
                409,
                "CONFLICT",
                "소셜 계정으로 가입된 이메일입니다. 소셜 로그인을 이용해주세요.",
            )
        raise APIError(404, "NOT_FOUND", "가입되지 않은 이메일입니다.")

    code = issue_code(db, user)
    expire_minutes = settings.password_reset_code_expire_minutes
    try:
        mailer.send(
            to=email_lower,
            subject="[eatlog] 비밀번호 재설정 인증코드",
            body=(
                f"비밀번호 재설정 인증코드: {code}\n\n"
                f"{expire_minutes}분 안에 앱에 입력해주세요.\n"
                "본인이 요청하지 않았다면 이 메일을 무시하셔도 됩니다."
            ),
        )
    except MailSendError:
        db.rollback()
        raise APIError(
            500, "INTERNAL_ERROR", "인증코드 메일 발송에 실패했습니다. 잠시 후 다시 시도해주세요."
        )
    db.commit()
    return PasswordMessageResponse(
        message=f"인증코드를 이메일로 보냈습니다. {expire_minutes}분 안에 입력해주세요."
    )


@router.post("/password/reset", response_model=PasswordMessageResponse)
def password_reset(body: PasswordResetRequest, db: DB) -> PasswordMessageResponse:
    email_lower = body.email.strip().lower()
    user = db.scalar(
        select(User).where(
            User.social_provider == EMAIL_PROVIDER,
            User.social_id == email_lower,
        )
    )
    invalid = APIError(400, "VALIDATION_ERROR", "인증코드가 올바르지 않습니다.")
    if user is None:
        raise invalid

    result = consume_code(db, user, body.code)
    if result != "ok":
        db.commit()  # 불일치 시도 횟수(attempt_count) 저장
        if result == "expired":
            raise APIError(
                400, "VALIDATION_ERROR", "인증코드가 만료되었습니다. 다시 요청해주세요."
            )
        raise invalid

    user.password_hash = hash_password(body.new_password)
    # 보안: 비밀번호 변경 시 기존 로그인 세션(refresh)을 전부 철회한다
    now = now_utc()
    active_tokens = db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    for token in active_tokens:
        token.revoked_at = now
    db.commit()
    return PasswordMessageResponse(
        message="비밀번호가 변경되었습니다. 새 비밀번호로 로그인해주세요."
    )


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
